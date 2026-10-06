"""参谋引擎接口：分析 / 建议 / 推演。

注意 `persist` 参数：默认 True，表示"这是对方刚发来的新消息，存进记忆"。
如果你只是想拿历史里的某句话反复试效果，传 persist=false，
就不会往库里塞重复消息。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..context import get_ctx
from ..engine import pipeline, simulator
from ..engine.context import build_context
from ..schemas import SimTree, SuggestionBundle

log = logging.getLogger("wingman.api.engine")
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


@router.post("/persons/{person_id}/suggest", response_model=SuggestionBundle)
async def suggest_for_person(
    person_id: str, payload: dict[str, Any] = Body(default={})
) -> SuggestionBundle:
    """以**对象**为单位跑一次分析（需求 8：先选对象，再勾选一个或多个渠道）。

    不传 `chat_ids` 时用这个对象名下的全部渠道；传了就只用在里面的那些。
    第一个被勾选的是主渠道 —— 你贴的那句话会记到它名下，检索也以它为主，
    但事实、摘要、近期消息来自全部勾选的渠道。
    """
    ctx = get_ctx()
    person = ctx.store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail=f"对象不存在：{person_id}")

    raw = payload.get("chat_ids")
    ids: list[str] = []
    if isinstance(raw, (list, tuple)):
        ids = [str(c) for c in raw if c]
    if not ids:
        detail = ctx.store.person_detail(person_id)
        ids = [c.chat_id for c in (detail.channels if detail else [])]
    if not ids:
        raise HTTPException(
            status_code=400,
            detail=f"「{person.name}」名下还没有绑定任何渠道，先去「采集」页导入或采集一段记录。",
        )

    for cid in ids:
        if ctx.store.get_chat(cid) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在：{cid}")

    message = str(payload.get("peer_message") or "").strip()
    if not message:
        for cid in ids:
            last = ctx.store.last_peer_message(cid)
            if last:
                message = str(last.get("text") or "").strip()
                break
    if not message:
        raise HTTPException(
            status_code=400,
            detail="没有提供 peer_message，且这些渠道里还没有对方的消息可分析。",
        )

    try:
        return await pipeline.run_analysis(
            ctx, ids, message,
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
            ctx, pack, option_text,
            option_id=str(payload.get("option_id") or ""),
            run_id=int(payload.get("run_id") or 0),
        )
    except Exception as exc:
        log.exception("推演失败")
        raise HTTPException(status_code=502, detail=f"推演失败：{exc}") from exc


# ================================================================ 历史输出（需求 11）


@router.get("/history/runs/{run_id}")
async def get_history_run(run_id: int) -> dict[str, Any]:
    """取一次指挥台运行的完整内容（含分析、策略、候选与推演），供回看 / 复用。

    里面是模型生成的回复原文，属隐私数据 —— 只在本机，不额外渲染。
    """
    run = await asyncio.to_thread(get_ctx().store.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这次输出：{run_id}")
    return run.model_dump()


@router.delete("/history/runs/{run_id}")
async def delete_history_run(run_id: int) -> dict[str, Any]:
    removed = await asyncio.to_thread(get_ctx().store.delete_run, run_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"没有这次输出：{run_id}")
    return {"ok": True, "deleted": removed}
