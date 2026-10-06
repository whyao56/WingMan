"""桌面启动器：把「服务 + 界面 + 出错能看」包装成双击就能用的东西。

exe 的用户和源码用户最大的区别是：**他没有控制台**。所以这里的每一条
错误路径都必须有出口 —— 要么弹窗告诉他人话，要么写进日志并告诉他日志在哪。
直接 ``raise`` 在打包版里等于「双击没反应」，是最糟的失败方式。

启动顺序：
    端口探测 → 单实例检查（已开着就只把界面调出来）→ 起服务线程
    → 等健康检查通过 → 开界面窗口 → 守着直到退出
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from typing import Any

from . import __version__
from .config import APP_DIR, IS_FROZEN, KEEP_ALIVE_S, LOG_DIR, RESOURCE_DIR, get_settings

log = logging.getLogger("wingman.desktop")

# `python -m app.desktop` 会以 `__main__` 的名字执行本文件，而
# `app.api.routes_admin` 又会 `from .. import desktop` 把同一份代码**再导入一次** ——
# 于是 `_WINDOW` / `_CLOSE` / `_SERVER` 在内存里变成两套：主流程写的是 `__main__`
# 那份，接口读的是 `app.desktop` 那份（永远是初始值）。症状很隐蔽：
# 窗口明明开着，`/api/desktop/state` 却说 `native_window: false`，
# 界面于是按「浏览器模式」说话；用户点「后台运行」时后端读到一个 None 窗口，
# 退化成「服务继续跑、窗口不隐藏」，还回一句「已关闭」。
# 提前把自己登记到 `app.desktop` 名下，后续任何导入拿到的都是这一份。
# （exe 与 `python run_wingman.py` 走的是 `from app.desktop import main`，
#  `__name__` 本来就是 `app.desktop`，这里不会触发。）
if __name__ == "__main__":      # pragma: no cover - 只有直接运行时才成立
    sys.modules.setdefault("app.desktop", sys.modules[__name__])

DEFAULT_PORT = 8787
# 从 8787 起往后试几个，避免和别的程序撞车
PORT_SCAN_RANGE = 20

# 这个**进程**是什么时候起来的。存在的理由很具体：界面是每次从磁盘现读的，
# 服务却是一个一直在跑的进程。升级完程序忘了重启，就会出现「界面是新的、
# 版本号是旧的」——用户会以为是版本号写错了。有了它，那种情况一眼可辨。
_STARTED_AT = time.time()


# ---------------------------------------------------------------- 小工具


def _free_port(start: int, tries: int = PORT_SCAN_RANGE) -> int:
    """从 start 开始找一个能绑上的端口。"""
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"从 {start} 起连续 {tries} 个端口都被占用了，请关掉一些程序再试。")


def _probe_wingman(port: int, timeout: float = 1.5) -> bool:
    """这个端口上跑的**是不是我们**。不能只看端口通不通。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # 局部关掉代理：环境里配了 http_proxy 时，访问 127.0.0.1 会被劫持
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read(400).decode("utf-8", "ignore")
        return '"counts"' in body and '"version"' in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait_healthy(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe_wingman(port):
            return True
        time.sleep(0.35)
    return False


def _ask_running_instance_to_show(port: int, timeout: float = 3.0) -> bool:
    """请**已经在跑的那个实例**把它自己的窗口显示出来。成功返回 True。

    为什么不是「在这里再开一个窗口」：服务只在一个进程里。第二个进程开出来的
    窗口只是一个空壳 —— 页面的请求都打到老进程的服务上，于是「关闭程序」
    关掉的是新壳，老进程连服务带窗口继续活着，用户看到的是「点了没反应」。
    「后台运行」藏起来的那个窗口本来就是老进程的，让它自己 show 回来才对。
    """
    url = f"http://127.0.0.1:{port}/api/desktop/show"
    try:
        # 和 `_probe_wingman` 一样局部关掉代理：http_proxy 会劫持 127.0.0.1
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url, method="POST", data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode("utf-8", "ignore")).get("shown"))
    except Exception as exc:
        log.info("让已有实例显示窗口失败（%s），改为新开一个窗口。", exc)
        return False


def _show_error(title: str, message: str) -> None:
    """原生弹窗。没有控制台时，这是唯一能让用户看见的失败方式。"""
    text = f"{message}\n\n日志文件：{LOG_DIR / 'wingman.log'}"
    log.error("%s | %s", title, message)
    if sys.platform == "win32":
        try:
            # 0x10 = MB_ICONERROR
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # type: ignore[attr-defined]
            return
        except Exception:
            pass
    print(f"[{title}] {text}", file=sys.stderr)


