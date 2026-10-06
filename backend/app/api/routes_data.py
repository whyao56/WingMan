"""数据相关接口：导入、会话、记忆、画像。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..adapters import registry
from ..context import get_ctx
from ..memory import profiler
from ..schemas import (
    ChatInfo, Fact, ImportPreview, ImportResult, Persona, ProfileBuildResult,
)

log = logging.getLogger("wingman.api.data")
router = APIRouter(prefix="/api", tags=["data"])

_MSG_ROLES = ("me", "peer", "system")
_BULK_ACTIONS = ("delete", "set_role", "set_ts", "set_sender")
_BULK_LABELS = {"delete": "删除", "set_role": "改角色", "set_ts": "改时间", "set_sender": "改发送者"}


def _iso_ts(value: Any) -> str:
    """把接口传来的时间归一成 ISO 字符串；不是 ISO 就 422，而不是写进库变成坏时间。"""
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"时间不是 ISO 格式：{value}") from exc


def _structured(out: dict[str, Any]) -> Any:
    """把 store 的结构化错误翻成带状态码的响应（撞键 409 / 不存在 404），不 500。"""
    if out.get("ok"):
        return out
    code = 404 if out.get("error") == "not_found" else 409
    return JSONResponse(status_code=code, content=out)


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
    platform: str | None = Form(default=None),
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
            platform=(platform or "").strip() or None,
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
        await asyncio.to_thread(_log_import_activity, ctx.store, result)
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
        # 默认 `auto`：粘贴框上写的示范格式是行式的，`generic` 读不了它。
        adapter_name=str(payload.get("adapter") or "auto"),
        platform=str(payload.get("platform") or "").strip() or None,
        options={"order": payload.get("order") or "ts_first"},
    )
    if result.inserted:
        try:
            await profiler.build_index(ctx, result.chat_id)
        except Exception as exc:
            result.warnings.append(f"建立索引失败：{exc}")
        await asyncio.to_thread(_log_import_activity, ctx.store, result)
    return result


def _log_import_activity(store, result: ImportResult) -> None:
    """导入成功留一条痕迹，供对象详情「历史」回看。失败不影响导入本身。"""
    try:
        info = store.get_chat(result.chat_id)
        store.log_activity(
            "import", person_id=(info.person_id if info else ""), chat_id=result.chat_id,
            summary=f"导入 {result.inserted} 条", detail=f"适配器 {result.adapter}",
        )
    except Exception as exc:  # pragma: no cover - 痕迹写不进去不该挡住导入
        log.warning("记录导入痕迹失败：%s", exc)


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
    """改会话信息：名称 / 双方称呼，以及**平台 / 渠道**（需求 7）。

    `platform` 变更时 `channel` 由 store 按映射表重算 —— 只在显式传 channel 时才覆盖。
    无论如何**不会改 chat_id**：它是幂等键与采集游标的依据。
    """
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
    if payload.get("platform") or payload.get("channel"):
        updated = await asyncio.to_thread(
            store.set_chat_platform, chat_id,
            str(payload.get("platform") or info.platform),
            str(payload.get("channel") or ""),
        )
        if updated is not None:
            info = updated
    return {"ok": True, "roles_changed": changed, "chat": info.model_dump()}


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


@router.post("/chats/{chat_id}/messages")
async def add_message(chat_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """手动往这条会话里加一条消息（聊天记录的「增加」）。

    撞幂等键返回 409 结构化错误，不 500 ——「你要加的这条已经在了」不是服务出错。
    """
    _require_chat(chat_id)
    sender = str(payload.get("sender") or "").strip()
    text = str(payload.get("text") or "").strip()
    role = str(payload.get("role") or "")
    if not sender:
        raise HTTPException(status_code=422, detail="sender 不能为空。")
    if not text:
        raise HTTPException(status_code=422, detail="text 不能为空。")
    if role not in _MSG_ROLES:
        raise HTTPException(status_code=422, detail=f"role 只能是 {_MSG_ROLES}。")
    ts = _iso_ts(payload["ts"]) if payload.get("ts") else datetime.now().isoformat(timespec="seconds")
    out = await asyncio.to_thread(
        get_ctx().store.insert_manual_message, chat_id,
        sender=sender, role=role, ts=ts, text=text,
        msg_type=str(payload.get("msg_type") or "text"),
        ts_source=str(payload.get("ts_source") or "manual"),
    )
    return _structured(out)


# ================================================================ 消息编辑（二次编辑 / 批量）


@router.patch("/messages/{msg_id}")
async def patch_message(msg_id: int, payload: dict[str, Any] = Body(...)) -> Any:
    """改一条消息。撞 `UNIQUE(chat_id, sender, ts, text)` 返回 409 结构化错误。"""
    patch: dict[str, Any] = {}
    if payload.get("text") is not None:
        patch["text"] = str(payload["text"])
    if payload.get("sender") is not None:
        sender = str(payload["sender"]).strip()
        if not sender:
            raise HTTPException(status_code=422, detail="发送者不能为空。")
        patch["sender"] = sender
    if payload.get("role") is not None:
        role = str(payload["role"])
        if role not in _MSG_ROLES:
            raise HTTPException(status_code=422, detail=f"role 只能是 {_MSG_ROLES}。")
        patch["role"] = role
    if payload.get("ts") is not None:
        patch["ts"] = _iso_ts(payload["ts"])
    if payload.get("ts_source") is not None:
        patch["ts_source"] = str(payload["ts_source"])
    if not patch:
        raise HTTPException(status_code=422, detail="没有要修改的字段。")
    out = await asyncio.to_thread(get_ctx().store.update_message, msg_id, patch)
    return _structured(out)


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: int) -> dict[str, Any]:
    """删一条消息。已经不在的返回 404，而不是假装成功。"""
    removed = await asyncio.to_thread(get_ctx().store.delete_messages, [msg_id])
    if not removed:
        raise HTTPException(status_code=404, detail=f"消息不存在：{msg_id}")
    return {"ok": True, "deleted": removed}


@router.post("/messages/bulk")
async def bulk_edit_messages(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """批量操作：`delete` / `set_role` / `set_ts` / `set_sender`。

    返回 `{changed, skipped, errors[]}` —— 前端据此提示「改了 N 条、跳过 M 条」。
    `skipped` 是请求里**本来就不存在**的 id（不是错误）；`errors` 是逐条失败的原因
    （例如改成和另一条完全重复）。`set_ts` 会顺带把 `ts_source` 标成 `manual`，
    因为时间是人工给的，不该继续冒充原始时间。
    """
    store = get_ctx().store
    raw_ids = payload.get("ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [x for x in raw_ids.replace("，", ",").split(",") if x.strip()]
    ids: list[int] = []
    for item in raw_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    action = str(payload.get("action") or "")
    if action not in _BULK_ACTIONS:
        raise HTTPException(status_code=422, detail=f"action 只能是 {_BULK_ACTIONS}。")

    field: str = ""
    value: Any = None
    extra: dict[str, Any] = {}
    if action == "set_role":
        value = str(payload.get("value") or "")
        if value not in _MSG_ROLES:
            raise HTTPException(status_code=422, detail=f"role 只能是 {_MSG_ROLES}。")
        field = "role"
    elif action == "set_ts":
        field, value = "ts", _iso_ts(payload.get("value"))
        extra = {"ts_source": "manual"}
    elif action == "set_sender":
        value = str(payload.get("value") or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail="发送者不能为空。")
        field = "sender"

    if not ids:
        return {"changed": 0, "skipped": 0, "errors": []}

    existing = {r["id"] for r in await asyncio.to_thread(store.messages_by_ids, ids)}

    def run() -> dict[str, Any]:
        changed = 0
        errors: list[dict[str, Any]] = []
        if action == "delete":
            changed = store.delete_messages(sorted(existing))
        else:
            for mid in ids:
                if mid not in existing:
                    continue
                patch = {field: value, **extra}
                out = store.update_message(mid, patch)
                if out.get("ok"):
                    changed += 1
                else:
                    errors.append({
                        "id": mid, "error": out.get("error", "error"),
                        "detail": out.get("detail", ""),
                    })
        skipped = len(ids) - len(existing)
        if changed:
            sample = store.messages_by_ids(sorted(existing)[:1])
            chat_id = sample[0]["chat_id"] if sample else ""
            chat = store.get_chat(chat_id) if chat_id else None
            store.log_activity(
                "edit", person_id=(chat.person_id if chat else ""), chat_id=chat_id,
                summary=f"批量{_BULK_LABELS[action]} {changed} 条",
                detail=f"请求 {len(ids)} 条，跳过 {skipped} 条，失败 {len(errors)} 条",
            )
        return {"changed": changed, "skipped": skipped, "errors": errors}

    return await asyncio.to_thread(run)


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
