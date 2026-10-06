"""回归：本机服务不许把浏览器还要复用的那条连接收掉。

用户报的现象是「点采集，界面弹出一句英文 `Failed to fetch`」。根因不在采集逻辑里：

uvicorn 的 `timeout_keep_alive` 默认是 **5 秒** —— 一条连接空闲超过 5 秒它就被
服务端关掉。浏览器（桌面版用的是 WebView2）并不知道，仍然会在这条连接上发下一个
请求，拿回来的是**零字节响应**。这一步的后果是不对称的：

    GET  浏览器会静默重试  → 看起来一切正常（所以「检查本机版本」是好的）
    POST 浏览器不重试      → `net::ERR_CONNECTION_CLOSED` 原样抛给 JS

而前端那时候是 `catch (e) { toast(e.message, "err") }`，于是界面上显示的就是
那句英文 `Failed to fetch`。采集页上所有动作都是 POST —— 所以只有采集「坏了」。

实测复现（v0.1.2 桌面版，默认配置）：

    第一次 GET                         → HTTP/1.1 200 OK（连接进入 keep-alive）
    空闲 6 秒后在同一连接上发 POST     → ConnectionAbortedError [WinError 10053]

这个文件钉两件事，缺一不可：

1. **配置层**：两个启动入口都必须把 `timeout_keep_alive` 显式设成足够大的值。
   只改一处是最容易漏的 —— 源码用户走 `python -m app.main`，桌面版走
   `app/desktop.py`，两条路都得出问题才算真的修好。
2. **行为层**：真起一个服务，真做「GET → 空闲 6 秒 → 同一条连接上 POST」，
   必须拿到 HTTP 响应。只断言配置的话，「配了但没传下去」是查不出来的。

跑法：

    cd backend
    python -m pytest tests/test_http_keepalive.py -q
    python tests/test_http_keepalive.py
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

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


# 空闲多久算「用户读了一会儿界面」。必须大于 uvicorn 的 5 秒默认值，
# 否则这个用例在坏代码上也会通过 —— 那就成了假守卫。
IDLE_S = 6.0

# 允许的最短 keep-alive。不是「越大越好」：它要明显长于浏览器的复用窗口，
# 60 秒是底线（用户看着界面想事情、翻一下支持矩阵，很容易超过 5 秒）。
MIN_KEEP_ALIVE_S = 60.0


# ---------------------------------------------------------------- 配置层


def test_both_entry_points_set_a_long_keep_alive() -> None:
    """两个启动入口都要显式设置 `timeout_keep_alive`，且不能小于底线。

    这是「源码扫一遍」的守卫，和 `test_privacy_pseudonyms.py` 同一个思路：
    这类配置项漏掉不会报错，只会让用户看到一句英文 —— 除了钉住没有别的办法。
    """
    from app.config import KEEP_ALIVE_S

    assert KEEP_ALIVE_S >= MIN_KEEP_ALIVE_S, (
        f"KEEP_ALIVE_S={KEEP_ALIVE_S} 太短：浏览器的复用窗口比它长，"
        "请求会被写进服务端已经收掉的连接")

    for name in ("desktop.py", "main.py"):
        src = (BACKEND / "app" / name).read_text(encoding="utf-8")
        assert "timeout_keep_alive=KEEP_ALIVE_S" in src, (
            f"app/{name} 没有把 timeout_keep_alive 设上 —— "
            "它一旦回落到默认的 5 秒，采集那些 POST 又会报 Failed to fetch")


# ---------------------------------------------------------------- 行为层


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _read_response(sock: socket.socket) -> bytes:
    """读**一整个** HTTP 响应（按 Content-Length 读到齐）。

    不能只 `recv` 一次：TCP 是字节流，一次 recv 可能只拿到半个响应体，
    剩下的部分会被误当成「下一个响应」—— 那样测出来的结论是假的
    （第一次读剩的尾字节会让第二个请求看起来有响应）。
    """
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return b""
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                length = 0
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


def _one_request(sock: socket.socket, raw: bytes) -> str:
    """发一个请求，返回状态行；拿不到响应就把原因写出来。"""
    sock.sendall(raw)
    sock.settimeout(15)
    try:
        data = _read_response(sock)
    except OSError as exc:
        return f"<连接被掐断：{type(exc).__name__}>"
    if not data:
        return "<零字节响应>"
    return data.split(b"\r\n", 1)[0].decode("utf-8", "replace")


def _start_server(port: int):
    """起一个和桌面版同配置的服务，返回 (server, thread)。

    `lifespan="off"`：这个用例要验的是 HTTP 连接的生命周期，不需要建库、
    也不需要碰用户的真实数据目录 —— 把启动钩子关掉，用例就不会有副作用。
    """
    import uvicorn

    from app.config import KEEP_ALIVE_S
    from app.main import app

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False, log_config=None,
        loop="asyncio", lifespan="off",
        timeout_keep_alive=KEEP_ALIVE_S,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="keepalive-probe", daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
                s.sendall(b"GET /api/collect/matrix HTTP/1.1\r\n"
                          b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
                if s.recv(64).startswith(b"HTTP/1.1 200"):
                    return server, thread
        except OSError:
            time.sleep(0.2)
    raise _SkipTest("服务在 20 秒内没有起来，跳过")


def test_post_on_a_reused_connection_still_gets_an_answer() -> None:
    """GET → 空闲 6 秒 → **同一条连接**上 POST，必须拿到 HTTP 响应。

    这就是用户踩的那一步。「连接被掐断 / 零字节响应」= 缺陷复现。
    """
    port = _free_port()
    server, thread = _start_server(port)
    body = b'{"client":"qq","key":"x"}'      # 会被拒（不是密钥），但一定会回一个 200
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            first = _one_request(s, b"GET /api/collect/matrix HTTP/1.1\r\n"
                                    b"Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
            assert first.startswith("HTTP/1.1 200"), first

            time.sleep(IDLE_S)

            second = _one_request(
                s, b"POST /api/collect/key/check HTTP/1.1\r\n"
                   b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                   b"Content-Length: %d\r\nConnection: keep-alive\r\n\r\n" % len(body)
                   + body)
            assert second.startswith("HTTP/1.1 200"), (
                f"空闲 {IDLE_S:.0f} 秒后复用连接发 POST 没拿到响应：{second}。"
                "浏览器对 POST 不会重试，用户看到的就是 Failed to fetch")
        finally:
            s.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_fresh_connection_is_always_a_fallback() -> None:
    """换一条新连接则一定成功 —— 说明失败从来不是「接口坏了」。

    这条同时是上一条的对照：两条用例跑完都在证明「问题在连接复用，不在业务」。
    所以前端加的那次重发（`api()` 里的 retryOnce）是安全的兜底，不是掩盖问题。
    """
    port = _free_port()
    server, thread = _start_server(port)
    body = b'{"client":"qq","key":"x"}'
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            head = _one_request(
                s, b"POST /api/collect/key/check HTTP/1.1\r\n"
                   b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                   b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                   + body)
            assert head.startswith("HTTP/1.1 200"), head
        finally:
            s.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    skipped = failed = 0
    print("=" * 64)
    print(f"HTTP 连接复用：{len(tests)} 项")
    print("=" * 64)
    for fn in tests:
        try:
            fn()
        except _SkipTest as exc:
            skipped += 1
            print(f"  - 跳过 {fn.__name__}：{exc}")
        except Exception:                       # noqa: BLE001
            failed += 1
            print(f"  x 失败 {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"  + 通过 {fn.__name__}")
    print("-" * 64)
    print(f"通过 {len(tests) - failed - skipped} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