# ---------------------------------------------------------------- 界面


# ---------------------------------------------------------------- 关窗协商
#
# 需求 9：点窗口右上角的 × 时，不要直接退 —— 弹一个小窗让用户选
# 「关闭程序」还是「关掉窗口、后台继续跑」。
#
# 为什么必须问：关掉窗口 = 关掉服务，正在跑的采集/分析会**静默中断**，
# 用户下次打开才发现少了一段。而「只是想把它挪到一边」的人也不少。
#
# 桥怎么搭：`closing` 事件在 Python 侧触发，但选择要在界面里做，
# 所以这里把关闭**拦下来**（返回 False），再让页面把对话框弹出来；
# 页面拿到用户的选择后回调 `/api/desktop/close`。
# 每条路径都有兜底，不允许出现「点了 × 什么都没发生」。

_CLOSE = {
    "pending": False,      # 已经拦下了一次关闭，正在等用户选
    "quitting": False,     # 用户已选「关闭程序」，此后所有关闭一律放行
}
_WINDOW: Any = None
# uvicorn 的 Server 对象与它的线程。留在这里是为了「关闭程序」时能真的
# 让它收工 —— 见 `_ask_server_to_stop`。
_SERVER: dict[str, Any] = {"server": None, "thread": None}


def _ask_frontend_to_choose(timeout: float = 5.0) -> bool:
    """让页面弹出「关闭程序 / 后台运行」的小窗。成功返回 True。

    **必须从非 UI 线程调用。** `evaluate_js` 是同步阻塞的（内部
    `semaphore.acquire()` 等一个只能由 UI 线程执行的回调），在 UI 线程上
    调它必然互等死锁 —— 见 `_on_closing`。

    再套一层超时：万一界面那边真的卡住了，也不能把用户永远留在
    「窗口不动」的状态里。到点返回 False，调用方按「直接关闭」处理。
    """
    if _WINDOW is None:
        return False

    done = threading.Event()
    ok = {"v": False}

    def _call() -> None:      # pragma: no cover - 依赖 pywebview 运行时
        try:
            _WINDOW.evaluate_js("window.__wingmanAskClose && window.__wingmanAskClose()")
            ok["v"] = True
        except Exception as exc:
            log.warning("通知界面弹出关闭选项失败：%s", exc)
        finally:
            done.set()

    threading.Thread(target=_call, name="wingman-ask-close", daemon=True).start()
    if not done.wait(timeout):
        log.warning("通知界面超时（%.1fs 没返回），按「直接关闭」处理。", timeout)
        return False
    return ok["v"]


def _on_closing() -> bool:
    """pywebview 的关窗钩子。返回 False = 取消这次关闭。

    ⚠️ 这个函数跑在 **UI 线程**上（winforms 的 `FormClosing` 事件回调），
    所以这里**绝不允许出现同步的 `evaluate_js`**。

    0.1.5 及以前正是那么写的：本函数直接调 `_ask_frontend_to_choose()`
    → `evaluate_js` → 内部 `semaphore.acquire()` 死等一个**必须由 UI 线程
    执行**的回调（`ContinueWith(..., syncContextTaskScheduler)`）。
    而 UI 线程正卡在本函数里，两边互等 —— 后果是**点一下 × 必卡死**：
    标题变成「WingMan 未响应」，CPU 纹丝不动（实测 8 秒内一个 tick 都没动，
    `IsHungAppWindow` 判真）。

    正确做法：只置标志，把「通知界面」丢给后台线程，立刻返回。
    """
    if _CLOSE["quitting"]:
        log.info("正在退出，放行这次关闭。")
        return True
    if _CLOSE["pending"]:
        # 再点一次 × → 直接放行。别把人卡在一个弹不出来的窗口里 ——
        # 那种「× 点不动」的观感比任何设计失误都糟。
        log.info("已在等用户选择，这次直接关闭。")
        return True
    _CLOSE["pending"] = True
    threading.Thread(
        target=_ask_frontend_or_close, name="wingman-close-ask", daemon=True
    ).start()
    return False


