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
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import ROUTERS
from .asr.capture import get_session
from .bus import bus
from .config import FRONTEND_DIR, LOG_DIR, get_settings
from .context import get_ctx
from .llm.base import LLMError

logger = logging.getLogger("wingman")


def _setup_logging(level: str) -> None:
    """控制台 + 滚动文件双写。

    打包成 exe 后没有控制台，stdout 直接进黑洞 —— 日志文件是唯一能
    事后排查的地方，所以这里不是「顺便加一下」，是必需品。
    """
    handlers: list[logging.Handler] = []

    # 没有控制台（pythonw / --noconsole）时不要往 stdout 写，某些环境下会抛异常
    if sys.stdout is not None and getattr(sys.stdout, "write", None):
        # 冻结后 stdout 会回落到 Windows 本地代码页（中文机器上是 GBK），
        # 中文日志直接变乱码。显式改成 UTF-8，解不出来的字符也别抛异常。
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass
        handlers.append(logging.StreamHandler(sys.stdout))

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / "wingman.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-26s | %(message)s"
        ))
        handlers.append(file_handler)
    except OSError:
        pass  # 磁盘只读之类，不因为这个把程序挡住

    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-26s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers or None,
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


class JsonCharsetMiddleware:
    """给所有 `application/json` 响应补上 `charset=utf-8`。

    背景：FastAPI/Starlette 的默认 JSON 响应头只有 `application/json`。中文用户在
    cp936 的 cmd 里按文档用 curl 排错时，curl 会按本地代码页解码，`/api/health`
    里的中文 note 就成了乱码。这里只在响应头层面补 charset：
      * 不改响应体字段、不改状态码、不碰其它 content-type（StaticFiles 的 text/html
        与 CORS 响应头都不受影响）；
      * 用纯 ASGI 中间件实现（不用 BaseHTTPMiddleware），因此流式响应与静态文件
        的行为保持不变。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_charset(message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                for index, (name, value) in enumerate(headers):
                    if name.lower() == b"content-type":
                        lowered = value.lower()
                        if lowered.startswith(b"application/json") and b"charset" not in lowered:
                            headers[index] = (name, value + b"; charset=utf-8")
                        break
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_charset)


# 先注册 charset 中间件、再注册 CORS：后注册的在外层，CORS 仍然能覆盖所有响应
app.add_middleware(JsonCharsetMiddleware)
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
