"""设置与健康检查。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import __version__
from ..config import EDITABLE_KEYS, SECRET_KEYS, mask_secret
from ..context import get_ctx
from ..schemas import HealthOut, ProviderInfo

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
    if which == "asr":
        eng = ctx.asr
        return {"ok": eng.available, "name": eng.name, "message": eng.note}
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
