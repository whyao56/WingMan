"""设置与健康检查。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import __version__
from ..config import EDITABLE_KEYS, SECRET_KEYS, mask_secret
from ..context import get_ctx
from ..schemas import HealthOut, ProviderInfo
from ..selfcheck import runtime_paths

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    ctx = get_ctx()
    counts = await asyncio.to_thread(ctx.store.counts)
    return HealthOut(
        version=__version__,
        db=str(ctx.store.db_path),
        counts=counts,
        providers=[ProviderInfo(**p) for p in ctx.provider_summary()],
    )


# ---------------------------------------------------------------- 自检


@router.get("/selfcheck")
async def selfcheck() -> dict[str, Any]:
    """给新手看的体检报告。每一项都带「缺什么、怎么补」。"""
    ctx = get_ctx()
    result = await asyncio.to_thread(ctx.self_check)
    result["runtime"] = ctx.runtime_info()
    result["paths"] = runtime_paths()
    return result


@router.post("/runtime/open")
async def open_path(which: str = "data") -> dict[str, Any]:
    """在系统文件管理器里打开数据/日志目录。

    只允许白名单里的目录 —— 这个接口不接受任意路径，免得变成一个
    「用浏览器就能打开本机任意目录」的洞。
    """
    paths = runtime_paths()
    if which not in paths:
        raise HTTPException(status_code=400, detail=f"未知目录：{which}")
    target = Path(paths[which])
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开目录失败：{exc}") from exc
    return {"ok": True, "path": str(target)}


@router.get("/settings")
async def get_settings_api() -> dict[str, Any]:
    ctx = get_ctx()
    values: dict[str, Any] = {}
    for key in EDITABLE_KEYS:
        raw = ctx.cfg(key)
        if key in SECRET_KEYS:
            values[key] = mask_secret(str(raw or ""))
            values[f"{key}_set"] = bool(raw)
        else:
            values[key] = raw
    return {
        "values": values,
        "overrides": {k: (mask_secret(v) if k in SECRET_KEYS else v)
                      for k, v in ctx.overrides().items()},
        "editable": list(EDITABLE_KEYS),
        "providers": ctx.provider_summary(),
    }


@router.put("/settings")
async def update_settings(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ctx = get_ctx()
    # 前端回传掩码值时不要覆盖真实密钥
    clean: dict[str, Any] = {}
    for k, v in patch.items():
        if k in SECRET_KEYS and isinstance(v, str) and "***" in v:
            continue
        clean[k] = v
    applied = ctx.update_cfg(clean)
    return {"applied": applied, "providers": ctx.provider_summary()}


@router.post("/settings/reset")
async def reset_settings() -> dict[str, Any]:
    ctx = get_ctx()
    ctx.reset_cfg()
    return {"ok": True, "providers": ctx.provider_summary()}


@router.post("/settings/test")
async def test_provider(which: str = "llm") -> dict[str, Any]:
    ctx = get_ctx()
    if which == "llm":
        ok, msg = await ctx.llm.ping()
        return {"ok": ok, "name": ctx.llm.name, "message": msg}
    if which == "embedder":
        emb = ctx.embedder
        try:
            v = await emb.embed_one("测试")
            return {"ok": True, "name": emb.name, "message": f"维度 {v.shape[0]} · {emb.note}"}
        except Exception as exc:
            return {"ok": False, "name": emb.name, "message": str(exc)[:300]}
    raise HTTPException(status_code=400, detail=f"未知的 provider 类型：{which}")


@router.get("/settings/models")
async def list_models(which: str = "llm") -> dict[str, Any]:
    ctx = get_ctx()
    provider = ctx.llm
    if not hasattr(provider, "list_models"):
        return {"models": [], "note": "当前 Provider 不支持列出模型"}
    try:
        models = await provider.list_models()
    except Exception as exc:
        return {"models": [], "note": str(exc)[:200]}
    return {"models": models, "note": ""}
