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
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from .config import APP_DIR, LOG_DIR, RESOURCE_DIR, get_settings

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


def _open_browser(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}/")


def _open_window(port: int) -> bool:
    """优先开原生窗口（像个正经桌面软件），拿不到就退回浏览器。

    pywebview 依赖系统 WebView2，不是所有机器都有；失败绝不能影响使用。
    """
    if os.environ.get("WINGMAN_USE_BROWSER") == "1":
        return False
    try:
        import webview  # type: ignore
    except ImportError:
        log.info("未安装 pywebview，改用浏览器打开界面。")
        return False

    url = f"http://127.0.0.1:{port}/"
    try:
        webview.create_window(
            "WingMan · 聊天僚机",
            url,
            width=1280,
            height=860,
            min_size=(940, 640),
            text_select=True,
        )
        # 非阻塞：窗口关掉后返回，主线程据此收尾
        webview.start()
        return True
    except Exception as exc:
        log.warning("原生窗口打开失败（%s），改用浏览器。", exc)
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
    p.add_argument(
        "--asr-test",
        nargs="?",
        const="",
        default=None,
        metavar="WAV",
        help="语音链路自检：不传文件就从麦克风录 5 秒，传 WAV 就转写该文件。"
             "用来确认「麦克风能不能录到 + 模型能不能转写」",
    )
    p.add_argument("--seconds", type=float, default=5.0,
                   help="配合 --asr-test 使用：录多久（默认 5 秒）")
    p.add_argument("--source", default="microphone", choices=("microphone", "loopback"),
                   help="配合 --asr-test 使用：麦克风（听自己）还是系统回环（听对方）")
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

        if args.asr_test is not None:
            return _run_asr_test(args.asr_test, args.seconds, args.source)

        wanted = args.port or cfg.port
        host = args.host or cfg.host

        # 单实例：已经在跑就只把界面调出来，别再起一个服务
        if _probe_wingman(wanted):
            # 日志必须说实话：--no-window 下我们**不会**打开任何界面。
            # 之前这里无论什么模式都印「直接打开界面」，排查别人的问题时
            # 会让人以为是自己那个进程起的窗口，白绕一圈。
            if args.no_window:
                log.info(
                    "%s 端口上已有 WingMan 在运行，直接复用（--no-window，不打开界面）。",
                    wanted,
                )
            else:
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


def _read_wav_mono16k(path) -> tuple["object", int]:
    """读 WAV → (int16 数组, 采样率)。顺手重采样到 16k，Whisper 只认这个。"""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    arr = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000 and len(arr):
        # 线性插值够用了；这里只是自检，不值得拉进 scipy
        n = int(round(len(arr) * 16000 / sr))
        src = np.linspace(0.0, 1.0, num=len(arr), endpoint=False)
        dst = np.linspace(0.0, 1.0, num=n, endpoint=False)
        arr = np.interp(dst, src, arr.astype(np.float64)).astype(np.int16)
        sr = 16000
    return arr, sr


def _record_audio(seconds: float, source: str) -> tuple["object", int]:
    """录一段。返回 (int16 数组, 采样率)。

    直接复用 ``capture.record_once`` —— 那边的设备选择逻辑（auto / 设备 id /
    名称子串）和实时会话是同一条路径。这里自己再写一遍没有意义，
    而且一旦两边不一致，「自检说没事、实际通话不行」这种问题就会冒出来。
    """
    from .asr.capture import record_once

    pcm, sr, name = record_once(seconds=seconds, kind=source)
    if sys.stdout is not None:
        try:
            print(f"[音频] 录制设备：{name}", flush=True)
        except Exception:
            pass
    return pcm, sr


