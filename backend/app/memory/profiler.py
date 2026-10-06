"""人物画像构建：事实抽取 → 画像综合。

两段式的原因：
- **事实层**要的是**准**，所以走小批量、强约束、必须带 evidence 的抽取；
- **画像层**要的是**全**，所以吃全部事实 + 抽样对话，让模型做一次综合。

分开做还有一个好处：事实可以人工校对（前端可删改），
画像可以随时用最新事实重建，两者互不污染。
"""

from __future__ import annotations

import logging
from typing import Any

from ..schemas import (
    Fact, Msg, Persona, PersonProfileBuildResult, ProfileBuildResult,
)
from .retriever import HybridRetriever

log = logging.getLogger("wingman.memory.profiler")

FACT_BATCH_LINES = 60
FACT_MAX_LINES = 600
PROFILE_SAMPLE = 80


def _fmt_lines(rows: list[dict[str, Any]], me_name: str = "我") -> list[str]:
    out: list[str] = []
    for r in rows:
        text = str(r.get("text") or "").replace("\n", " ").strip()
        if not text or len(text) < 2:
            continue
        if str(r.get("msg_type") or "text") != "text":
            continue
        who = me_name if r.get("role") == "me" else str(r.get("sender") or "")
        out.append(f"#{r['id']}|{who}|{text[:220]}")
    return out


def _sample_evenly(lines: list[str], limit: int) -> list[str]:
    """均匀抽样而不是取前 N 条 —— 否则早期聊天会被完全忽略。"""
    if len(lines) <= limit:
        return lines
    step = len(lines) / limit
    return [lines[int(i * step)] for i in range(limit)]


async def build_index(ctx: Any, chat_id: str) -> int:
    """为会话建立/补齐向量索引。"""
    retriever = HybridRetriever(ctx.store, ctx.embedder)
    return await retriever.index_chat(chat_id)


