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

from .base import ASREngine, ASRResult

log = logging.getLogger("chatwing.asr.whisper")

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
            return "未安装 faster-whisper（pip install faster-whisper）"
        return f"本地 faster-whisper · {self.model_size} · {self.device}/{self.compute_type}"

    # ------------------------------------------------------ 模型

    def _get_model(self) -> Any:
        key = (self.model_size, self.device, self.compute_type)
        with _model_lock:
            if key in _model_cache:
                return _model_cache[key]
            from faster_whisper import WhisperModel

            log.info("加载 whisper 模型 %s（首次会下载，请稍候）…", self.model_size)
            model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
            _model_cache[key] = model
            return model

    # ------------------------------------------------------ 转写

    async def transcribe(
        self, pcm: np.ndarray, sample_rate: int = 16000, language: str = "zh"
    ) -> ASRResult:
        arr = np.asarray(pcm)
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32) / (32768.0 if arr.dtype == np.int16 else 1.0)
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        dur_ms = int(len(arr) / max(1, sample_rate) * 1000)
        if len(arr) < sample_rate * 0.2:      # 短于 200ms 基本是噪声
            return ASRResult(text="", language=language, duration_ms=dur_ms)

        def _run() -> tuple[str, str, float]:
            model = self._get_model()
            segments, info = model.transcribe(
                arr,
                language=None if language in ("auto", "") else language,
                beam_size=3,
                vad_filter=False,          # 外层已经做过 VAD 了
                condition_on_previous_text=False,
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