def _ask_frontend_or_close() -> None:
    """后台线程：请界面弹窗；弹不出来就自己把窗口关掉。

    后面这一刀是必需的。`_on_closing` 已经返回 False（取消了这次关闭），
    如果通知界面又失败，用户再点 × 也还是关不掉 —— 就成了「点了没反应」。
    """
    if _ask_frontend_to_choose():
        log.info("已拦下关闭请求，等用户选择「关闭程序 / 后台运行」。")
        return
    log.info("界面接不住关闭请求，直接关闭窗口。")
    _CLOSE["pending"] = False
    _CLOSE["quitting"] = True      # 让 destroy 触发的 _on_closing 放行
    _destroy_window()


def close_action(action: str) -> dict[str, Any]:
    """界面里选完之后回调这里。`action`：quit | background | cancel。"""
    action = (action or "").strip()
    _CLOSE["pending"] = False

    if action == "cancel":
        return {"ok": True, "action": "cancel", "hint": "已取消，程序继续运行。"}

    if action == "background":
        if _WINDOW is not None:
            try:
                _WINDOW.hide()
                log.info("窗口已隐藏，服务继续在后台运行。")
                return {
                    "ok": True, "action": "background",
                    "hint": "窗口已隐藏，服务继续在后台跑。想把它调回来，再双击一次 WingMan 就行。",
                }
            except Exception as exc:
                log.warning("隐藏窗口失败（%s），改为退出。", exc)
        return {
            "ok": True, "action": "quit",
            "hint": "浏览器模式没有可隐藏的窗口 —— 关掉这个标签页即可，服务本来就在后台跑。",
        }

    # quit：先让 uvicorn 收工，再关窗口，最后才兜底强退。
    log.info("用户选择关闭程序，准备退出。")
    _CLOSE["quitting"] = True
    _ask_server_to_stop()
    threading.Thread(target=_quit_soon, name="wingman-exit", daemon=True).start()
    return {"ok": True, "action": "quit", "hint": "正在关闭…"}


def _ask_server_to_stop() -> None:
    """告诉 uvicorn 收工。

    0.1.5 及以前这里只有一个 `_STOP.set()`，而那个 Event **没有任何消费者** ——
    注释写着「先让 uvicorn 收工」，实际 uvicorn 根本没收到信号，整个退出完全靠
    1.2 秒后 `os._exit(0)` 硬切。正在写的库随时可能被掐断，WAL 也来不及 checkpoint。
    现在是真的通知到它了。
    """
    server = _SERVER.get("server")
    if server is None:
        return
    try:
        server.should_exit = True
        log.info("已通知服务收工（uvicorn should_exit）。")
    except Exception as exc:      # pragma: no cover - 依赖 uvicorn 内部属性
        log.warning("通知服务收工失败：%s", exc)


def _wait_server_stop(timeout: float) -> bool:
    """等 uvicorn 线程结束。返回它是否真的结束了。"""
    t = _SERVER.get("thread")
    if t is None:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not t.is_alive():
            return True
        time.sleep(0.05)
    return False


def _destroy_window() -> None:
    """关掉原生窗口。从任何线程调都行 —— pywebview 自己会转到 UI 线程。"""
    global _WINDOW
    win = _WINDOW
    if win is None:
        return
    try:
        win.destroy()
    except Exception as exc:      # pragma: no cover - 依赖 pywebview 运行时
        log.warning("关闭窗口失败：%s", exc)
    _WINDOW = None


def _quit_soon(grace: float = 4.0) -> None:      # pragma: no cover - 进程收尾
    """收尾：先让服务体面收工，再关窗口，最后兜底强退。

    **顺序不能反。** 窗口一关，主线程就从 `webview.start()` 返回、一路走到
    进程退出；那时 uvicorn 若还在写库，就是被硬生生掐断的。宁可让用户多看
    一两秒「正在关闭…」，也要先把该落的盘落完。
    """
    if _wait_server_stop(grace):
        log.info("服务已收工。")
    else:
        log.warning("服务 %.0fs 内没收工，仍然继续关闭。", grace)
    _destroy_window()
    # 窗口关掉后主线程会自己走完收尾；这里只是兜底，
    # 防止某个非守护线程或 pywebview 回调把退出卡住。
    time.sleep(1.5)
    log.info("兜底强退。")
    os._exit(0)


def show_window() -> dict[str, Any]:
    """把被「后台运行」藏起来的窗口调回来。

    没有原生窗口（浏览器模式）时 `shown=False` —— 界面文案要让用户知道
    去浏览器打开，而不是傻等一个不会出现的窗口。
    """
    if _WINDOW is None:
        return {
            "ok": True, "shown": False,
            "hint": "当前没有原生窗口（浏览器模式）——服务本来就在后台跑，"
                    "用浏览器打开 http://127.0.0.1:8787/ 即可。",
        }
    try:
        _WINDOW.show()
        log.info("窗口已重新显示。")
        return {"ok": True, "shown": True, "hint": "窗口已调回来。"}
    except Exception as exc:      # pragma: no cover - 依赖 pywebview 运行时
        log.warning("显示窗口失败：%s", exc)
        return {"ok": False, "shown": False, "error": str(exc)}


