"""数据相关接口：导入、会话、记忆、画像。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from ..adapters import registry
from ..context import get_ctx
from ..memory import profiler
from ..schemas import (
    ChatInfo, Fact, ImportPreview, ImportResult, Persona, ProfileBuildResult,
)

log = logging.getLogger("wingman.api.data")
router = APIRouter(prefix="/api", tags=["data"])


def _parse_options(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _require_chat(chat_id: str) -> ChatInfo:
    info = get_ctx().store.get_chat(chat_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{chat_id}")
    return info


# ================================================================ 导入


@router.post("/import/preview", response_model=ImportPreview)
async def import_preview(
    file: UploadFile = File(...),
    adapter: str | None = Form(default=None),
    options: str | None = Form(default=None),
    sample_size: int = Form(default=30),
) -> ImportPreview:
    """只解析不入库，让用户先确认适配器选得对不对。"""
    suffix = Path(file.filename or "upload.txt").suffix or ".txt"
    tmp = Path(tempfile.mkdtemp(prefix="wingman_preview_")) / f"upload{suffix}"
    try:
        tmp.write_bytes(await file.read())
        return await asyncio.to_thread(
            registry.preview_file, tmp,
            adapter_name=adapter, options=_parse_options(options),
            sample_size=max(5, min(100, sample_size)),
        )
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


@router.post("/import", response_model=ImportResult)
async def import_records(
    file: UploadFile = File(...),
    chat_name: str | None = Form(default=None),
    adapter: str | None = Form(default=None),
    options: str | None = Form(default=None),
) -> ImportResult:
    ctx = get_ctx()
    suffix = Path(file.filename or "upload.txt").suffix or ".txt"
    tmp = Path(tempfile.mkdtemp(prefix="wingman_import_")) / f"upload{suffix}"
    try:
        tmp.write_bytes(await file.read())
        result = await asyncio.to_thread(
            registry.import_file, ctx.store, tmp,
            chat_name=chat_name, adapter_name=adapter,
            options=_parse_options(options),
        )
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    # 导入完顺手建索引（失败不影响导入结果）
    if result.inserted:
        try:
            n = await profiler.build_index(ctx, result.chat_id)
            log.info("导入后自动建索引 %d 条", n)
        except Exception as exc:
            result.warnings.append(f"自动建立向量索引失败（不影响导入）：{exc}")
    return result


@router.post("/import/text", response_model=ImportResult)
async def import_pasted_text(payload: dict[str, Any] = Body(...)) -> ImportResult:
    """直接粘贴文本导入，方便快速试跑。"""
    ctx = get_ctx()
    text = str(payload.get("text") or "")
    if len(text.strip()) < 10:
        raise HTTPException(status_code=400, detail="文本太短，至少 10 个字符。")
    name = str(payload.get("chat_name") or "").strip() or "粘贴导入"
    result = await asyncio.to_thread(
        registry.import_text, ctx.store, text,
        chat_name=name,
        adapter_name=str(payload.get("adapter") or "generic"),
        options={"order": payload.get("order") or "ts_first"},
    )
    if result.inserted:
        try:
            await profiler.build_index(ctx, result.chat_id)
        except Exception as exc:
            result.warnings.append(f"建立索引失败：{exc}")
    return result


# ================================================================ 会话


@router.get("/chats", response_model=list[ChatInfo])
async def list_chats() -> list[ChatInfo]:
    return await asyncio.to_thread(get_ctx().store.list_chats)


@router.get("/chats/{chat_id}", response_model=ChatInfo)
async def get_chat(chat_id: str) -> ChatInfo:
    return await asyncio.to_thread(_require_chat, chat_id)


@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str) -> dict[str, Any]:
    _require_chat(chat_id)
    await asyncio.to_thread(get_ctx().store.delete_chat, chat_id)
    return {"ok": True}


@router.patch("/chats/{chat_id}")
async def patch_chat(chat_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    info = _require_chat(chat_id)
    store = get_ctx().store
    name = str(payload.get("name") or info.name)
    await asyncio.to_thread(
        store.rename_chat, chat_id, name,
        str(payload.get("peer_name") or ""),
        str(payload.get("me_name") or ""),
    )
    changed = 0
    if payload.get("me_names"):
        names = payload["me_names"]
        if isinstance(names, str):
            names = [n.strip() for n in names.replace("，", ",").split(",") if n.strip()]
        changed = await asyncio.to_thread(store.set_roles, chat_id, names)
    return {"ok": True, "roles_changed": changed}


@router.get("/chats/{chat_id}/messages")
async def get_messages(chat_id: str, limit: int = 200, before_id: int | None = None) -> dict[str, Any]:
    _require_chat(chat_id)
    rows = await asyncio.to_thread(
        get_ctx().store.list_messages, chat_id, min(limit, 1000), before_id
    )
    return {"messages": rows, "count": len(rows)}


@router.get("/chats/{chat_id}/export")
async def export_chat(chat_id: str) -> dict[str, Any]:
    _require_chat(chat_id)
    return await asyncio.to_thread(get_ctx().store.export_chat_json, chat_id)


# ================================================================ 记忆


@router.post("/chats/{chat_id}/index")
async def build_index(chat_id: str) -> dict[str, Any]:
    ctx = get_ctx()
    _require_chat(chat_id)
    n = await profiler.build_index(ctx, chat_id)
    from ..memory.retriever import HybridRetriever

    stats = HybridRetriever(ctx.store, ctx.embedder).stats(chat_id)
    return {"added": n, **stats}


@router.get("/chats/{chat_id}/facts", response_model=list[Fact])
async def list_facts(chat_id: str, subject: str | None = None) -> list[Fact]:
    _require_chat(chat_id)
    return await asyncio.to_thread(get_ctx().store.list_facts, chat_id, subject)


@router.post("/chats/{chat_id}/facts", response_model=Fact)
async def add_fact(chat_id: str, payload: dict[str, Any] = Body(...)) -> Fact:
    _require_chat(chat_id)
    fact = Fact(
        chat_id=chat_id,
        subject=str(payload.get("subject") or "peer"),
        key=str(payload.get("key") or "").strip(),
        value=str(payload.get("value") or "").strip(),
        confidence=float(payload.get("confidence") or 0.9),
        evidence=str(payload.get("evidence") or "手动添加"),
    )
    if not fact.key or not fact.value:
        raise HTTPException(status_code=400, detail="key 和 value 不能为空。")
    await asyncio.to_thread(get_ctx().store.upsert_fact, chat_id, fact)
    return fact


@router.delete("/chats/{chat_id}/facts/{fact_id}")
async def delete_fact(chat_id: str, fact_id: int) -> dict[str, Any]:
    _require_chat(chat_id)
    await asyncio.to_thread(get_ctx().store.delete_fact, fact_id)
    return {"ok": True}


@router.get("/chats/{chat_id}/persona", response_model=Persona)
async def get_persona(chat_id: str) -> Persona:
    _require_chat(chat_id)
    return await asyncio.to_thread(get_ctx().store.get_persona, chat_id)


@router.put("/chats/{chat_id}/persona", response_model=Persona)
async def put_persona(chat_id: str, payload: dict[str, Any] = Body(...)) -> Persona:
    _require_chat(chat_id)
    allowed = {k: str(v) for k, v in payload.items()
               if k in ("goal", "my_style", "peer_profile", "taboos", "stage")}
    return await asyncio.to_thread(get_ctx().store.save_persona, chat_id, allowed)


@router.post("/chats/{chat_id}/profile", response_model=ProfileBuildResult)
async def build_profile(chat_id: str) -> ProfileBuildResult:
    """事实抽取 + 画像综合，是「记忆」页的主按钮。"""
    ctx = get_ctx()
    _require_chat(chat_id)
    try:
        return await profiler.build_profile(ctx, chat_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/chats/{chat_id}/voice")
async def list_voice(chat_id: str, limit: int = 200) -> dict[str, Any]:
    _require_chat(chat_id)
    rows = await asyncio.to_thread(get_ctx().store.list_voice_segments, chat_id, limit)
    return {"segments": rows, "count": len(rows)}
