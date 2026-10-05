"""Mock Provider —— 不用任何 API Key 也能把整条链路跑出「像样」的结果。

它不是返回一堆 lorem ipsum，而是**用规则真的去读你给的上下文**：
数标点、看长度、抽关键词、匹配雷区词、扫事实句式。
所以第一次跑起来时，你会看到一个能自圆其说的完整输出，
足以判断交互流程对不对，再决定要不要接真模型。

它的输出**不可当作真实智能**。定性一句话：这是个写得比较认真的假货。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import ChatProvider, LLMError, task_tag


def _make_provider_note() -> str:
    return "内置规则引擎（无需 API Key，输出仅供流程演示）"


# ---------------------------------------------------------------- 文本工具

_STOP = {
    "的", "了", "啊", "呀", "吧", "呢", "吗", "嘛", "哦", "嗯", "唉", "哈", "哈哈", "哈哈哈",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "自己",
    "是", "在", "有", "和", "跟", "就", "都", "也", "很", "不", "没", "要", "会", "能",
    "一", "个", "这", "那", "这个", "那个", "什么", "怎么", "为什么", "可以", "还是",
    "今天", "明天", "昨天", "现在", "然后", "但是", "因为", "所以", "如果", "只是",
    "感觉", "真的", "有点", "一点", "一下", "已经", "还是", "其实", "就是",
}

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,8}")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F1E6-\U0001F1FF]|"
    "\\[(?:微笑|偷笑|大哭|流泪|捂脸|呲牙|憨笑|玫瑰|爱心|强|点赞|比心|破涕为笑)\\]"
)


# prompts.py 的 user 模板在最后一个小节之后，还会跟一句**给模型的指令**
# （「请分析这条消息。」「请给出本轮策略。」「请给出 4 条候选回复。」
#   「请推演发出这句话之后，对话会怎么走。」）。
#
# 而小节提取用的正则终止符只有 `\n【` / `\n<<<` / 文本结尾，所以当某个【小节】
# 正好是模板的最后一段时（单条分析的默认路径就是这样），这句指令会被当成小节内容：
#
#     extract_peer_message('【对方最新消息】\n哈哈哈今天好累啊\n\n请分析这条消息。')
#       → '哈哈哈今天好累啊\n\n请分析这条消息。'
#     keywords(..., 5) → ['今天好累', '分析这条', '析这条消', '请分析', '条消息']
#
# 后几个词接着就会被写进用户能看到的「潜台词」文案里。
#
# 修法：按**整行精确匹配**剥掉末尾的指令行。
# 刻意不用「以『请』开头就删」这类模糊规则 —— 用户自己的话（「请我吃饭吧」）
# 正是以「请」开头，模糊规则会误伤真实消息，那是比原缺陷更糟的问题。
PROMPT_TAIL_LINES: tuple[str, ...] = (
    "请分析这条消息。",
    "请给出本轮策略。",
    "请给出 4 条候选回复。",
    "请推演发出这句话之后，对话会怎么走。",
    "请综合出画像。",
)


def strip_prompt_tail(text: str) -> str:
    """剥掉文本末尾的 prompt 指令行（以及它前面的空行）。

    - 只做整行精确匹配，且只动**末尾**：正文中间出现同样的句子不受影响；
    - 干净输入（没有可剥的行）原样返回（只做 strip），不重排换行；
    - 用户消息本身以「请」开头（「请我吃饭吧」）不会被误伤 —— 它不等于任何指令行。
    """
    raw = text or ""
    lines = raw.splitlines()
    popped = False
    while lines:
        if not lines[-1].strip():
            lines.pop()
            popped = True
            continue
        if lines[-1].strip() in PROMPT_TAIL_LINES:
            lines.pop()
            popped = True
            continue
        break
    if not popped:
        return raw.strip()
    return "\n".join(lines).strip()


def extract_peer_message(user_text: str) -> str:
    """从 prompt 里抠出「对方最新消息」。"""
    m = re.search(r"【对方最新消息】\s*\n?(.*?)(?:\n【|\n<<<|$)", user_text, re.S)
    if m:
        return strip_prompt_tail(m.group(1)).strip().strip('"“”')
    # 退化：取最后一段非空且不像元信息的文本（指令行同样要跳过 —— 它不是对方的话）
    for line in reversed(user_text.splitlines()):
        t = line.strip()
        if not t or t.startswith(("【", "#", "-", "[TASK")):
            continue
        if t in PROMPT_TAIL_LINES:
            continue
        if len(t) < 200:
            return t
    return ""


# 单个停用字。判断 n-gram 是否"像词"时看它的首尾字符：
# 以虚词开头或结尾的窗口基本都是切偏的产物（「哈哈今」「累啊」）。
_STOP_CHARS = set(
    "的了啊呀吧呢吗嘛哦嗯哈唉诶啦哇咯嘞我你他她它们是在有和跟就都也很不没要会能一"
    "个这那什么怎么还如果只是感觉真的有点已经其实于与之其为以"
)


def _squash(text: str) -> str:
    """把连续重复字收敛成两个：哈哈哈 → 哈哈，啊啊啊啊 → 啊啊。

    不收敛的话，滑动窗口会切出「哈哈哈今」这种明显不该当关键词的东西。
    """
    return re.sub(r"(.)\1{2,}", r"\1\1", text or "")


def _is_wordish(gram: str) -> bool:
    if len(gram) < 2:
        return False
    if gram in _STOP:
        return False
    if gram[0] in _STOP_CHARS or gram[-1] in _STOP_CHARS:
        return False
    # 停用字占比过高也不像词
    if sum(1 for c in gram if c in _STOP_CHARS) >= 2:
        return False
    return True


def keywords(text: str, limit: int = 5) -> list[str]:
    """朴素中文关键词：2~4 字滑动窗口 + 停用字首尾过滤 + 长度优先。

    比不上分词器，但在这个场景够用：它要的是"看起来像话题词的东西"，
    用来填 mock 的模板、以及给信号描述当佐证。
    """
    freq: dict[str, int] = {}
    for run in _CJK_RUN.findall(_squash(text)):
        if len(run) < 2:
            continue
        for n in (2, 3, 4):
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                gram = run[i:i + n]
                if not _is_wordish(gram):
                    continue
                freq[gram] = freq.get(gram, 0) + 1

    if not freq:
        return []

    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    out: list[str] = []
    for w, _ in ranked:
        # 已被更长的候选覆盖就跳过（「今天好累」收下后，「今天」就不必再出）
        if any(w in o for o in out):
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


_EMOTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("疲惫", ("累", "疲", "困", "加班", "熬夜", "忙死", "撑不住", "心力交瘁")),
    ("低落", ("难过", "难受", "委屈", "想哭", "崩溃", "郁闷", "低落", "丧", "emo", "不开心")),
    ("不满", ("生气", "气死", "讨厌", "烦人", "烦死", "恼火", "无语", "受不了")),
    ("焦虑", ("紧张", "焦虑", "担心", "压力", "怕", "慌")),
    ("开心", ("开心", "高兴", "好棒", "太好了", "喜欢", "期待", "爽", "幸福")),
    ("轻松愉悦", ("哈哈", "嘻嘻", "笑死", "好玩", "有趣", "有意思")),
    ("温柔亲近", ("想你", "抱抱", "晚安", "早安", "照顾好", "注意身体")),
)


def detect_emotion(text: str) -> tuple[str, int]:
    t = text or ""
    hits: list[tuple[str, int]] = []
    for label, words in _EMOTION_RULES:
        n = sum(t.count(w) for w in words)
        if n:
            hits.append((label, n))
    if not hits:
        base = "平静"
    else:
        hits.sort(key=lambda kv: -kv[1])
        base = hits[0][0]

    intensity = 3
    intensity += min(3, t.count("!") + t.count("！"))
    intensity += min(2, len(_EMOJI.findall(t)))
    if re.search(r"(.)\1{2,}", t):   # 哈哈哈 / 啊啊啊
        intensity += 1
    if hits:
        intensity += min(2, hits[0][1])
    if len(t) >= 25:
        intensity += 1
    return base, max(1, min(10, intensity))


_DRY = ("嗯", "哦", "好的", "行", "知道了", "随便", "都行", "还行", "在忙", "回头说")


def signals_of(text: str) -> list[str]:
    t = (text or "").strip()
    out: list[str] = []
    out.append(f"消息长度 {len(t)} 字")
    q = t.count("?") + t.count("？")
    if q:
        out.append(f"含 {q} 个问句 —— 她在把话头递回来，期待你接")
    emo = _EMOJI.findall(t)
    if emo:
        out.append(f"使用了 {len(emo)} 个表情/表情符，情绪外放")
    ex = t.count("!") + t.count("！")
    if ex >= 2:
        out.append(f"连用 {ex} 个感叹号，情绪浓度高")
    if re.search(r"(.)\1{2,}", t):
        out.append("出现重复拖音（如「哈哈哈」「啊啊啊」），处于放松状态")
    if any(d in t for d in _DRY) and len(t) <= 8:
        out.append("回复偏短且用词中性 —— 当前话题热度不高，不宜硬推")
    if len(t) <= 4:
        out.append("消息很短，可能是随手回，也可能在忙")
    if not out:
        out.append("语气平稳，没有明显情绪波动")
    return out[:5]


def read_taboo_section(user_text: str) -> list[str]:
    m = re.search(r"【(?:雷区|禁忌|must_not)】\s*\n?(.*?)(?:\n【|\n<<<|$)", user_text, re.S)
    if not m:
        return []
    raw = strip_prompt_tail(m.group(1))
    parts = [p.strip(" -·•\t") for p in re.split(r"[,，、;；\n]+", raw) if p.strip()]
    return [p for p in parts if p and p not in ("无", "(空)", "暂无")]


def read_section(user_text: str, *names: str) -> str:
    for n in names:
        m = re.search(rf"【{re.escape(n)}】\s*\n?(.*?)(?:\n【|\n<<<|$)", user_text, re.S)
        if m:
            # 同样剥掉末尾指令行：若这个小节正好是模板最后一段，不剥就会被当成内容
            return strip_prompt_tail(m.group(1))
    return ""


# ---------------------------------------------------------------- 事实抽取

_FACT_PATTERNS: tuple[tuple[str, str, float], ...] = (
    (r"我(?:最|超|特别)?(?:喜欢|爱|超喜欢|特别喜欢)([\u4e00-\u9fffA-Za-z0-9]{1,12})", "喜欢", 0.75),
    (r"我(?:不喜欢|讨厌|最烦|受不了|烦死了?)([\u4e00-\u9fffA-Za-z0-9]{1,12})", "讨厌", 0.75),
    (r"我(?:叫|的名字是|名字叫)([\u4e00-\u9fff]{1,8})", "名字", 0.8),
    # 「我是……」几乎必然误命中「我是认真的」这类表述，所以只认「在做/从事/是做」
    (r"我(?:在做|从事|是做)([\u4e00-\u9fff]{2,10})", "身份/职业", 0.7),
    (r"我(?:住在|老家在|老家是)([\u4e00-\u9fff]{2,10})", "所在地", 0.7),
    (r"我(?:养|有)(?:一)?(?:只|条|个)?(猫|狗|仓鼠|兔子|金毛|橘猫)", "养的宠物", 0.8),
    (r"(?:我|我家)(?:的)?(?:猫|狗)叫([\u4e00-\u9fff]{1,8})", "宠物名字", 0.85),
    (r"(?:生日|过生日)(?:是|在)?(\d{1,2}月\d{1,2}[日号])", "生日", 0.9),
    (r"我(?:最近|在|正)(?:学|在学|学)([\u4e00-\u9fff]{2,10})", "最近在学", 0.7),
    (r"(?:周末|假期|这两天)(?:我)?(?:去|在)([\u4e00-\u9fff]{2,12})", "近期行程", 0.6),
    (r"我(?:特别|超|很)?(?:爱吃|想吃|馋)([\u4e00-\u9fff]{1,10})", "爱吃", 0.7),
)

_LINE_ID = re.compile(r"#(\d+)\|")

# 同一个 key 下最多保留几条。事实抽取的总量上限也要放开：
# 早期版本只留 1 条/键 + 总量 20，会把「喜欢猫」「喜欢火锅」这种
# 同类但不同内容的事实压掉一半。
MAX_FACTS_PER_KEY = 4
MAX_FACTS_TOTAL = 30

# 抽出来的值常带着句尾语气词或标点（"猫的" "看展吗"），
# 以及一些明显不构成事实的泛化词（"认真的" "这样的"）。
_TAIL_PARTICLES = "的了吗啊呀吧呢嘛哦嗯哈啦嘞哇诶唉"
_FACT_BLACKLIST = {
    "认真的", "真的", "对的", "错的", "这样的", "那样的", "什么的", "一个人",
    "自己", "这种", "那种", "一样", "回事", "意思", "事情", "东西", "时候",
    # 清掉句尾语气词后可能残留的单字/双字功能词
    "的", "了", "吗", "吧", "呢", "啊", "呀", "哦", "嗯", "是", "在", "有",
    "什么", "怎么", "这样", "那样", "这些", "那些", "一个", "多少",
}

# 这些键允许单字取值：中文里「喜欢猫」「爱吃辣」本身就是完整事实。
# 其他键的单字结果基本是语气词残渣，丢弃更安全。
_SHORT_VALUE_KEYS = {"喜欢", "讨厌", "爱吃", "养的宠物"}


def clean_fact_value(value: str) -> str:
    v = re.sub(r"[\s，。！？,.!?、；;：:…~～]+$", "", (value or "").strip())
    while v and v[-1] in _TAIL_PARTICLES:
        v = v[:-1]
    return v.strip()


def extract_facts_from_context(user_text: str) -> list[dict[str, Any]]:
    """在带 `#id|sender|text` 标记的上下文里扫事实句式。"""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for line in user_text.splitlines():
        mid = _LINE_ID.search(line)
        body = line.split("|", 2)[-1] if "|" in line else line
        speaker = "peer"
        parts = line.split("|", 2)
        if len(parts) >= 3:
            speaker = "me" if parts[1].strip() in ("我", "me", "ME") else "peer"
        for pat, key, conf in _FACT_PATTERNS:
            m = re.search(pat, body)
            if not m:
                continue
            value = clean_fact_value(m.group(1))
            if not value or value in _FACT_BLACKLIST:
                continue
            # 注意：不要用「长度 < 2」一刀切 —— "我超喜欢猫的" 清成 "猫" 后
            # 只有 1 个字，但它是真事实。改由键的白名单来决定。
            if len(value) < 2 and key not in _SHORT_VALUE_KEYS:
                continue
            k = (speaker, f"{key}:{value}")
            if k in found:
                continue
            found[k] = {
                "subject": speaker,
                "key": key,
                "value": value,
                "confidence": conf,
                "evidence": mid.group(1) if mid else "",
            }
    # 同 key 分组保留前 N 条（按置信度）。
    #
    # 这里**不能**只保留 1 条：「我超爱吃火锅的」和「我超喜欢猫的」都会被
    # "喜欢" 命中，只留一条会让画像凭空丢掉「她喜欢猫」这种关键信息。
    # 而且 found 的键本身就是 (speaker, key:value)，重复项早已合并，
    # 这层压缩只该起到"防止单一 key 刷屏"的作用，不该丢不同内容的事实。
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in found.values():
        grouped.setdefault((f["subject"], f["key"]), []).append(f)

    out: list[dict[str, Any]] = []
    for items in grouped.values():
        items.sort(key=lambda d: -d["confidence"])
        out.extend(items[:MAX_FACTS_PER_KEY])
    return out[:MAX_FACTS_TOTAL]


# ---------------------------------------------------------------- Provider


class MockProvider(ChatProvider):
    name = "mock"
    available = True

    @property
    def note(self) -> str:
        return _make_provider_note()

    async def chat_raw(self, messages: list[dict[str, str]], **kw: Any) -> str:
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = "\n".join(m["content"] for m in messages if m.get("role") == "user")
        tag = task_tag(system)
        payload = self._dispatch(tag, user)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def ping(self) -> tuple[bool, str]:
        """连通性自检：Mock 是内置演示引擎，本身就是「可用」的。

        基类的 ping() 会真发一次 complete()，而 MockProvider.chat_raw 是按 system
        首行的 [TASK:XXX] 标记分派的；「测试连通」用的 system 没有标记（task_tag
        返回 UNKNOWN），会被分派表当成错误 → 控制台上显示红叉，新用户会以为坏了。
        这里直接返回成功语义 + 解释文案：说明当前是演示引擎、去哪里接真实模型。
        真实 provider 的 ping()（真调一次）不受影响，仍由基类实现。
        """
        return True, (
            "演示引擎（内置规则）已就绪：不需要 API Key，输出仅用于跑通流程、看效果。"
            "要接真实模型：在「设置」里把 Provider 换成 OpenAI 兼容或 Ollama，"
            "填好 base_url / api_key / model 后再次点「测试连通」。"
        )

    # ------------------------------------------------------ 分派

    def _dispatch(self, tag: str, user: str) -> dict[str, Any]:
        handler = {
            "ANALYZE": self._analyze,
            "STRATEGY": self._strategy,
            "SUGGEST": self._suggest,
            "SIMULATE": self._simulate,
            "FACTS": self._facts,
            "PROFILE": self._profile,
        }.get(tag)
        if handler is None:
            raise LLMError(
                f"Mock Provider 不认识任务标记 [TASK:{tag}]。"
                f"请检查 prompts.py 是否正确设置了标记。"
            )
        return handler(user)

    # ------------------------------------------------------ 各任务

    def _analyze(self, user: str) -> dict[str, Any]:
        msg = extract_peer_message(user)
        emotion, intensity = detect_emotion(msg)
        kws = keywords(msg, 4)
        taboos = read_taboo_section(user)

        delta = 0
        if "?" in msg or "？" in msg:
            delta += 1
        if len(_EMOJI.findall(msg)) >= 2:
            delta += 1
        if re.search(r"(.)\1{2,}", msg):
            delta += 1
        if len(msg.strip()) <= 6 and not any(c in msg for c in "?？"):
            delta -= 1
        if any(d in msg for d in _DRY):
            delta -= 1
        delta = max(-3, min(3, delta))

        stage = read_section(user, "关系阶段") or "熟悉期"
        risk = "暂未发现明显风险点。"
        for t in taboos:
            if t and t in msg:
                risk = f"对方的话里出现了「{t}」，正好落在你设定的雷区边缘，回复时务必避开。"
                break
        if risk == "暂未发现明显风险点。" and any(w in msg for w in ("忙", "累", "加班")):
            risk = "她处于疲惫状态，此时推邀约/要承诺容易适得其反，宜轻不宜重。"

        subtext = {
            "疲惫": "她在释放「我今天很累」的信号，主要诉求是被理解，而不是被解决。",
            "低落": "情绪偏低，此刻需要的是陪伴感，不是建议。先接住情绪，别急着分析对错。",
            "不开心": "她可能遇到不顺心的事，愿意跟你说说明你在她的信任圈内。",
            "轻松愉悦": "状态放松，是适合闲聊和轻度试探的好窗口。",
            "开心": "情绪正面向，可以顺势往前推一点，成功率较高。",
            "不满": "可能有情绪需要出口，先别站队、别评判，让她说完。",
            "平静": "中性状态，话题可进可退，适合先接住再自然推进。",
            "焦虑": "她在为某件事紧绷，回复要稳，不要增加不确定性。",
        }.get(emotion, "她的表达偏中性，没有明显情绪指向，适合稳扎稳打地接话。")

        if kws:
            subtext += f" 从用词看，她此刻的关注点集中在「{'、'.join(kws[:3])}」。"

        return {
            "emotion": emotion,
            "emotion_intensity": intensity,
            "interest_delta": delta,
            "intent": (
                "把今天的状态分享给你，并留了一个话头等你接"
                if delta >= 0 else
                "随口回应，当前社交电量不高"
            ),
            "subtext": subtext,
            "topics": kws or ["日常"],
            "signals": signals_of(msg),
            "risk": risk,
            "stage_guess": stage,
        }

    def _strategy(self, user: str) -> dict[str, Any]:
        stage = read_section(user, "关系阶段") or "熟悉期"
        goal = read_section(user, "目标") or "自然地维持并加深联系"
        taboos = read_taboo_section(user)
        plan = {
            "暧昧期": ("制造专属感、巩固情绪连接", "轻松带点私人语境，不急着挑明"),
            "推进期": ("把话题落到具体的时间地点上", "自然、具体、给对方留退路"),
            "熟悉期": ("扩大话题面，找到更多共同点", "好奇、有来有回"),
            "陌生期": ("建立基础好感，避免压迫感", "克制、有分寸"),
            "稳定期": ("维持温度，做深度交流", "真诚、不敷衍"),
        }.get(stage, ("稳住节奏，自然推进", "轻松真诚"))
        goal_turn, tone = plan
        return {
            "stage": stage,
            "goal_this_turn": f"{goal_turn}（你的长期目标：{goal}）",
            "tone": tone,
            "must_do": ["先回应对方的情绪，再谈内容", "留一个开放的话头给对方"],
            "must_not": (taboos[:3] if taboos else []) + ["说教式安慰", "连续追问"],
        }

    def _suggest(self, user: str) -> dict[str, Any]:
        msg = extract_peer_message(user)
        kws = keywords(msg, 3)
        kw = kws[0] if kws else "今天"
        emotion, _ = detect_emotion(msg)

        if emotion in ("低落", "疲惫", "不满"):
            warm = f"听着就累，{kw}这事儿最磨人了。别硬扛，先歇会儿"
        else:
            warm = f"看你说得这么起劲，{kw}是今天最大的事了吧"

        return {
            "options": [
                {
                    "id": "A",
                    "style": "接梗调侃",
                    "text": f"那我是不是得给你颁个「{kw}」勋章，专治今天这种日子",
                    "rationale": "用玩笑把沉重的部分轻量化，同时表明你认真看了她说的内容",
                    "expected_effect": "大概率被逗笑并顺着接话，气氛继续保持轻松",
                    "risk": "如果她今天确实很低落，玩笑会显得没接住情绪",
                },
                {
                    "id": "B",
                    "style": "真诚共情",
                    "text": warm,
                    "rationale": "先给情绪一个落点，不急着给建议，避免「说教感」",
                    "expected_effect": "她更愿意继续往下说，信任感小幅上升",
                    "risk": "若长期只共情不推进，关系容易停在「树洞」位置",
                },
                {
                    "id": "C",
                    "style": "好奇引导",
                    "text": f"{kw}？展开讲讲，我想听细节",
                    "rationale": "把话头递回去，让她多说，同时传递「我愿意听」",
                    "expected_effect": "她输出更多信息，你能拿到更多可用于后续推进的素材",
                    "risk": "如果她本来就累，被追问细节可能会更累",
                },
                {
                    "id": "D",
                    "style": "顺势推进",
                    "text": f"那别自己扛了，周末带你去换个环境，跟{kw}彻底断联两小时",
                    "rationale": "把关心落到具体行动上，为线下见面铺路",
                    "expected_effect": "推进关系进度，若她答应则直接打开邀约窗口",
                    "risk": "节奏偏快，她若没准备好可能尴尬回避",
                },
            ]
        }

    def _simulate(self, user: str) -> dict[str, Any]:
        opt = read_section(user, "待推演回复") or extract_peer_message(user) or "（这条回复）"
        opt = opt[:60]
        # 关键词必须只从「对方最新消息」里取。
        # 从整个 prompt 取会把人物卡里的人名（比如「小鹿」）当成话题词，
        # 生成「不过确实挺小鹿的」这种莫名其妙的句子。
        kws = keywords(extract_peer_message(user), 3)
        kw = kws[0] if kws else "这个话题"

        return {
            "branches": [
                {
                    "label": "她接住了梗，情绪被调动",
                    "likelihood": "likely",
                    "steps": [
                        {"speaker": "peer", "text": f"哈哈你至于吗，不过确实挺{kw}的",
                         "note": "被逗笑，话题顺着原方向继续"},
                        {"speaker": "me", "text": "我这不是怕你一个人闷着",
                         "note": "接住轻松氛围，同时表达关心"},
                        {"speaker": "peer", "text": "那你还算有点良心",
                         "note": "带轻微撒娇意味，关系温度上行"},
                    ],
                    "outcome": "气氛延续，你获得了一次自然的情绪连接，后续可择机落到具体安排上",
                    "temperature": "升温",
                },
                {
                    "label": "她只回了个表情，兴致一般",
                    "likelihood": "possible",
                    "steps": [
                        {"speaker": "peer", "text": "[捂脸]", "note": "情绪响应弱，只是礼貌性回应"},
                        {"speaker": "me", "text": "哈哈行吧，那你先忙", "note": "及时收住，不追问"},
                    ],
                    "outcome": "话题降温但没伤到关系，此时应换话题或干脆放一放，明天再找机会",
                    "temperature": "持平",
                },
                {
                    "label": "她今天真的很累，没心思接话",
                    "likelihood": "unlikely",
                    "steps": [
                        {"speaker": "peer", "text": "嗯，我先去休息了", "note": "明确表示要结束对话"},
                        {"speaker": "me", "text": "好，早点睡，明天再聊", "note": "干脆收尾，不添压力"},
                    ],
                    "outcome": "今天推进失败，但你的得体收尾反而加了分 —— 疲惫时被纠缠是最掉分的",
                    "temperature": "降温",
                },
            ],
            "advice": (
                "这条回复的优势是「接住了情绪又不施压」，适合她状态还行的时候用。"
                "如果观察到她回复间隔变长、字数明显变少，建议换更轻的接法，或者干脆不推。"
            ),
        }

    def _facts(self, user: str) -> dict[str, Any]:
        facts = extract_facts_from_context(user)
        return {"facts": facts}

    def _profile(self, user: str) -> dict[str, Any]:
        facts = extract_facts_from_context(user)
        likes = [f["value"] for f in facts if f["key"] in ("喜欢", "爱吃", "最近在学")]
        dislikes = [f["value"] for f in facts if f["key"] in ("讨厌",)]
        pets = [f["value"] for f in facts if "宠物" in f["key"]]
        names = [f["value"] for f in facts if f["key"] == "名字"]

        parts: list[str] = []
        if names:
            parts.append(f"名字/昵称信息：{'、'.join(names)}。")
        if likes:
            parts.append(f"偏好：喜欢{'、'.join(likes[:5])}。")
        if dislikes:
            parts.append(f"明确表达过不喜欢：{'、'.join(dislikes[:4])}。")
        if pets:
            parts.append(f"养了宠物：{'、'.join(pets[:3])}。")
        parts.append("表达风格偏口语化，带较多语气词和表情，情绪表达直接。")
        parts.append("（以上由本地规则从聊天记录中抽取，准确度有限，建议接入真实模型后重建。）")

        taboos = list(dislikes[:2]) + ["在她表达疲惫时追问细节"]
        return {
            "peer_profile": "".join(parts),
            "taboos": "、".join(taboos) if taboos else "暂未识别到明确雷区",
            "stage": "熟悉期",
            "my_style": "（Mock 模式无法推断你的说话风格，接入真实模型后会自动总结。）",
        }
