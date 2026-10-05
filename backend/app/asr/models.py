"""本地语音模型的管理：下载、进度、体积。

为什么不能直接调 ``WhisperModel("small")`` 就完事：
首次使用要下 500MB 左右，中间没有任何提示 —— 用户看到的就是「点了没反应」，
然后以为软件坏了。所以这里把「下载」变成一件看得见、可等候、可中断的事。

另一个必须处理的点是**下载源**。模型权重放在 HuggingFace 上，国内直连
经常超时。这里支持切到国内镜像（``hf-mirror.com``），不切也能用官方源。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from ..config import APP_DIR

log = logging.getLogger("wingman.asr.models")

# 模型清单。体积是单份权重的近似大小，用来算下载进度。
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "tiny":     {"mb": 78,   "note": "最快，中文准确率一般，适合先试通流程"},
    "base":     {"mb": 148,  "note": "较快，中文一般"},
    "small":    {"mb": 486,  "note": "推荐起步。中文明显更准，CPU 也能接受"},
    "medium":   {"mb": 1530, "note": "更准，但 CPU 上慢到不实用"},
    "large-v3": {"mb": 3090, "note": "最准。基本只有显卡才跑得动，CPU 上不要选"},
}

DEFAULT_SIZE = "base"
MIRROR_ENDPOINT = "https://hf-mirror.com"

# 只下推理真正需要的文件，省掉用不上的权重格式
ALLOW_PATTERNS = ["*.bin", "*.json", "*.txt", "*.model"]


def models_root() -> Path:
    """模型权重放哪。放在用户目录下，方便用户自己看体积、手动删。"""
    return APP_DIR / "models"


def model_dir(size: str) -> Path:
    """单个模型的落地目录。

    刻意用 ``local_dir`` 而不是 HuggingFace 的 cache 目录结构：
    cache 会同时留 ``blobs/`` 和 ``snapshots/`` 两份，Windows 上没开
    开发者模式时不做硬链接而是**复制**，磁盘占用直接翻倍
    （实测 tiny 本应 75MB，占了 149MB）。我们不需要 cache 的版本语义，
    直下到目标目录最省地方，路径也对用户更好读。
    """
    return models_root() / f"faster-whisper-{size}"


def dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total / 1024 / 1024


def is_installed(size: str) -> bool:
    """判断模型是否已经下全了。

    只看目录存在不够 —— 下到一半中断也会留下目录，那种情况下
    加载会失败。所以必须真的找到 ``model.bin``。
    """
    d = model_dir(size)
    if not d.is_dir():
        return False
    return (d / "model.bin").is_file() or any(p.name == "model.bin" for p in d.rglob("model.bin"))


def installed_sizes() -> list[str]:
    return [s for s in MODEL_CATALOG if is_installed(s)]


def catalog() -> list[dict[str, Any]]:
    root = models_root()
    out: list[dict[str, Any]] = []
    for size, meta in MODEL_CATALOG.items():
        d = model_dir(size)
        got = is_installed(size)
        out.append({
            "size": size,
            "mb": meta["mb"],
            "note": meta["note"],
            "installed": got,
            "on_disk_mb": round(dir_size_mb(d)) if d.exists() else 0,
        })
    return out


def library_available() -> bool:
    """faster-whisper 这个库本身在不在。

    标准版 exe 不带它（体积原因），所以要区分「库没装」和「模型没下载」——
    这两种情况的解法完全不同，不能笼统报「语音不可用」。
    """
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------- 下载


class DownloadState:
    """下载进度。放内存里就行 —— 重启了用户重下便是，不需要持久化。"""

    def __init__(self) -> None:
        self.running = False
        self.size = ""
        self.started_at = 0.0
        self.error = ""
        self.done = False
        self.endpoint = ""

    def snapshot(self) -> dict[str, Any]:
        size = self.size
        expected = float(MODEL_CATALOG.get(size, {}).get("mb", 0) or 0)
        current = dir_size_mb(model_dir(size)) if size else 0.0

        # 目录体积会因为临时文件抖动，夹一下范围再算百分比
        pct = 0.0
        if expected > 0:
            pct = max(0.0, min(99.0, current / expected * 100))
        if self.done:
            pct = 100.0
            current = current or expected

        elapsed = max(0.001, time.time() - self.started_at) if self.started_at else 0.0
        speed = (current / elapsed) if elapsed > 0 and current else 0.0
        remain = ((expected - current) / speed) if speed > 0 and not self.done else 0.0

        # 字段形状保持一致 —— 前端不该因为「还没开始」就拿到另一套结构
        return {
            "running": self.running,
            "done": self.done,
            "error": self.error,
            "error_hint": _explain_error(self.error) if self.error else "",
            "size": size,
            "downloaded_mb": round(current, 1),
            "expected_mb": round(expected, 1),
            "percent": round(pct, 1),
            "speed_mbps": round(speed, 2),
            "remain_sec": int(remain),
            "endpoint": self.endpoint,
        }


def _explain_error(err: str) -> str:
    """把底层报错翻译成「下一步该做什么」。

    这些错误信息本身（CAS / 401 / xethub）对用户毫无意义，但每种都对应
    一个明确的动作，所以必须翻译。
    """
    e = err or ""
    # 「缺组件」必须排在所有网络关键词之前判断。
    # 这是个安装包问题，给再多的网络建议都不可能修好 —— 镜像补不上一个
    # 根本没打进包的模块。而且模块名有可能碰巧命中下面的关键词
    # （比如某个模块名里带 connection），排在后面就会被翻译成
    # 「勾上国内镜像重试」，把用户带进一个永远转不出来的死胡同。
    if ("ModuleNotFoundError" in e or "ImportError" in e
            or "No module named" in e or "cannot import name" in e):
        return ("程序缺少一个必需组件，这是安装包的问题，不是你操作的问题。"
                "请到发布页重新下载安装包；若反复出现，请把上面这行原文反馈给作者。")
    if "xethub" in e or "CAS Client" in e or "Xet" in e:
        return ("下载走错了通道（Xet）。这通常发生在国内网络下，"
                "程序会自动改走经典下载；请重试一次，仍失败就换小一号的模型。")
    if "401" in e or "403" in e or "Unauthorized" in e or "Forbidden" in e:
        return "被服务器拒绝。多半是网络中间有拦截，勾上「使用国内镜像」再试。"
    if "timed out" in e.lower() or "timeout" in e.lower():
        return "连接超时。勾上「使用国内镜像」，或换个小一号的模型重试。"
    if "No space" in e or "磁盘" in e or "ENOSPC" in e:
        return "磁盘空间不够了，清一些空间再试。"
    if "Connection" in e or "getaddrinfo" in e or "Name or service" in e:
        return "连不上下载服务器。检查网络，或勾上「使用国内镜像」。"
    return "可以先重试一次；一直失败的话，勾上「使用国内镜像」或换个小一号的模型。"


STATE = DownloadState()


def apply_endpoint(endpoint: str) -> None:
    """设置下载源。必须在导入 huggingface_hub 之前生效才有用。

    这里同时关掉 Xet —— 这不是可选项，是必须的：

    ``huggingface_hub`` 默认走 HF 新的 Xet 内容寻址存储，而 Xet 的 CAS
    服务器固定在 ``cas-server.xethub.hf.co``，**国内镜像不代理它**。
    结果就是：主站域名换成了镜像，数据却仍然直连真 HF，最后 401：

        CAS Client Error: HTTP status client error (401 Unauthorized),
        domain: https://cas-server.xethub.hf.co/v2/reconstructions/...

    这个错误看起来像鉴权问题，其实是路径选错了，排查时非常容易被带偏。
    关掉 Xet 强制走经典 HTTP 下载，镜像才能真正生效。
    """
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    else:
        os.environ.pop("HF_ENDPOINT", None)


def download(size: str, endpoint: str = "") -> None:
    """阻塞式下载。调用方自己丢到线程里，别卡住事件循环。

    失败一定要把原因留下来 —— 「下载失败」这四个字对用户没有任何帮助，
    常见原因（网络、镜像、磁盘）的解法完全不同。
    """
    if size not in MODEL_CATALOG:
        raise ValueError(f"未知的模型规格：{size}")
    if not library_available():
        raise RuntimeError(
            "当前程序没有内置本地语音识别库。"
            "请使用带语音的完整版，或改用云 ASR。"
        )

    apply_endpoint(endpoint)
    root = models_root()
    root.mkdir(parents=True, exist_ok=True)

    STATE.running = True
    STATE.done = False
    STATE.error = ""
    STATE.size = size
    STATE.started_at = time.time()
    STATE.endpoint = endpoint or "huggingface.co"

    log.info("开始下载语音模型 %s（源：%s）→ %s", size, STATE.endpoint, root)
    try:
        from huggingface_hub import snapshot_download

        target = model_dir(size)
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=f"Systran/faster-whisper-{size}",
            local_dir=str(target),
            allow_patterns=ALLOW_PATTERNS,
        )
        if not is_installed(size):
            raise RuntimeError("下载结束但没有找到模型文件，可能被中断了，请重试。")
        STATE.done = True
        log.info("语音模型 %s 下载完成（%.0f MB）", size, dir_size_mb(target))
    except Exception as exc:
        STATE.error = f"{type(exc).__name__}: {exc}"[:400]
        log.exception("下载语音模型失败")
    finally:
        STATE.running = False


def delete(size: str) -> bool:
    """删掉模型，把磁盘空间还回去。"""
    d = model_dir(size)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def on_progress(cb: Callable[[dict[str, Any]], None]) -> None:  # pragma: no cover
    """预留：将来换成真正的流式回调钩子。目前用轮询 STATE 即可。"""
    cb(STATE.snapshot())
