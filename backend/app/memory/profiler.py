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

from ..schemas import Fact, Msg, Persona, ProfileBuildResult
from .retriever import HybridRetriever

log = logging.getLogger("chatwing.memory.profiler")

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
