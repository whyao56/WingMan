#!/usr/bin/env python
"""WingMan 离线 OpenAI 兼容 stub —— 用来证明「文档教的接模型配置」真的通。

它做三件事，别的什么都不做：

    POST /v1/chat/completions   校验 Authorization: Bearer，返回 chat.completion
    GET  /v1/models             返回模型列表（同样要 Bearer）

为什么它值得存在：docs/MODELS.md 让用户填 `LLM_PROVIDER=openai_compat` +
`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`。这套配置通不通，不该靠「看起来对」
来判断 —— 把它指向本 stub，就能在**完全离线、没有任何真实密钥**的情况下，
用真实 HTTP + 真实 OpenAICompatProvider 跑通整条链路，并且 `/api/health` 的
providers 里 llm 会如实显示 `openai_compat`（而不是悄悄退回 mock）。

    python scripts\\openai_stub.py                 # 默认 127.0.0.1:8791
    python scripts\\openai_stub.py --port 8801
    python scripts\\openai_stub.py --model my-model --quiet

回复内容**不是这里发明的**：带 `[TASK:XXX]` 标记的请求会原样交给
backend/app/llm/mock.py 的 MockProvider.chat_raw()，把它返回的 JSON 字符串
塞进 assistant message。也就是说，通过 stub 拿到的输出与 Mock 模式逐字一致，
变的是「它真的走了 HTTP + openai_compat provider」这条路径。

刻意不做的事（避免「验证」变成走过场）：
- 不带 Bearer、Bearer 为空 → 401；路径不对 → 404；请求体不是合法 JSON → 400。
- 不实现 embeddings / audio / 流式（openai_compat.py 用不到），
  也不依赖 response_format —— 传了就忽略，照常返回 JSON 文本。

依赖：只用标准库做 HTTP 服务；唯一的外部依赖是复用 MockProvider 时连带
import 到 httpx，所以请用项目虚拟环境的解释器运行：

    backend\\.venv\\Scripts\\python.exe scripts\\openai_stub.py

退出码：0 = 正常停止（Ctrl+C）；1 = 运行期错误（例如端口被占用）；3 = 环境不满足（依赖缺失）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
PROJECT_DIR = SCRIPT_PATH.parents[1]
BACKEND_DIR = PROJECT_DIR / "backend"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
DEFAULT_MODEL = "wingman-stub"
API_PREFIX = "/v1"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ENV = 3

CHAT_PATH = f"{API_PREFIX}/chat/completions"
MODELS_PATH = f"{API_PREFIX}/models"

QUIET = False

# 早退时最多愿意为「把连接读干净」而丢弃多少请求体字节；超过就直接关连接
MAX_DRAIN_BYTES = 1024 * 1024

# 未识别 [TASK:XXX] 时的兜底回复。
# 存在的理由：控制台「测试连通」用的是没有任务标记的普通 system prompt，
# 规则引擎会直接抛「不认识任务标记」。返回一句诚实的短回复，比让用户看到红叉好；
# 它不会被当成规则引擎的输出（日志里标 UNKNOWN，一眼能区分）。
PLAIN_REPLY = "离线 stub 连通正常。"


# ---------------------------------------------------------------- 输出编码


def _can_encode(encoding: str | None, text: str) -> bool:
    if not encoding:
        return False
    try:
        text.encode(encoding, "strict")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _configure_stdio(mode: str = "auto") -> None:
    """自己处理 stdout/stderr 编码：GBK 控制台不抛 UnicodeEncodeError。

    auto 策略与 scripts/preflight.py 保持一致：连着真实控制台就沿用控制台编码；
    输出接了管道/重定向就用 UTF-8（PowerShell 7.4+ 默认按 UTF-8 解码原生输出）；
    调用方显式设了 PYTHONIOENCODING/PYTHONUTF8 就尊重调用方。
    utf-8 / console 分别强制 UTF-8 / 强制沿用流自身编码。
    无论哪种模式都带 errors=replace：漏网字符只会变 '?'，不会中断程序。
    """
    caller_forced = bool(os.environ.get("PYTHONIOENCODING") or os.environ.get("PYTHONUTF8"))
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        target = "utf-8" if mode == "utf-8" else None
        if mode == "auto":
            try:
                attached_to_console = bool(stream.isatty())
            except Exception:
                attached_to_console = False
            if not caller_forced and not attached_to_console:
                target = "utf-8"
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                if target:
                    reconfigure(encoding=target, errors="replace")
                else:
                    reconfigure(errors="replace")
            except Exception:
                pass


def say(text: str = "") -> None:
    print(text, flush=True)


def warn(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 规则引擎


def _load_mock_provider() -> Any:
    """复用 backend/app/llm/mock.py 的 MockProvider（不重新发明回复内容）。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    try:
        from app.llm.mock import MockProvider  # noqa: PLC0415 - 延迟 import 才能给出友好报错
    except ImportError as exc:
        warn(f"[stub] 无法导入规则引擎（backend/app/llm/mock.py）：{exc}")
        warn("[stub] 请用项目虚拟环境的解释器运行，例如：")
        warn(f"[stub]     {BACKEND_DIR / '.venv' / 'Scripts' / 'python.exe'} scripts\\openai_stub.py --port {DEFAULT_PORT}")
        warn("[stub] 还没建虚拟环境就先在仓库根目录运行 wingman.cmd --setup-only。")
        raise SystemExit(EXIT_ENV) from exc
    return MockProvider()


