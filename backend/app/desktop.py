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
from typing import Any

from .config import APP_DIR, KEEP_ALIVE_S, LOG_DIR, RESOURCE_DIR, get_settings

log = logging.getLogger("wingman.desktop")

DEFAULT_PORT = 8787
# 从 8787 起往后试几个，避免和别的程序撞车
PORT_SCAN_RANGE = 20


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
    "hide_ok": False,      # 这个窗口对象支不支持 hide（拿不到就当不支持）
}
_WINDOW: Any = None
_STOP = threading.Event()


def _ask_frontend_to_choose() -> bool:
    """让页面弹出「关闭程序 / 后台运行」的小窗。成功返回 True。"""
    if _WINDOW is None:
        return False
    try:
        _WINDOW.evaluate_js("window.__wingmanAskClose && window.__wingmanAskClose()")
        return True
    except Exception as exc:      # pragma: no cover - 依赖 pywebview 运行时
        log.warning("通知界面弹出关闭选项失败：%s", exc)
        return False


def _on_closing() -> bool:
    """pywebview 的关窗钩子。返回 False = 取消这次关闭。

    已在等用户选择时（`pending`）再点一次 × → 直接放行，别把人卡死在
    一个弹不出来的窗口里 —— 那种「× 点不动」的观感比任何设计失误都糟。
    """
    if _CLOSE["pending"]:
        log.info("已在等待用户选择，这次直接关闭。")
        return True
    if _ask_frontend_to_choose():
        _CLOSE["pending"] = True
        log.info("已拦下关闭请求，等用户选择「关闭程序 / 后台运行」。")
        return False
    log.info("界面没法弹窗（没开原生窗口），按「关闭程序」处理。")
    return True


def close_action(action: str) -> dict[str, Any]:
    """界面里选完之后回调这里。`action`：quit | background | cancel。"""
    _CLOSE["pending"] = False
    action = (action or "").strip()

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

    # quit：先让 uvicorn 收工，再结束进程。
    # 直接 `os._exit` 会跳过 uvicorn 的优雅退出（正在写的库可能被打断），
    # 所以先给服务一个退出的信号，等一小会儿再兜底强退。
    log.info("用户选择关闭程序，准备退出。")
    _STOP.set()
    threading.Thread(target=_force_exit_soon, name="wingman-exit", daemon=True).start()
    return {"ok": True, "action": "quit", "hint": "正在关闭…"}


def _force_exit_soon(delay: float = 1.2) -> None:      # pragma: no cover - 进程收尾
    time.sleep(delay)
    try:
        if _WINDOW is not None:
            _WINDOW.destroy()
    except Exception:
        pass
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
    """界面用来自查「有没有原生窗口」——决定 × 按钮该怎么说话。"""
    return {
        "native_window": _WINDOW is not None,
        "pending_close": _CLOSE["pending"],
        "platform": sys.platform,
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
        log.info("界面已关闭，退出。")
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
