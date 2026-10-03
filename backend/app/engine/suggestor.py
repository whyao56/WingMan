"""候选回复生成 + 本地打分。

两个关键设计：

**1. 先分风格再生成。**
直接让模型"给几个回复"，出来的几条会高度同质（同一个意思换词）。
先指定 4 种策略风格各生成一条，强制发散。

**2. 不让模型给自己打分。**
模型对自产内容的自评普遍偏高、方差极小，排不出先后。
这里改用可解释的本地启发式打分，好处是：
- 有区分度（长度、雷区、AI 腔这些是硬指标）
- 用户看得懂分数是怎么来的（`score_notes` 会列出扣分理由）
- 不额外消耗 token

**分数是给"排序"用的，不是给"判断"用的。** 差 0.3 分不代表 A 比 B 好，
但 8.6 vs 4.1 的差距是有意义的。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..schemas import PeerAnalysis, Prediction, ReplyOption, Scores, Strategy
from . import prompts
from .context import ContextPack

log = logging.getLogger("chatwing.engine.suggestor")

STYLES = ("接梗调侃", "真诚共情", "好奇引导", "顺势推进", "保守稳妥")

AI_TELLS = (
    "作为一个", "首先", "其次", "总的来说", "总之", "综上", "希望我的建议",
    "需要注意的是", "建议你", "希望能帮到", "换言之", "从这个角度", "综合来看",
    "值得注意的是", "一方面", "另一方面", "不失为", "无疑",
)
BOOKISH = ("因此", "此外", "例如", "务必", "应当", "似乎", "或许可以", "若", "倘若", "并非")
ABSOLUTES = ("永远", "一直", "唯一", "一辈子", "绝对", "从不", "必须")
EMOTION_WORDS = (
    "辛苦", "累", "抱抱", "心疼", "别急", "懂你", "理解", "陪", "顺其自然",
    "没事的", "我懂", "委屈", "难过", "开心", "逗", "笑",
)
ACTION_WORDS = ("约", "一起", "带你去", "周末", "见面", "出来", "安排", "走起", "接你", "请你")
QUESTION_WORDS = ("吗", "？", "?", "呢", "怎么", "什么", "要不要", "展开", "讲讲", "说说")
MOOD_WORDS = ("啊", "呀", "吧", "呢", "嘛", "哦", "哈", "嘿", "诶", "唉", "啦", "嘞", "哇", "咯")

_PROGRESS_GOAL_HINTS = ("约", "见面", "推进", "表白", "确定关系", "线下", "一起")


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _ngrams(s: str, n: int = 2) -> set[str]:
    t = re.sub(r"[\s\W]+", "", s or "")
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _split_taboos(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,，、;；\n]+", raw)
    out = []
    for p in parts:
        p = p.strip(" -·•\t")
        if p and p not in ("无", "暂无", "暂未识别到明确雷区", "(空)"):
            out.append(p)
    return out


# ================================================================ 打分


def local_score(
    text: str,
    *,
    analysis: PeerAnalysis | None = None,
    persona: Any = None,
    peer_message: str = "",
) -> tuple[Scores, list[str], float, Prediction]:
    """返回 (四维分数, 打分理由, 总分 0~10, 走向预测)。"""
    notes: list[str] = []
    t = (text or "").strip()
    L = len(t)

    # ---------- 自然度 ----------
    if L < 3:
        n, why = 0.35, "过短，像在敷衍"
    elif L <= 25:
        n, why = 1.0, None
    elif L <= 40:
        n, why = 0.72, "略长，聊天里显得啰嗦"
    else:
        n, why = 0.35, "过长，像在写小作文"
    if why:
        notes.append(f"− {why}")
    if any(w in t for w in MOOD_WORDS):
        n += 0.05
        notes.append("+ 带语气词，更像真人打字")
    tells = [x for x in AI_TELLS if x in t]
    if tells:
        n -= 0.55
        notes.append(f"− 出现 AI 腔（「{tells[0]}」）")
    if any(b in t for b in BOOKISH):
        n -= 0.25
        notes.append("− 书面语偏重")
    n = _clamp(n)

    # ---------- 共情温度 ----------
    w = 0.25
    topics = (analysis.topics if analysis else []) or []
    hit = [x for x in topics if x and x in t]
    if hit:
        w += min(0.45, 0.2 * len(hit))
        notes.append(f"+ 呼应了对方提到的「{hit[0]}」")
    if any(e in t for e in EMOTION_WORDS):
        w += 0.3
        notes.append("+ 对情绪做了回应")
    if analysis and analysis.emotion_intensity >= 7 and not hit and not any(e in t for e in EMOTION_WORDS):
        w -= 0.2
        notes.append("− 对方情绪强度高，这条没有接住情绪")
    w = _clamp(w)

    # ---------- 目标推进 ----------
    acts = [a for a in ACTION_WORDS if a in t]
    if acts:
        p = 0.85
        notes.append(f"+ 含行动引导（「{acts[0]}」）")
    elif any(q in t for q in QUESTION_WORDS):
        p = 0.55
        notes.append("+ 用提问把话头递了回去")
    else:
        p = 0.3
    goal = getattr(persona, "goal", "") or ""
    if goal and any(k in goal for k in _PROGRESS_GOAL_HINTS) and acts:
        p = min(1.0, p + 0.15)
        notes.append("+ 与你的目标方向一致")
    p = _clamp(p)

    # ---------- 风险 ----------
    r = 0.0
    for tb in _split_taboos(getattr(persona, "taboos", "") or ""):
        if tb and tb in t:
            r += 0.6
            notes.append(f"− 命中雷区「{tb}」")
            break
    ex = t.count("！") + t.count("!")
    if ex >= 3:
        r += 0.2
        notes.append("− 感叹号过多，用力过猛")
    abs_hit = [a for a in ABSOLUTES if a in t]
    if abs_hit:
        r += 0.25
        notes.append(f"− 绝对化表达（「{abs_hit[0]}」），给人压力")
    if peer_message:
        a, b = _ngrams(t), _ngrams(peer_message)
        if a and b:
            jac = len(a & b) / max(1, len(a | b))
            if jac > 0.55:
                r += 0.3
                notes.append("− 与对方原话重合度过高，像复读")
    if L > 60:
        r += 0.15
    if tells:
        r += 0.3
    r = _clamp(r)

    scores = Scores(
        naturalness=round(n, 3), warmth=round(w, 3),
        progress=round(p, 3), risk=round(r, 3),
    )
    raw = 0.30 * n + 0.25 * w + 0.30 * p - 0.35 * r
    total = round(1 + 9 * _clamp(raw), 1)

    if r >= 0.5:
        direction = "降温"
    elif p >= 0.6 and r <= 0.25:
        direction = "升温"
    else:
        direction = "持平"
    confidence = round(_clamp(0.35 + 0.5 * _clamp(raw) - 0.3 * r, 0.1, 0.95), 2)

    if not notes:
        notes.append("各维度表现均衡，没有明显减分项")
    return scores, notes, total, Prediction(direction=direction, confidence=confidence)


# ================================================================ 生成


def _coerce_option(item: dict[str, Any], index: int) -> ReplyOption:
    oid = str(item.get("id") or chr(ord("A") + index)).strip()[:4]
    return ReplyOption(
        id=oid,
        style=str(item.get("style") or (STYLES[index] if index < len(STYLES) else "其他"))[:12],
        text=str(item.get("text") or "").strip()[:400],
        rationale=str(item.get("rationale") or "")[:400],
        expected_effect=str(item.get("expected_effect") or "")[:300],
        risk=str(item.get("risk") or "")[:300],
    )


async def suggest(
    ctx: Any, pack: ContextPack, analysis: PeerAnalysis, strategy: Strategy
) -> tuple[list[ReplyOption], list[str]]:
    warnings: list[str] = []
    analysis_text = (
        f"情绪：{analysis.emotion}（{analysis.emotion_intensity}/10）\n"
        f"兴趣变化：{analysis.interest_delta:+d}\n"
        f"意图：{analysis.intent}\n"
        f"潜台词：{analysis.subtext}\n"
        f"风险：{analysis.risk}"
    )
    strategy_text = (
        f"阶段：{strategy.stage}\n本轮目标：{strategy.goal_this_turn}\n语气：{strategy.tone}\n"
        f"必须做到：{'；'.join(strategy.must_do)}\n必须避免：{'；'.join(strategy.must_not)}"
    )

    user = prompts.suggest_user(
        profile=pack.profile_text,
        stage=strategy.stage,
        goal=pack.persona.goal,
        taboos=pack.persona.taboos,
        style_samples=pack.style_text,
        recent=pack.recent_text,
        peer_message=pack.peer_message,
        analysis=analysis_text,
        strategy=strategy_text,
    )
    data = await ctx.llm.complete_json(
        prompts.SUGGEST_SYSTEM, user,
        schema_hint=prompts.SUGGEST_SCHEMA, temperature=0.9,
    )

    raw_items = data.get("options") or data.get("items") or []
    options: list[ReplyOption] = []
    for i, item in enumerate(raw_items[:6]):
        if not isinstance(item, dict):
            continue
        opt = _coerce_option(item, i)
        if not opt.text:
            continue
        scores, notes, total, pred = local_score(
            opt.text, analysis=analysis, persona=pack.persona, peer_message=pack.peer_message,
        )
        opt.scores = scores
        opt.score_notes = notes
        opt.total = total
        opt.prediction = pred
        options.append(opt)

    if not options:
        warnings.append("模型没有产出可用的回复建议，请重试或换一个模型。")
    elif len(options) < 3:
        warnings.append(f"只生成了 {len(options)} 条建议（期望 4 条），可能是模型输出被截断。")

    options.sort(key=lambda o: -o.total)
    return options, warnings
