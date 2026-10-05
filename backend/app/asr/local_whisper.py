"""本地 faster-whisper 转写。

隐私最好的方案：**音频一帧都不出本机**。
代价是要装依赖、要等模型下载、CPU 上会慢一些。

安装：
    pip install faster-whisper
模型大小建议：
    tiny   —— 最快，中文准确率一般
    base   —— 速度与准确率的平衡点
    small  —— 推荐起步（中文明显更准）
    medium —— 准但慢，CPU 上不实用
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import numpy as np

from .base import ASREngine, ASRResult, to_float_mono

log = logging.getLogger("wingman.asr.whisper")

# Whisper 有个中文特有的毛病：**同一个模型会时而不时地吐繁体**。
# 实测 base 把「今天加班到10点,好累呀,你周末有空吗?」写成
# 「今天加班到10點,好累呀,你周末有空嗎?」—— 内容是对的，字形是错的，
# 而用户一眼就能看出来，还会以为程序认错了字。
#
# 给一段简体中文的 initial_prompt 就能把它带回简体（同一段音频实测恢复成
# 「今天加班到10点,好累呀。你周末有空吗?」）。这比引一个繁简转换库更根本：
# 转换库只是事后补救，而它是从源头就不让模型往繁体跑，还能顺手把语气
# 定在普通话上。
#
# 只在明确是中文时加。副作用是 beam search 路径会变，个别用词会有出入
# （tiny 会把「10点」写成「十点」，同样正确），可以接受。
ZH_SIMPLIFIED_PROMPT = "以下是普通话的句子，请用简体中文转写。"

_model_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str], Any] = {}


class LocalWhisperASR(ASREngine):
    name = "local"

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._load_error: str | None = None

    @property
    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def note(self) -> str:
        if not self.available:
            return "未安装 faster-whisper（当前程序未内置本地语音识别）"
        try:
            from .models import is_installed

            if not is_installed(self.model_size):
                return f"本地 faster-whisper · 模型 {self.model_size} 尚未下载"
        except Exception:
            pass
        return f"本地 faster-whisper · {self.model_size} · {self.device}/{self.compute_type}"

    @property
    def ready(self) -> bool:
        """库在 **且** 权重已下载，才叫真的能用。缺一个都转写不了。"""
        if not self.available:
            return False
        try:
            from .models import is_installed

            return is_installed(self.model_size)
        except Exception:
            return False

    @property
    def not_ready_reason(self) -> str:
        if not self.available:
            return "这个程序没有内置 faster-whisper 语音识别库"
        try:
            from .models import installed_sizes, is_installed

            if not is_installed(self.model_size):
                have = installed_sizes()
                return (
                    f"模型 {self.model_size} 还没下载"
                    + (f"（已下载：{'、'.join(have)}）" if have else "")
                )
        except Exception:
            return "无法确认模型状态"
        return ""

    # ------------------------------------------------------ 模型

    def _get_model(self) -> Any:
        key = (self.model_size, self.device, self.compute_type)
        with _model_lock:
            if key in _model_cache:
                return _model_cache[key]
            from faster_whisper import WhisperModel

            from .models import is_installed, model_dir

            # 没下模型就直接加载会静默卡住几分钟，用户只会觉得「死了」。
            # 明确报错，让他去界面点「下载模型」。
            if not is_installed(self.model_size):
                raise RuntimeError(
                    f"本地模型 {self.model_size} 还没下载。"
                    f"请到「设置 → 语音识别」里点「下载」，"
                    f"或改用云端 ASR。预期位置：{model_dir(self.model_size)}"
                )

            path = model_dir(self.model_size)
            log.info("加载本地 whisper 模型 %s ← %s", self.model_size, path)
            # 传本地目录而不是模型名：这样完全不碰 HuggingFace，
            # 离线也能用，也不会意外触发下载。
            model = WhisperModel(
                str(path),
                device=self.device,
                compute_type=self.compute_type,
            )
            _model_cache[key] = model
            return model

    # ------------------------------------------------------ 转写

    async def transcribe(
        self, pcm: np.ndarray, sample_rate: int = 16000, language: str = "zh"
    ) -> ASRResult:
        # 统一规整：兼容 int16、以及「int16 量级的 float32」这类错误输入
        arr = to_float_mono(pcm)
        dur_ms = int(len(arr) / max(1, sample_rate) * 1000)
        if len(arr) < sample_rate * 0.2:      # 短于 200ms 基本是噪声
            return ASRResult(text="", language=language, duration_ms=dur_ms)

        def _run() -> tuple[str, str, float]:
            model = self._get_model()
            # 只有明确说了是中文才加简体提示词。
            # language 为 auto/空 时是在做语种识别，此时塞一段中文进去会
            # 把语种判断本身带偏 —— 宁可偶尔出繁体，也不要认错语种。
            prompt = (
                ZH_SIMPLIFIED_PROMPT
                if (language or "").strip().lower().startswith("zh")
                else None
            )
            segments, info = model.transcribe(
                arr,
                language=None if language in ("auto", "") else language,
                beam_size=3,
                vad_filter=False,          # 外层已经做过 VAD 了
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            parts: list[str] = []
            probs: list[float] = []
            for seg in segments:
                txt = (seg.text or "").strip()
                if txt:
                    parts.append(txt)
                    probs.append(float(getattr(seg, "avg_logprob", 0.0)))
            import math

            conf = 0.0
            if probs:
                conf = max(0.0, min(1.0, math.exp(sum(probs) / len(probs))))
            return "".join(parts).strip(), getattr(info, "language", language), conf

        try:
            text, lang, conf = await asyncio.to_thread(_run)
        except Exception as exc:
            log.exception("本地转写失败")
            return ASRResult(
                text="", language=language, duration_ms=dur_ms,
                meta={"error": str(exc)[:200]},
            )
        return ASRResult(text=text, language=lang, duration_ms=dur_ms, confidence=conf)

    def supports_stream(self) -> bool:
        # faster-whisper 支持流式，但需要维护跨段的上下文状态。
        # 脚手架先走「整段转写」，见 ROADMAP 阶段 1.3。
        return False