def desktop_state() -> dict[str, Any]:
    """界面用来自查「有没有原生窗口」——决定 × 按钮该怎么说话。

    顺便交代**这个进程的身份**：版本、启动时刻、打包版还是源码运行。
    打包版和源码版跑同一份前端，光看界面分不出来；而「我明明升级过了，
    怎么还是旧版本」这种疑问，答案通常就是「那是个没退干净的老进程」。
    有了 `started_at`，用户看到的时间和自己的操作一对，就能自己得出结论，
    不用来问人。
    """
    return {
        "native_window": _WINDOW is not None,
        "pending_close": _CLOSE["pending"],
        "quitting": _CLOSE["quitting"],
        "platform": sys.platform,
        "version": __version__,
        "started_at": datetime.fromtimestamp(_STARTED_AT).astimezone().isoformat(timespec="seconds"),
        "uptime_seconds": int(max(0, time.time() - _STARTED_AT)),
        "mode": "exe" if IS_FROZEN else "source",
        "pid": os.getpid(),
    }


def _open_browser(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}/")


def _open_window(port: int) -> bool:
    """优先开原生窗口（像个正经桌面软件），拿不到就退回浏览器。

    pywebview 依赖系统 WebView2，不是所有机器都有；失败绝不能影响使用。
    """
    global _WINDOW
    if os.environ.get("WINGMAN_USE_BROWSER") == "1":
        return False
    try:
        import webview  # type: ignore
    except ImportError:
        log.info("未安装 pywebview，改用浏览器打开界面。")
        return False

    url = f"http://127.0.0.1:{port}/"
    try:
        window = webview.create_window(
            "WingMan · 聊天僚机",
            url,
            width=1280,
            height=860,
            min_size=(940, 640),
            text_select=True,
        )
        _WINDOW = window
        try:
            window.events.closing += _on_closing
        except Exception as exc:      # pragma: no cover - 老版本 pywebview 没有 events
            log.warning("注册关窗钩子失败（%s），× 将直接退出。", exc)
        # 非阻塞：窗口关掉后返回，主线程据此收尾
        webview.start()
        return True
    except Exception as exc:
        log.warning("原生窗口打开失败（%s），改用浏览器。", exc)
        _WINDOW = None
        return False


# ---------------------------------------------------------------- 服务


def _run_server(port: int, host: str, log_level: str) -> threading.Thread:
    import uvicorn

    # 冻结态不要用 import string（"app.main:app"），直接给对象更稳
    from .main import app

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        log_config=None,       # 复用我们自己的 logging 配置
        loop="asyncio",
        # 见 `config.KEEP_ALIVE_S`：默认的 5 秒会让浏览器把请求写进一条
        # 服务端已经收掉的连接，界面上表现为英文 `Failed to fetch`。
        timeout_keep_alive=KEEP_ALIVE_S,
    )
    server = uvicorn.Server(config)

    def _target() -> None:
        try:
            server.run()
        except Exception:
            log.exception("服务线程异常退出")

    t = threading.Thread(target=_target, name="wingman-server", daemon=True)
    # 留一份引用：退出时要靠 `server.should_exit` 让它体面收工，
    # 靠 `t.is_alive()` 判断它收干净了没有。
    _SERVER["server"] = server
    _SERVER["thread"] = t
    t.start()
    return t


