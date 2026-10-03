"""参谋引擎接口：分析 / 建议 / 推演。

注意 `persist` 参数：默认 True，表示"这是对方刚发来的新消息，存进记忆"。
如果你只是想拿历史里的某句话反复试效果，传 persist=false，
就不会往库里塞重复消息。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..context import get_ctx
from ..engine import pipeline, simulator
from ..engine.context import build_context
from ..schemas import SimTree, SuggestionBundle

log = logging.getLogger("chatwing.api.engine")
router = APIRouter(prefix="/api", tags=["engine"])


def _require_chat(chat_id: str) -> None:
    if get_ctx().store.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{chat_id}")


def _resolve_message(chat_id: str, payload: dict[str, Any]) -> str:
    msg = str(payload.get("peer_message") or "").strip()
    if msg:
        return msg
    last = get_ctx().store.last_peer_message(chat_id)
    if last:
        return str(last.get("text") or "").strip()
    raise HTTPException(
        status_code=400,
        detail="没有提供 peer_message，且这条会话里还没有对方的消息可分析。",
    )


@router.post("/chats/{chat_id}/suggest", response_model=SuggestionBundle)
async def suggest(chat_id: str, payload: dict[str, Any] = Body(default={})) -> SuggestionBundle:
    """一次跑完 分析 → 策略 → 候选回复。前端指挥台的主按钮打的就是这个。"""
    ctx = get_ctx()
    _require_chat(chat_id)
    message = _resolve_message(chat_id, payload)
    try:
        return await pipeline.run_analysis(
            ctx, chat_id, message,
            persist=bool(payload.get("persist", True)),
            top_k=max(5, min(80, int(payload.get("top_k") or 30))),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("生成建议失败")
        raise HTTPException(status_code=502, detail=f"生成建议失败：{exc}") from exc


@router.post("/chats/{chat_id}/analyze")
async def analyze_only(chat_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """只要对方状态分析，不要回复建议。用于快速看一条消息。"""
    ctx = get_ctx()
    _require_chat(chat_id)
    message = _resolve_message(chat_id, payload)
    from ..engine import analyzer

    pack = await build_context(ctx, chat_id, message, top_k=int(payload.get("top_k") or 20))
    result = await analyzer.analyze(ctx, pack)
    return {
        "analysis": result.model_dump(),
        "context_used": pack.context_used,
        "warnings": pack.warnings,
    }


@router.post("/chats/{chat_id}/simulate", response_model=SimTree)
async def simulate(chat_id: str, payload: dict[str, Any] = Body(...)) -> SimTree:
    """对某一条候选回复做 3 轮走向推演。"""
    ctx = get_ctx()
    _require_chat(chat_id)
    option_text = str(payload.get("option_text") or "").strip()
    if not option_text:
        raise HTTPException(status_code=400, detail="缺少 option_text。")
    message = str(payload.get("peer_message") or "").strip() or _resolve_message(chat_id, payload)

    # 推演不需要检索，省掉一次向量召回
    pack = await build_context(ctx, chat_id, message, use_retrieval=False)
    try:
        return await simulator.simulate(
            ctx, pack, option_text, option_id=str(payload.get("option_id") or "")
        )
    except Exception as exc:
        log.exception("推演失败")
        raise HTTPException(status_code=502, detail=f"推演失败：{exc}") from exc
