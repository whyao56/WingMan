"""FastAPI 应用入口。

    python -m uvicorn app.main:app --reload --port 8787
或
    python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import ROUTERS
from .asr.capture import get_session
from .bus import bus
from .config import FRONTEND_DIR, get_settings
from .context import get_ctx
from .llm.base import LLMError

logger = logging.getLogger("wingman")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-26s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _setup_logging(settings.log_level)

    ctx = get_ctx()
    bus.bind_loop(asyncio.get_running_loop())

    counts = ctx.store.counts()
    logger.info("WingMan v%s 启动中…", __version__)
    logger.info("数据文件：%s", ctx.store.db_path)
    logger.info(
        "已有数据：%d 条消息 / %d 条事实 / %d 个会话",
        counts["messages"], counts["facts"], counts["chats"],
    )
    for p in ctx.provider_summary():
        flag = "OK " if p["available"] else "!! "
        logger.info("%s%-9s → %-14s %s", flag, p["kind"], p["name"], p["note"])

    if ctx.llm.name == "mock":
        logger.warning(
            "当前使用 Mock Provider，输出仅用于演示流程。"
            "接入真实模型请在控制台「设置」里配置 base_url / api_key / model。"
        )

    try:
        yield
    finally:
        session = get_session()
        if session and session.running:
            logger.info("停止语音采集…")
            await asyncio.to_thread(session.stop)
        logger.info("WingMan 已退出")


app = FastAPI(
    title="WingMan · 聊天僚机",
    description="本地优先的对话参谋系统：记忆聊天记录、听懂通话、给出可推演的回复建议。",
    version=__version__,
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _settings.cors_origins.split(",") if o.strip()] or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in ROUTERS:
    app.include_router(r)


# ---------------------------------------------------------------- 错误处理


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
    logger.warning("模型调用失败：%s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc), "kind": "llm_error"},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "kind": "internal"},
    )


# ---------------------------------------------------------------- 前端

if FRONTEND_DIR.exists():
    # 放在所有 API 路由之后注册，保证 /api/* 优先命中
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="console")
else:
    logger.warning("找不到前端目录：%s", FRONTEND_DIR)


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        reload=False,
        log_level=s.log_level.lower(),
    )


if __name__ == "__main__":
    main()