# ---------------------------------------------------------------- HTTP


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按 1 字 1 token，其余按 4 字符 1 token。"""
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    rest = len(text) - cjk
    return max(1, cjk + max(0, rest) // 4)


def _completion_id() -> str:
    return f"chatcmpl-stub-{int(time.time() * 1000) % 10_000_000:07d}"


class StubHandler(BaseHTTPRequestHandler):
    server_version = "WingManStub/1.0"
    protocol_version = "HTTP/1.1"
    sys_version = ""

    # 由 run_server() 注入
    provider: Any = None
    default_model: str = DEFAULT_MODEL
    request_count: int = 0

    # ---------------------------------------------------------- 基础设施

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆盖标准库方法名
        """默认实现往 stderr 打带时间戳的一行；这里改成 stdout 且受 --quiet 控制。"""
        if not QUIET:
            say(f"[stub] {self.address_string()} {fmt % args}")

    def handle_one_request(self) -> None:
        """每个请求开始前清掉「请求体已读」标记。

        同一个 handler 实例会在一条 keep-alive 连接上处理多个请求，
        不复位的话：上一个请求读过 body 留下的 _body_read=True，
        会让下一个请求在 401/404 早退时跳过「把 body 读掉」，
        残留字节被当成下一个请求解析（实测报 Unsupported method '{"model":...}POST'）。
        """
        self._body_read = False
        super().handle_one_request()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        # 必须显式声明 charset：中文在响应体里，不带 charset 时部分客户端会按 latin-1 解
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str = "invalid_request_error") -> None:
        # 早退（401/404/400）时请求体可能还没读出来。HTTP/1.1 是长连接，
        # 残留字节会被当成下一个请求解析 —— 实测会解析出
        # Unsupported method ('{"model":...}POST')，客户端随后拿到空响应。
        # 所以：能读就主动读掉丢弃（连接可继续复用），读不完就显式关连接。
        if not getattr(self, "_body_read", False):
            length = self._pending_body_length()
            if length > 0:
                if length <= MAX_DRAIN_BYTES:
                    try:
                        self.rfile.read(length)
                        self._body_read = True
                    except Exception:
                        self.close_connection = True
                else:
                    self.close_connection = True
        self._send_json(status, {
            "error": {"message": message, "type": code, "code": code, "param": None},
        })

    def _pending_body_length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _authorized(self) -> bool:
        """严格校验 Bearer：本地 stub 也必须让「没填 Key 就 401」这条路真的通。"""
        raw = self.headers.get("Authorization") or ""
        if not raw.startswith("Bearer "):
            self._error(401, "缺少 Authorization: Bearer <api_key>。"
                             "请确认配置里填了非空的 llm_api_key（内容是任意字符串即可）。",
                        code="invalid_api_key")
            return False
        if not raw[len("Bearer "):].strip():
            self._error(401, "Authorization 里的 Bearer token 为空。", code="invalid_api_key")
            return False
        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        length = self._pending_body_length()
        raw = self.rfile.read(length) if length > 0 else b""
        self._body_read = True  # 标记已把请求体读干净，后续出错不用关连接
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"请求体不是合法 JSON：{exc}")
            return None
        if not isinstance(payload, dict):
            self._error(400, "请求体必须是 JSON 对象。")
            return None
        return payload

    # ---------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定的方法名
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != MODELS_PATH:
            self._error(404, f"未知路径 {self.path}；本 stub 只提供 {CHAT_PATH} 与 {MODELS_PATH}。",
                        code="not_found")
            return
        if not self._authorized():
            return
        self._send_json(200, {
            "object": "list",
            "data": [{
                "id": self.default_model,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "wingman-stub",
            }],
        })

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != CHAT_PATH:
            self._error(404, f"未知路径 {self.path}；本 stub 只提供 {CHAT_PATH} 与 {MODELS_PATH}。",
                        code="not_found")
            return
        if not self._authorized():
            return
        payload = self._read_json_body()
        if payload is None:
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error(400, "messages 必须是非空数组（OpenAI 兼容格式）。")
            return
        cleaned: list[dict[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                self._error(400, "messages 里每一项都必须是 {\"role\", \"content\"} 对象。")
                return
            cleaned.append({"role": str(item.get("role") or "user"),
                            "content": str(item.get("content") or "")})

        model = str(payload.get("model") or self.default_model)
        # 规则引擎按 system prompt 首行的 [TASK:XXX] 分派；这里只做「有没有标记」的兜底，
        # 内容一律由 MockProvider.chat_raw 产生（见模块 docstring）。
        from app.llm.base import task_tag  # noqa: PLC0415

        system = next((m["content"] for m in cleaned if m["role"] == "system"), "")
        tag = task_tag(system)
        used_rule_engine = True
        try:
            content = asyncio.run(self.provider.chat_raw(
                cleaned,
                json_mode=bool(payload.get("response_format")),
                temperature=payload.get("temperature"),
                max_tokens=payload.get("max_tokens"),
            ))
        except Exception as exc:  # noqa: BLE001
            if tag == "UNKNOWN":
                # 普通对话（没有任务标记）：规则引擎不认识，回一句诚实的短回复
                content = PLAIN_REPLY
                used_rule_engine = False
            else:
                self._error(400, f"规则引擎处理 [TASK:{tag}] 失败：{type(exc).__name__}: {exc}")
                return

        prompt_text = "".join(m["content"] for m in cleaned)
        usage = {
            "prompt_tokens": _estimate_tokens(prompt_text),
            "completion_tokens": _estimate_tokens(content),
            "total_tokens": _estimate_tokens(prompt_text) + _estimate_tokens(content),
        }
        StubHandler.request_count += 1
        if not QUIET:
            say(f"[stub] POST {CHAT_PATH} 200 · model={model} · task={tag} · "
                f"{'规则引擎' if used_rule_engine else '兜底回复'} · {len(content)} 字符")
        self._send_json(200, {
            "id": _completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": usage,
            "system_fingerprint": "wingman-offline-stub",
        })


class StubServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一个端口（请求随机落到其中一个），
    # 那会让「端口占用」静默成功 —— stub 宁可启动失败也不要这种假象。
    allow_reuse_address = os.name != "nt"


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openai_stub.py",
        description="WingMan 离线 OpenAI 兼容 stub：不联网、不要真实密钥，"
                    "用来验证 openai_compat 那套 base_url / api_key / model 配置真的通。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "在 .env 或控制台「设置」里这样填：\n"
            f"  LLM_PROVIDER=openai_compat\n"
            f"  LLM_BASE_URL=http://{DEFAULT_HOST}:{DEFAULT_PORT}{API_PREFIX}\n"
            f"  LLM_API_KEY=wingman-stub          （本地 stub 不校验内容，非空即可）\n"
            f"  LLM_MODEL={DEFAULT_MODEL}\n"
            f"  EMBEDDER=hash                    （离线 stub 没有 /v1/embeddings，\n"
            f"                                    默认 auto 会跟随 llm_base_url 指向 stub 并建索引失败）\n"
            "填完在控制台点「测试连通」，或看 /api/health 的 providers：llm 应显示 openai_compat。\n"
            "退出码：0 正常停止 / 1 运行期错误（如端口被占用）/ 3 环境不满足（依赖缺失）。\n"
        ),
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"监听地址，默认 {DEFAULT_HOST}（只在本机可见）")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"/v1/models 里公布的模型名，默认 {DEFAULT_MODEL}")
    parser.add_argument("--quiet", action="store_true", help="不打每个请求的日志，只留启动提示")
    parser.add_argument("--encoding", default="auto", choices=("auto", "utf-8", "console"),
                        help="输出编码：auto=控制台用控制台编码、管道用 UTF-8（默认）；"
                             "utf-8=强制 UTF-8；console=强制沿用流自身编码")
    return parser


def _prescan_argv(argv: list[str]) -> tuple[str, bool]:
    """正式解析前先拿 --encoding / --quiet，保证启动提示本身不乱码、且尊重 --quiet。"""
    encoding, quiet = "auto", False
    for index, token in enumerate(argv):
        if token.startswith("--encoding="):
            encoding = token.split("=", 1)[1]
        elif token == "--encoding" and index + 1 < len(argv):
            encoding = argv[index + 1]
        elif token == "--quiet":
            quiet = True
    return encoding, quiet


def run_server(args: argparse.Namespace) -> int:
    provider = _load_mock_provider()
    StubHandler.provider = provider
    StubHandler.default_model = args.model
    StubHandler.request_count = 0

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        warn(f"[stub] 注意：绑定到 {args.host} 会让这个本地 stub 暴露给同网段的其他机器。")

    try:
        server = StubServer((args.host, args.port), StubHandler)
    except OSError as exc:
        warn(f"[stub] 无法监听 {args.host}:{args.port} —— {exc}")
        warn(f"[stub] 常见原因：端口已被占用（换个端口：--port {args.port + 1}），"
             "或该端口需要管理员权限。")
        return EXIT_ERROR

    base_url = f"http://{args.host}:{args.port}{API_PREFIX}"
    say("=" * 64)
    say("WingMan 离线 OpenAI 兼容 stub 已就绪（不联网，不需要真实密钥）")
    say("=" * 64)
    say(f"  base_url : {base_url}")
    say(f"  api_key  : 任意非空字符串（测试用可直接填 {DEFAULT_MODEL}）")
    say(f"  model    : {args.model}")
    say(f"  端点     : POST {CHAT_PATH} · GET {MODELS_PATH}")
    say(f"  地址     : http://{args.host}:{args.port}    （停止：Ctrl+C）")
    say("")
    say("  在 .env 里这样填（或填到控制台「设置」）：")
    say("    LLM_PROVIDER=openai_compat")
    say(f"    LLM_BASE_URL={base_url}")
    say(f"    LLM_API_KEY={DEFAULT_MODEL}")
    say(f"    LLM_MODEL={args.model}")
    # 这一行不是可选项：应用默认 EMBEDDER=auto 会复用主模型端点（memory/embedder.py:194-198），
    # 而本 stub 按设计没有 /v1/embeddings，不改成 hash 的话向量索引永远建不起来。
    say("    # 离线 stub 没有 /v1/embeddings；不设下面这行，建索引会失败")
    say("    EMBEDDER=hash")
    say("=" * 64)
    say(f"  回复由 {type(provider).__module__}.{type(provider).__name__} 产生"
        "（任务标记 ANALYZE / STRATEGY / SUGGEST / SIMULATE / FACTS / PROFILE 走规则引擎，"
        "其余走一句话兜底回复）")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("")
        say(f"[stub] 收到 Ctrl+C，已停止（共处理 {StubHandler.request_count} 个请求）。")
        return EXIT_OK
    finally:
        server.server_close()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    global QUIET
    argv = list(sys.argv[1:] if argv is None else argv)
    encoding, quiet = _prescan_argv(argv)
    _configure_stdio(encoding)
    # 先按预扫描结果定下 --quiet，保证 argparse 自身的报错输出也遵守它
    QUIET = quiet

    args = build_parser().parse_args(argv)
    QUIET = args.quiet
    if not 1 <= args.port <= 65535:
        warn(f"[stub] 端口超出范围：{args.port}（合法范围 1-65535）")
        return EXIT_ERROR
    try:
        return run_server(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 不打堆栈给用户，给一句话
        warn(f"[stub] 启动失败：{type(exc).__name__}: {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_OK)
