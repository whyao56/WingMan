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


# ---------------------------------------------------------------- 桌面壳（需求 9）


@router.get("/desktop/state")
async def desktop_state() -> dict[str, Any]:
    """界面问一句：现在有没有原生窗口？

    有 → 右上角的 × 已经被后端接管，界面不用自己画一个；
    没有（浏览器模式）→ 界面左上角给一个 × 按钮，走同一套选择流程。
    """
    from .. import desktop

    return desktop.desktop_state()


@router.post("/desktop/close")
async def desktop_close(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """用户在「关闭程序 / 后台运行」里选完之后回调这里。

    `background` 在没有原生窗口时（浏览器模式）退化成「什么都不做」——
    服务本来就是一个独立进程，关掉标签页它就还在跑。文案会把这点说清楚，
    而不是假装自己隐藏了一个并不存在的窗口。
    """
    from .. import desktop

    action = str(payload.get("action") or "").strip()
    if action not in {"quit", "background", "cancel"}:
        raise HTTPException(status_code=422, detail="action 只能是 quit / background / cancel。")
    return await asyncio.to_thread(desktop.close_action, action)


@router.post("/desktop/show")
async def desktop_show() -> dict[str, Any]:
    """把「后台运行」藏起来的窗口调回来。

    存在的理由：`desktop.py` 的单实例分支靠这个接口把老进程的窗口 show 出来。
    不这么做的话，第二次双击 `WingMan.exe` 会开出一个**没有服务的空壳窗口**，
    关掉它并不会停掉真正在跑的那个进程 —— 用户会觉得「关闭程序失灵了」。
    """
    from .. import desktop

    return await asyncio.to_thread(desktop.show_window)


# ---------------------------------------------------------------- 检查更新（需求 5）


REPO_URL = "https://github.com/whyao56/WingMan"
RELEASES_API = "https://api.github.com/repos/whyao56/WingMan/releases/latest"


def _ver_tuple(v: str) -> tuple:
    """把版本号拆成可比较的元组。非数字段按 0 处理，不抛错。"""
    parts = []
    for chunk in str(v or "").strip().lstrip("v").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


@router.get("/update/check")
async def update_check() -> dict[str, Any]:
    """问一次 GitHub 上有没有新版本。

    **只有你点按钮时才会联网**，平时不查 —— 一个聊天记录工具没有理由
    开机就往外面发请求。查不到（断网、被墙、限流）不是错误，是要如实说
    清楚的一件事：不知道就说不知道，别把「查不到」说成「已是最新」。
    """
    import json as _json
    import urllib.request

    def _fetch() -> dict[str, Any]:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"User-Agent": f"WingMan/{__version__}",
                     "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            return _json.loads(resp.read().decode("utf-8", "ignore"))

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as exc:
        return {
            "ok": False, "current": __version__,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            "hint": "没查到不等于已是最新 —— 可能是断网、代理或 GitHub 访问不了。"
                    "可以直接打开项目地址自己看一眼。",
            "repo": REPO_URL,
        }

    latest = str(data.get("tag_name") or "").lstrip("v")
    return {
        "ok": True,
        "current": __version__,
        "latest": latest,
        "has_update": _ver_tuple(latest) > _ver_tuple(__version__),
        "name": str(data.get("name") or ""),
        "published_at": str(data.get("published_at") or ""),
        "notes": str(data.get("body") or "")[:4000],
        "page": str(data.get("html_url") or f"{REPO_URL}/releases"),
        "repo": REPO_URL,
    }