async def extract_facts(ctx: Any, chat_id: str, max_lines: int = FACT_MAX_LINES) -> list[Fact]:
    from ..engine import prompts

    chat = ctx.store.get_chat(chat_id)
    me_name = (chat.me_name if chat else "") or "我"
    rows = ctx.store.all_messages(chat_id)
    lines = _sample_evenly(_fmt_lines(rows, me_name), max_lines)
    if not lines:
        return []

    collected: dict[tuple[str, str], Fact] = {}
    batches = [lines[i:i + FACT_BATCH_LINES] for i in range(0, len(lines), FACT_BATCH_LINES)]

    for idx, batch in enumerate(batches):
        user = prompts.facts_user("\n".join(batch), me_name)
        try:
            data = await ctx.llm.complete_json(
                prompts.FACTS_SYSTEM, user, schema_hint=prompts.FACTS_SCHEMA, temperature=0.2
            )
        except Exception as exc:
            log.warning("第 %d 批事实抽取失败：%s", idx + 1, exc)
            continue
        for item in (data.get("facts") or []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            value = str(item.get("value") or "").strip()
            if not key or not value or len(value) > 60:
                continue
            subject = str(item.get("subject") or "peer").strip()
            if subject not in ("peer", "me", "relationship"):
                subject = "peer"
            k = (subject, f"{key}={value}")
            conf = item.get("confidence", 0.6)
            try:
                conf = max(0.0, min(1.0, float(conf)))
            except (TypeError, ValueError):
                conf = 0.6
            if k in collected:
                # 同一事实反复出现 → 提高置信度，并合并证据
                prev = collected[k]
                prev.confidence = min(1.0, prev.confidence + 0.1)
                ev = str(item.get("evidence") or "").strip()
                if ev and ev not in prev.evidence:
                    prev.evidence = ",".join(filter(None, [prev.evidence, ev]))[:200]
                continue
            collected[k] = Fact(
                chat_id=chat_id, subject=subject, key=key, value=value,
                confidence=conf, evidence=str(item.get("evidence") or "")[:200],
            )

    facts = list(collected.values())
    ctx.store.replace_facts(chat_id, "peer", [f for f in facts if f.subject == "peer"])
    ctx.store.replace_facts(chat_id, "me", [f for f in facts if f.subject == "me"])
    ctx.store.replace_facts(chat_id, "relationship", [f for f in facts if f.subject == "relationship"])
    return ctx.store.list_facts(chat_id)


async def build_profile(ctx: Any, chat_id: str) -> ProfileBuildResult:
    """抽取事实 + 综合画像，并写回 personas 表。"""
    from ..engine import prompts

    warnings: list[str] = []
    chat = ctx.store.get_chat(chat_id)
    if chat is None:
        raise KeyError(chat_id)
    me_name = chat.me_name or "我"

    if chat.message_count == 0:
        return ProfileBuildResult(
            facts_extracted=0, facts_total=0, persona=ctx.store.get_persona(chat_id),
            warnings=["这条会话还没有任何消息，先导入聊天记录。"],
        )

    before = len(ctx.store.list_facts(chat_id))
    await extract_facts(ctx, chat_id)
    facts = ctx.store.list_facts(chat_id)
    if not facts:
        warnings.append("没有抽取到结构化事实，画像将主要依赖对话抽样。")

    rows = ctx.store.all_messages(chat_id)
    lines = _sample_evenly(_fmt_lines(rows, me_name), PROFILE_SAMPLE)
    facts_text = "\n".join(
        f"[{f.subject}] {f.key}: {f.value}（置信度 {f.confidence:.1f}）" for f in facts
    ) or "（无）"

    cur = ctx.store.get_persona(chat_id)
    user = prompts.profile_user(facts_text, "\n".join(lines), cur.goal, me_name)

    try:
        data = await ctx.llm.complete_json(
            prompts.PROFILE_SYSTEM, user, schema_hint=prompts.PROFILE_SCHEMA, temperature=0.4
        )
    except Exception as exc:
        warnings.append(f"画像综合失败：{exc}")
        return ProfileBuildResult(
            facts_extracted=len(facts) - before, facts_total=len(facts),
            persona=cur, warnings=warnings,
        )

    patch = {
        "peer_profile": str(data.get("peer_profile") or cur.peer_profile or ""),
        "taboos": str(data.get("taboos") or cur.taboos or ""),
        "stage": str(data.get("stage") or cur.stage or ""),
        "my_style": str(data.get("my_style") or cur.my_style or ""),
    }
    persona = ctx.store.save_persona(chat_id, patch)
    return ProfileBuildResult(
        facts_extracted=max(0, len(facts) - before),
        facts_total=len(facts),
        persona=persona,
        warnings=warnings,
    )


# 用户手写的字段 vs 模型产出的字段。
# 这个划分是**产品语义**，不是实现细节：`goal/stage/taboos/my_style` 是用户对自己
# 关系的判断，模型只能在它们之上补充；`peer_profile` 本来就是「模型生成、可手改」，
# 所以默认归模型。分错了的后果是用户不敢用这个按钮 —— 谁知道它会不会把手打的
# 「别叫她宝贝」冲掉。
_USER_FIELDS = ("goal", "stage", "taboos", "my_style")
_MODEL_FIELDS = ("peer_profile",)
_PERSONA_FIELDS = (*_USER_FIELDS, *_MODEL_FIELDS)


async def build_person_profile(
    ctx: Any, person_id: str, *, overwrite: bool = False
) -> PersonProfileBuildResult:
    """把一个人名下**全部渠道**合起来，整理出对象级画像。

    和 `build_profile` 的三处关键差别：

    1. **跨渠道**：输入是这个人所有渠道的消息，每条标着来自哪个渠道。
       跨渠道才看得到「在两个地方说话方式不一样」这种单渠道永远发现不了的事。
    2. **不吃掉用户的手写稿**：默认只填空。用户已经写过的 `goal/stage/taboos/my_style`
       会原样保留，并且**照样喂给模型**当依据。`overwrite=True` 才允许改写。
    3. **先建索引**：顺手把每个渠道的向量索引补齐，这样刚导入完就能直接分析。

    返回里明确列出哪些字段「保留」、哪些「更新」，让用户看得见它做了什么。
    """
    from ..engine import prompts

    person = ctx.store.get_person(person_id)
    if person is None:
        raise KeyError(f"没有这个人：{person_id}")

    warnings: list[str] = []
    detail = ctx.store.person_detail(person_id)
    channels = list(detail.channels) if detail else []
    chat_ids = [c.chat_id for c in channels]
    if not chat_ids:
        return PersonProfileBuildResult(
            persona=ctx.store.get_person_persona(person_id),
            warnings=["这个人名下还没有任何渠道。先「导入」或「采集」一段聊天记录，再让 AI 整理。"],
        )

    # 「这次新抽到几条」要在抽取**之前**取基线，否则永远是 0。
    before_ids = {f.id for f in ctx.store.list_facts_for_person(person_id)}

    # 逐渠道建索引 + 抽事实。一个渠道失败不影响其它渠道 —— 其余的证据照样有用。
    total_msgs = 0
    for ch in channels:
        total_msgs += ch.message_count
        try:
            await build_index(ctx, ch.chat_id)
        except Exception as exc:  # 索引失败不致命，检索退化成关键词
            warnings.append(f"「{ch.name or ch.chat_id}」建索引失败：{exc}")
        try:
            await extract_facts(ctx, ch.chat_id)
        except Exception as exc:
            warnings.append(f"「{ch.name or ch.chat_id}」抽事实失败：{exc}")

    if total_msgs == 0:
        return PersonProfileBuildResult(
            persona=ctx.store.get_person_persona(person_id),
            channels=chat_ids,
            warnings=["这些渠道里都还没有消息。"],
        )

    # 事实：对象视野（对象级 + 各渠道级），按 scope 标出来给模型看
    facts = ctx.store.list_facts_for_person(person_id)
    facts_text = "\n".join(
        f"[{'对象级' if f.scope == 'person' else '渠道级'}|{f.subject}] "
        f"{f.key}: {f.value}（置信度 {f.confidence:.1f}）"
        for f in facts
    ) or "（无）"

    # 对话抽样：每个渠道各自均匀抽，再拼起来。
    # 为什么不是「全部消息混在一起抽」——话多的渠道会把话少的整个盖掉，
    # 而「Ta 在 QQ 上很冷淡」这种结论恰恰只存在于那个话少的渠道里。
    per_chat = max(10, PROFILE_SAMPLE // max(1, len(channels)))
    sample: list[str] = []
    for ch in channels:
        rows = ctx.store.all_messages(ch.chat_id)
        me_name = ch.me_name or "我"
        lines = _fmt_lines(rows, me_name)
        label = ch.name or ch.chat_id
        sample.extend(f"[{label}|{ch.channel}] {ln}" for ln in _sample_evenly(lines, per_chat))
    cur = ctx.store.get_person_persona(person_id)
    user = prompts.person_profile_user(
        facts=facts_text,
        sample="\n".join(sample),
        goal=cur.goal, stage=cur.stage, taboos=cur.taboos,
        my_style=cur.my_style, peer_profile=cur.peer_profile,
        notes=person.notes,
    )

    try:
        data = await ctx.llm.complete_json(
            prompts.PERSON_PROFILE_SYSTEM, user,
            schema_hint=prompts.PERSON_PROFILE_SCHEMA, temperature=0.4,
        )
    except Exception as exc:
        return PersonProfileBuildResult(
            facts_total=len(facts), channels=chat_ids,
            persona=cur,
            warnings=[*warnings, f"画像整理失败：{exc}"],
        )

    # 合并策略：用户手写的那几项默认不动（只在他本来没写时填）；
    # `peer_profile` 本来就是模型产的，允许改写 —— 除非它一直是空的。
    #
    # `kept` 的判定**不看模型有没有给出新值**：只要用户写过、而这次是默认模式，
    # 就把这一项列进「保留」。理由是这个列表是给用户看的**保证**——
    # 「你手写的这四项，我一项都没动」。如果只在模型恰好也想改这一项时才列出来，
    # 用户看到的就是一份随模型心情浮动的清单，反而更不敢按这个按钮。
    patch: dict[str, str] = {}
    kept: list[str] = []
    updated: list[str] = []
    for field in _PERSONA_FIELDS:
        old = str(getattr(cur, field) or "").strip()
        new = str(data.get(field) or "").strip()
        if field in _USER_FIELDS and old and not overwrite:
            kept.append(field)
            continue
        if not new:
            continue
        patch[field] = new
        updated.append(field)

    persona = ctx.store.save_person_persona(person_id, patch) or cur
    after_ids = {f.id for f in ctx.store.list_facts_for_person(person_id)}
    if not facts:
        warnings.append("没有抽取到结构化事实，画像主要依赖对话抽样。")

    return PersonProfileBuildResult(
        facts_extracted=len(after_ids - before_ids),
        facts_total=len(after_ids),
        channels=chat_ids,
        persona=persona,
        kept=kept,
        updated=updated,
        warnings=[w for w in warnings if w],
    )


async def summarize_period(ctx: Any, chat_id: str, kind: str = "weekly") -> str:
    """把一个时间段的闲聊压成一段脉络，作为 L2 长期记忆。"""
    from ..engine import prompts

    chat = ctx.store.get_chat(chat_id)
    if chat is None:
        raise KeyError(chat_id)
    me_name = chat.me_name or "我"

    rows = ctx.store.all_messages(chat_id)
    if not rows:
        return ""
    # 取最后 200 条
    lines = _fmt_lines(rows[-200:], me_name)
    period = f"{str(rows[-1].get('ts'))[:10]}"
    user = prompts.summary_user("\n".join(lines), kind, me_name)
    try:
        data = await ctx.llm.complete_json(
            prompts.SUMMARY_SYSTEM, user, schema_hint=prompts.SUMMARY_SCHEMA, temperature=0.4
        )
    except Exception as exc:
        log.warning("生成摘要失败：%s", exc)
        return ""
    content = str(data.get("content") or "").strip()
    if not content:
        return ""
    from ..schemas import Summary

    ctx.store.add_summary(Summary(chat_id=chat_id, kind=kind, period=period, content=content))
    return content
