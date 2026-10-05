"""本地语音模型的下载与管理。

单独成一组路由，是因为它和「语音采集」是两件事：采集关心设备，
这里关心磁盘上的模型文件。混在一起会让 routes_voice 变得很难读。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ..asr import models as M

log = logging.getLogger("wingman.api.models")

router = APIRouter(prefix="/api/asr", tags=["asr-models"])


def _payload() -> dict[str, Any]:
    return {
        "library_available": M.library_available(),
        "models_root": str(M.models_root()),
        "items": M.catalog(),
        "progress": M.STATE.snapshot(),
        "mirror": M.MIRROR_ENDPOINT,
    }


@router.get("/models")
async def list_models() -> dict[str, Any]:
    """能下哪些、已经下了哪些。"""
    return await asyncio.to_thread(_payload)


@router.post("/models/download")
async def start_download(size: str = M.DEFAULT_SIZE, mirror: bool = False) -> dict[str, Any]:
    """开始下载模型。立刻返回，前端轮询 /models 看进度。"""
    if M.STATE.running:
        return {"ok": False, "message": "已经在下载了，请等它完成。", **_payload()}
    if size not in M.MODEL_CATALOG:
        raise HTTPException(status_code=400, detail=f"未知模型规格：{size}")
    if not M.library_available():
        raise HTTPException(
            status_code=400,
            detail="当前程序没有内置本地语音识别库。请换用带语音的完整版，或改用云 ASR。",
        )

    endpoint = M.MIRROR_ENDPOINT if mirror else ""
    asyncio.create_task(asyncio.to_thread(M.download, size, endpoint))
    return {
        "ok": True,
        "message": f"已开始下载 {size}"
        + ("（国内镜像）" if mirror else "（HuggingFace 官方源，国内可能较慢）"),
    }


@router.get("/models/progress")
async def progress() -> dict[str, Any]:
    return M.STATE.snapshot()


@router.delete("/models/{size}")
async def remove(size: str) -> dict[str, Any]:
    if M.STATE.running:
        raise HTTPException(status_code=409, detail="正在下载，先等它结束或重启程序。")
    if size not in M.MODEL_CATALOG:
        raise HTTPException(status_code=400, detail=f"未知模型规格：{size}")
    removed = await asyncio.to_thread(M.delete, size)
    return {"ok": removed, "message": "已删除" if removed else "本来就没下载过"}
