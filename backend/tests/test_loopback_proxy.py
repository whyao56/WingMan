"""回归：回环（本机）服务不能被系统代理劫持。

背景（t5 实测）：httpx 默认 `trust_env=True` 会继承 `HTTP_PROXY` 环境变量**以及 Windows
注册表里的系统代理**。本机代理软件开全局模式、或公司代理存在时，发往 `127.0.0.1` 的请求
会被代理接管 —— 本机 Ollama / 离线 stub / 本机 embedding 服务直接不可用，而且错误会伪装成
「网关错误（502）」，用户查不到原因。

本文件用两个本地假服务把这件事变成确定性实验：
- `_FakeProxy`：对任何请求都回 **502 空体**（模拟「代理接了但转发不通」）；
- `_FakeLoopbackService`：模拟本机 stub / Ollama / embedding / ASR 服务。

用例：
1) 回环 base_url → 请求直连本机服务（不被代理截胡），且假代理收到 0 个请求；
2) 非回环 base_url → 仍然走环境代理（502），证明没有误改 trust_env 语义；
3) 回环 + 无人监听端口 → 报的是连接类错误，**不是** 502 网关错误（本缺陷的核心可观察点）；
4) 四份 `_trust_env_for` 实现行为一致的漂移守卫。

说明：这些用例只用本地 socket 与 provider 对象，不碰数据库（不涉及 backend/data/wingman.db）。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


# ---------------------------------------------------------------- 假服务


class _FakeProxy(BaseHTTPRequestHandler):
    """模拟「代理接了请求但转发不通」：一律 502 空体。"""

    protocol_version = "HTTP/1.0"
    seen: list[str] = []

    def do_GET(self):  # noqa: N802
        self._fail()

    def do_POST(self):  # noqa: N802
        self._fail()

    def _fail(self):
        _FakeProxy.seen.append(self.path)
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: D102
        pass


class _FakeLoopbackService(BaseHTTPRequestHandler):
    """模拟本机 OpenAI 兼容 stub / Ollama / embedding / ASR 服务。"""

    protocol_version = "HTTP/1.0"
    seen: list[str] = []

    def do_GET(self):  # noqa: N802
        self._respond()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._respond()

    def _respond(self):
        path = self.path.split("?", 1)[0]
        _FakeLoopbackService.seen.append(path)
        if path.endswith("/chat/completions"):
            body = {"choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "stub-ok"}}]}
        elif path.endswith("/api/chat"):
            body = {"message": {"role": "assistant", "content": "ollama-ok"}}
        elif path.endswith("/api/tags"):
            body = {"models": [{"name": "qwen2.5:7b"}]}
        elif path.endswith("/embeddings"):
            body = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]}
        elif path.endswith("/audio/transcriptions"):
            body = {"text": "你好", "language": "zh"}
        else:
            body = {"error": f"unknown path {path}"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D102
        pass


class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _proxy_env:
    """把环境代理指向假代理（并清掉 NO_PROXY，避免绕过）。"""

    KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

    def __init__(self, proxy_url: str):
        self.proxy_url = proxy_url
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for key in self.KEYS:
            self.saved[key] = os.environ.get(key)
            os.environ[key] = self.proxy_url
        for key in ("NO_PROXY", "no_proxy"):
            self.saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- 用例


def test_loopback_requests_bypass_env_proxy_and_reach_local_service() -> None:
    """回环 base_url：四个 provider 都必须直连本机服务，假代理一个请求都收不到。"""
    from app.asr.cloud import CloudASR
    from app.llm.ollama import OllamaProvider
    from app.llm.openai_compat import OpenAICompatProvider
    from app.memory.embedder import CloudEmbedder

    proxy = _Server(_FakeProxy)
    service = _Server(_FakeLoopbackService)
    _FakeProxy.seen.clear()
    _FakeLoopbackService.seen.clear()
    try:
        with _proxy_env(proxy.url):
            messages = [{"role": "user", "content": "你好"}]

            llm = OpenAICompatProvider(base_url=f"{service.url}/v1", api_key="k", model="m")
            assert asyncio.run(llm.chat_raw(messages)) == "stub-ok"
            assert asyncio.run(llm.list_models()) == []

            ollama = OllamaProvider(host=service.url, model="qwen2.5:7b")
            assert asyncio.run(ollama.chat_raw(messages)) == "ollama-ok"
            assert asyncio.run(ollama.list_models()) == ["qwen2.5:7b"]

            embedder = CloudEmbedder(base_url=f"{service.url}/v1", api_key="k", model="emb", dim=4)
            vector = asyncio.run(embedder.embed_one("喜欢猫"))
            assert vector.shape[0] == 4, vector.shape

            asr = CloudASR(base_url=f"{service.url}/v1", api_key="k")
            pcm = (np.ones(16000, dtype=np.float32) * 0.5)
            result = asyncio.run(asr.transcribe(pcm, sample_rate=16000))
            assert result.text == "你好", result

        assert _FakeProxy.seen == [], f"回环请求被代理截胡了：{_FakeProxy.seen}"
        paths = _FakeLoopbackService.seen
        for expected in ("/v1/chat/completions", "/v1/models", "/api/chat", "/api/tags",
                         "/v1/embeddings", "/v1/audio/transcriptions"):
            assert expected in paths, f"本机服务没收到 {expected}：{paths}"
    finally:
        service.stop()
        proxy.stop()


def test_non_loopback_still_uses_env_proxy() -> None:
    """非回环 base_url：仍然继承环境代理（不能把用户走代理访问云端的能力改没了）。"""
    from app.llm.base import LLMError
    from app.llm.openai_compat import OpenAICompatProvider

    proxy = _Server(_FakeProxy)
    _FakeProxy.seen.clear()
    try:
        with _proxy_env(proxy.url):
            llm = OpenAICompatProvider(base_url="http://example.invalid/v1",
                                       api_key="k", model="m")
            try:
                asyncio.run(llm.chat_raw([{"role": "user", "content": "hi"}]))
            except LLMError as exc:
                text = str(exc)
                assert "502" in text, f"非回环请求没有走代理（期望代理的 502）：{text}"
            else:
                raise AssertionError("非回环地址竟然绕过了环境代理")
        assert len(_FakeProxy.seen) == 1, f"代理应收到 1 个请求，实际 {_FakeProxy.seen}"
    finally:
        proxy.stop()


def test_dead_loopback_port_reports_connection_error_not_gateway_502() -> None:
    """本缺陷的核心可观察点：回环 + 无人监听 → 连接类错误，而不是「网关错误（502）」。"""
    from app.llm.base import LLMError
    from app.llm.openai_compat import OpenAICompatProvider

    proxy = _Server(_FakeProxy)
    _FakeProxy.seen.clear()
    dead = _free_port()
    try:
        with _proxy_env(proxy.url):
            llm = OpenAICompatProvider(base_url=f"http://127.0.0.1:{dead}/v1",
                                       api_key="k", model="m")
            try:
                asyncio.run(llm.chat_raw([{"role": "user", "content": "hi"}]))
            except LLMError as exc:
                text = str(exc)
                assert "502" not in text, f"错误信息被代理污染了：{text}"
                assert "网关错误" not in text, f"还是网关错误：{text}"
                assert ("网络错误" in text) or ("超时" in text), text
            else:
                raise AssertionError("指向无人监听的端口竟然没有报错")
        assert _FakeProxy.seen == [], f"回环请求不该经过代理：{_FakeProxy.seen}"
    finally:
        proxy.stop()


def test_trust_env_helper_agrees_across_all_four_modules() -> None:
    """四份实现必须行为一致（写范围不允许新增共用模块，所以用这个用例防漂移）。"""
    from app.asr.cloud import _trust_env_for as asr_trust
    from app.llm.ollama import _trust_env_for as ollama_trust
    from app.llm.openai_compat import _trust_env_for as openai_trust
    from app.memory.embedder import _trust_env_for as embed_trust

    helpers = {
        "openai_compat": openai_trust,
        "ollama": ollama_trust,
        "embedder": embed_trust,
        "asr_cloud": asr_trust,
    }
    loopback = [
        "http://127.0.0.1:8787/v1",
        "http://127.0.0.5:11434",
        "127.0.0.1:11434",              # 没写 scheme 也要认出来
        "http://localhost:11434",
        "http://LOCALHOST:1/v1",
        "http://localhost.:11434",      # 带尾点的写法
        "http://[::1]:8000/v1",
        "::1",
    ]
    remote = [
        "https://api.deepseek.com/v1",
        "http://192.168.1.10:11434",
        "http://ollama.lan:11434",
        "http://example.invalid",
        "",
    ]
    for url in loopback:
        results = {name: fn(url) for name, fn in helpers.items()}
        assert set(results.values()) == {False}, f"{url} 应视为回环：{results}"
    for url in remote:
        results = {name: fn(url) for name, fn in helpers.items()}
        assert set(results.values()) == {True}, f"{url} 应继承环境代理：{results}"


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [(name, obj) for name, obj in list(globals().items())
            if name.startswith("test_") and callable(obj)]


def main() -> int:
    print("=" * 68, flush=True)
    print("回环代理回归：本机 stub / Ollama / embedding / ASR 不得被系统代理劫持", flush=True)
    print("=" * 68, flush=True)
    failures: list[tuple[str, str]] = []
    skipped = 0
    tests = _collect_tests()
    for index, (name, function) in enumerate(tests, start=1):
        try:
            function()
        except _SkipTest as exc:
            skipped += 1
            print(f"[{index:>2}/{len(tests)}] SKIP {name} —— {exc}", flush=True)
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"[{index:>2}/{len(tests)}] FAIL {name} —— {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"[{index:>2}/{len(tests)}] OK   {name}", flush=True)
    print("-" * 68, flush=True)
    print(f"通过 {len(tests) - len(failures) - skipped} / 跳过 {skipped} / 失败 {len(failures)}", flush=True)
    if failures:
        for name, error in failures:
            print(f"  FAILED {name}: {error}", flush=True)
        print("结果：失败", flush=True)
        return 1
    print("结果：全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
