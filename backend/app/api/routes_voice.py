"""语音接口：设备枚举、启停采集、SSE 实时字幕。

还有一个 `POST /voice/ingest` —— 手动把一段文字当作"听到的话"注入事件流。
没有麦克风、不想装 soundcard、或者只是想验证前端字幕渲染时，用它最方便。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..asr.capture import VoiceSession, get_session, list_audio_devices, set_session
from ..bus import bus
from ..context import get_ctx
from ..schemas import AudioDevice, TranscriptSegment, VoiceStatus

log = logging.getLogger("wingman.api.voice")
router = APIRouter(prefix="/api/voice", tags=["voice"])

_seq = 0


@router.get("/devices", response_model=list[AudioDevice])
async def devices() -> list[AudioDevice]:
    return await asyncio.to_thread(list_audio_devices)


@router.get("/status", response_model=VoiceStatus)
async def status() -> VoiceStatus:
    session = get_session()
    if session is None:
        return VoiceStatus(running=False, engine=get_ctx().asr.name)
    return session.status()


@router.post("/start", response_model=VoiceStatus)
async def start(payload: dict[str, Any] = Body(default={})) -> VoiceStatus:
    ctx = get_ctx()
    chat_id = str(payload.get("chat_id") or "").strip() or None
    if chat_id and ctx.store.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{chat_id}")

    existing = get_session()
    if existing and existing.running:
        existing.stop()

    session = VoiceSession(
        ctx,
        chat_id=chat_id,
        peer_device=str(payload.get("peer_device") or "auto"),
        me_device=str(payload.get("me_device") or "auto"),
        enable_peer=bool(payload.get("enable_peer", True)),
        enable_me=bool(payload.get("enable_me", True)),
    )
    try:
        session.start(asyncio.get_running_loop())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    set_session(session)
    return session.status()


@router.post("/stop", response_model=VoiceStatus)
async def stop() -> VoiceStatus:
    session = get_session()
    if session is None:
        return VoiceStatus(running=False, engine=get_ctx().asr.name)
    await asyncio.to_thread(session.stop)
    st = session.status()
    set_session(None)
    return st


@router.post("/ingest")
async def ingest(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """手动注入一条"听到的话"，走完整条转写链路（含落库与 SSE 推送）。"""
    global _seq
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空。")
    channel = str(payload.get("channel") or "peer")
    if channel not in ("peer", "me"):
        channel = "peer"
    chat_id = str(payload.get("chat_id") or "").strip() or None
    if chat_id and get_ctx().store.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{chat_id}")

    _seq += 1
    seg = TranscriptSegment(
        seq=_seq, channel=channel, text=text,
        start_ms=0, end_ms=0,
        ts=datetime.now().isoformat(timespec="seconds"),
        duration_ms=0, rms=0.0,
    )
    if chat_id:
        ctx = get_ctx()
        ctx.store.add_voice_segment(chat_id, channel, seg.ts, text, 0, 0, 0)
    bus.publish({"type": "transcript", **seg.model_dump()})
    return {"ok": True, "segment": seg.model_dump()}


@router.get("/recent")
async def recent(limit: int = 50) -> dict[str, Any]:
    return {"events": bus.recent(limit)}


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE。前端用 EventSource 接，断线会自动重连。"""
    queue = bus.subscribe(replay=True)

    async def gen():
        # 先推一条握手事件，让前端立刻知道连上了
        yield f"data: {json.dumps({'type': 'hello', 'subs': bus.subscriber_count}, ensure_ascii=False)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                payload = json.dumps(evt, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("SSE 流结束：%s", exc)
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
