"""流水线编排：把 分析 → 策略 → 建议 串起来，并记录每步耗时。

耗时记录（`trace`）不是摆设。接入真模型后，你会想知道
「是检索慢还是模型慢」，这个字段直接告诉你该优化哪一步。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from ..schemas import Msg, SuggestionBundle
from . import analyzer, planner, suggestor
from .context import build_context

log = logging.getLogger("chatwing.engine.pipeline")


def store_peer_message(store: Any, chat_id: str, text: str) -> int | None:
    """把对方的新消息落库。如果和上一条完全相同就跳过 —— 避免反复分析造成重复。"""
    text = (text or "").strip()
    if not text:
        return None
    last = store.last_peer_message(chat_id)
    if last and str(last.get("text") or "").strip() == text:
        return int(last["id"])
    chat = store.get_chat(chat_id)
    if chat is None:
        return None
    msg = Msg(
        chat_id=chat_id,
        platform=chat.platform,
        sender=chat.peer_name or "对方",
        role="peer",
        ts=datetime.now(),
        text=text,
    )
    store.insert_messages([msg])
    last = store.last_peer_message(chat_id)
    return int(last["id"]) if last else None


async def run_analysis(
    ctx: Any,
    chat_id: str,
    peer_message: str,
    *,
    persist: bool = True,
    top_k: int = 30,
) -> SuggestionBundle:
    t0 = time.perf_counter()
    trace: dict[str, Any] = {}

    if persist:
        mid = store_peer_message(ctx.store, chat_id, peer_message)
        trace["stored_message_id"] = mid

    t = time.perf_counter()
    pack = await build_context(ctx, chat_id, peer_message, top_k=top_k)
    trace["context_ms"] = round((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    analysis = await analyzer.analyze(ctx, pack)
    trace["analyze_ms"] = round((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    strategy = await planner.plan(ctx, pack, analysis)
    trace["strategy_ms"] = round((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    options, warnings = await suggestor.suggest(ctx, pack, analysis, strategy)
    trace["suggest_ms"] = round((time.perf_counter() - t) * 1000)

    trace["total_ms"] = round((time.perf_counter() - t0) * 1000)
    trace["llm"] = getattr(ctx.llm, "name", "unknown")
    trace["retrieved"] = len(pack.retrieved)
    trace["facts"] = len(pack.facts)

    return SuggestionBundle(
        analysis=analysis,
        strategy=strategy,
        options=options,
        context_used=pack.context_used,
        persona=pack.persona,
        warnings=pack.warnings + warnings,
        trace=trace,
    )