def _run_asr_test(wav: str, seconds: float, source: str = "microphone") -> int:
    """`--asr-test`：把「采集 → 转写」这条链路跑一遍，把结果给人看。

    为什么值得单独做：语音出问题时，用户完全无法分辨是**录不到声音**
    还是**模型识别不准** —— 这两件事的现象一模一样（「没反应」）。
    这个命令把中间结果摊开：设备列表、电平、耗时、识别文本。

    ``source`` 传 ``loopback`` 就能验证「听对方」这条路：
    让系统放点声音（视频/音乐），同时跑
    ``WingMan.exe --asr-test --source loopback``，看能不能识别出来。
    """
    import asyncio

    from .asr.base import rms
    from .context import get_ctx

    lines: list[str] = []
    ok = True

    def say(line: str = "") -> None:
        lines.append(line)
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except Exception:
                pass

    say(f"WingMan 语音自检  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say("=" * 62)

    # ---- 1. 设备
    try:
        from .asr.capture import list_audio_devices

        devices = list_audio_devices()
        loops = [d for d in devices if getattr(d, "kind", "") == "loopback"]
        say(f"[设备] 找到 {len(devices)} 个（回环 {len(loops)} 个）")
        for d in devices:
            tag = "回环·听对方" if getattr(d, "kind", "") == "loopback" else "麦克风·听自己"
            star = " *默认" if getattr(d, "is_default", False) else ""
            say(f"       - {getattr(d, 'name', '?')}  [{tag}]{star}")
        if not loops:
            say("       ⚠ 没有回环设备 —— 「系统声音（对方）」采集会不可用。")
            say("         Windows 上回环跟着「默认播放设备」走，先确认扬声器在响。")
    except Exception as exc:  # noqa: BLE001
        say(f"[设备] 枚举失败：{type(exc).__name__}: {exc}")
        say("       回环/麦克风采集会不可用。源码运行请 pip install soundcard。")
        ok = False

    # ---- 2. 取音频
    try:
        if wav:
            from pathlib import Path

            p = Path(wav)
            if not p.is_file():
                raise RuntimeError(f"找不到文件：{p}")
            pcm, sr = _read_wav_mono16k(p)
            say(f"[音频] 读入 {p.name}：{len(pcm) / max(1, sr):.1f}s @ {sr}Hz")
        else:
            pcm, sr = _record_audio(seconds, source)
            say(f"[音频] 录制 {len(pcm) / max(1, sr):.1f}s @ {sr}Hz"
                f"（来源：{'系统声音' if source == 'loopback' else '麦克风'}）")
        level = rms(pcm.astype("float32") / 32768.0)
        say(f"[音频] 电平 RMS = {level:.4f}"
            + ("   ← 几乎是静音！检查设备是否选对/被静音" if level < 0.005 else ""))
        if level < 0.002:
            ok = False
    except Exception as exc:  # noqa: BLE001
        say(f"[音频] 采集失败：{type(exc).__name__}: {exc}")
        ok = False
        pcm = None

    # ---- 3. 转写
    if pcm is not None:
        try:
            ctx = get_ctx()
            eng = ctx.asr
            say(f"[引擎] {getattr(eng, 'name', '?')} · {getattr(eng, 'note', '')}")
            if getattr(eng, "name", "") == "mock":
                say("       注意：这是 Mock 引擎，输出是假的，不代表识别能力。")
                ok = False
            t0 = time.time()
            res = asyncio.run(eng.transcribe(pcm, sr, language="zh"))
            cost = time.time() - t0
            say(f"[结果] {cost:.1f}s  语种={res.language}  置信度={res.confidence:.3f}")
            say(f"[文本] {res.text or '(空 —— 没听出内容)'}")
            if res.meta.get("error"):
                say(f"[报错] {res.meta['error']}")
                ok = False
            if not res.text.strip():
                ok = False
        except Exception as exc:  # noqa: BLE001
            say(f"[转写] 失败：{type(exc).__name__}: {exc}")
            ok = False

    say("=" * 62)
    say("结论：" + ("链路正常" if ok else "链路有问题，看上面带 → 的提示"))
    report = "\n".join(lines)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "asr_test.txt").write_text(report, encoding="utf-8")
        say(f"（报告已写入 {LOG_DIR / 'asr_test.txt'}）")
    except OSError:
        pass

    return 0 if ok else 1


def _run_check() -> int:
    """`--check`：不启动服务，只跑自检。

    结果同时写进 ``logs/selfcheck.txt`` —— 窗口版 exe 没有 stdout，
    用户想反馈问题只能靠这个文件。
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