# ---------------------------------------------------------------- 入口


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="wingman", description="WingMan · 聊天僚机")
    p.add_argument("--port", type=int, default=None, help="指定端口（默认 8787，占用则自动往后找）")
    p.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，仅本机可访问）")
    p.add_argument("--no-window", action="store_true", help="不开界面窗口，只起服务")
    p.add_argument("--browser", action="store_true", help="强制用浏览器而不是原生窗口")
    p.add_argument("--check", action="store_true", help="只做启动自检并打印，然后退出")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = get_settings()

    if args.browser:
        os.environ["WINGMAN_USE_BROWSER"] = "1"

    from .main import _setup_logging  # 复用同一套日志配置

    _setup_logging(cfg.log_level)

    log.info("=" * 62)
    log.info("WingMan 桌面版启动")
    log.info("资源目录：%s", RESOURCE_DIR)
    log.info("数据目录：%s", APP_DIR)
    log.info("=" * 62)

    try:
        if args.check:
            return _run_check()

        wanted = args.port or cfg.port
        host = args.host or cfg.host

        # 单实例：已经在跑就**把原来那个窗口调出来**，别再起一个服务。
        if _probe_wingman(wanted):
            # 日志必须说实话：--no-window 下我们**不会**打开任何界面。
            # 之前这里无论什么模式都印「直接打开界面」，排查别人的问题时
            # 会让人以为是自己那个进程起的窗口，白绕一圈。
            if args.no_window:
                log.info(
                    "%s 端口上已有 WingMan 在运行，直接复用（--no-window，不打开界面）。",
                    wanted,
                )
                return 0
            # 「后台运行」把窗口藏起来了 —— 再双击一次要的是「把它调回来」，
            # 而不是再起一个壳。那个壳没有服务，关它不会停掉真正在跑的那个进程。
            if _ask_running_instance_to_show(wanted):
                log.info("已把后台运行的窗口调回来。")
                return 0
            log.info("检测到 %s 端口上已有 WingMan 在运行，直接打开界面。", wanted)
            if not _open_window(wanted):
                _open_browser(wanted)
            return 0

        port = _free_port(wanted)
        if port != wanted:
            log.warning("端口 %s 被占用，改用 %s。", wanted, port)

        _run_server(port, host, cfg.log_level)

        if not _wait_healthy(port):
            _show_error(
                "WingMan 启动失败",
                "服务在 40 秒内没有就绪。最常见的原因是端口被占用或依赖缺失。\n"
                "请把日志文件发出来定位。",
            )
            return 1

        url = f"http://127.0.0.1:{port}/"
        log.info("服务已就绪：%s", url)

        if args.no_window:
            # 无界面模式：守着别退出，Ctrl+C 结束
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                log.info("收到中断，退出。")
            return 0

        opened_native = _open_window(port)
        if not opened_native:
            _open_browser(port)
            log.info("界面已在浏览器打开，关闭本窗口会同时关闭服务。")
            # 浏览器模式下没有「关窗」信号，只能挂住
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            _ask_server_to_stop()
            _wait_server_stop(4.0)
            log.info("收到中断，退出。")
            return 0

        # 原生窗口关掉了。可能来自三条路：用户选了「关闭程序」、通知界面失败后
        # 我们主动关的、或者用户连点了两次 ×。无论哪条，都要等服务收干净再走 ——
        # 主线程一返回，进程就开始退，守护线程会连同它正在写的库一起被掐掉。
        log.info("界面已关闭，等后台服务收工…")
        _ask_server_to_stop()
        if not _wait_server_stop(4.0):
            log.warning("服务 4s 内没收工，进程仍将退出。")
        log.info("已退出。")
        return 0

    except Exception as exc:  # 兜底：任何异常都要让用户看见，不许静默退出
        _show_error("WingMan 启动出错", f"{type(exc).__name__}: {exc}")
        log.exception("启动失败")
        return 1


def _run_check() -> int:
    """`--check`：不启动服务，只跑自检。

    结果同时写进**数据目录**的 ``logs/selfcheck.txt``
    （``%LOCALAPPDATA%\\WingMan\\logs``）—— 窗口版 exe 没有 stdout，
    用户想反馈问题只能靠这个文件。

    这里特意点明「数据目录」：早先几处文案写成「程序目录」，用户照着去找不到，
    容易以为自检根本没跑起来 —— 一个找不到的文件比没有文件更让人困惑。
    """
    from .context import get_ctx

    ctx = get_ctx()
    result = ctx.self_check()

    lines: list[str] = []
    lines.append(f"WingMan 自检报告  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"结论：{result['summary']}")
    lines.append("-" * 62)
    icon = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}
    for item in result["items"]:
        lines.append(f"{icon.get(item['status'], '[?]   ')} {item['label']}：{item['detail']}")
        if item.get("fix"):
            lines.append(f"          → {item['fix']}")
    lines.append("-" * 62)
    report = "\n".join(lines)

    if sys.stdout is not None:
        try:
            print(report)
        except Exception:
            pass

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "selfcheck.txt").write_text(report, encoding="utf-8")
        log.info("自检报告已写入 %s", LOG_DIR / "selfcheck.txt")
    except OSError:
        pass

    return 1 if result["worst"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
