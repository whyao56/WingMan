"""Mock ASR：不装任何语音依赖时使用。

它**故意**输出带明显标记的占位文本，而不是编造一段像模像样的"转写结果"。
因为一个假的转写会让人误以为语音链路已经通了 —— 那比报错更糟。
"""

from __future__ import annotations

import numpy as np

from .base import ASREngine, ASRResult, rms


class MockASR(ASREngine):
    name = "mock"

    @property
    def note(self) -> str:
        return "未接入真实语音识别，输出为占位文本（安装 faster-whisper 或配置云 ASR 后可用）"

    async def transcribe(
        self, pcm: np.ndarray, sample_rate: int = 16000, language: str = "zh"
    ) -> ASRResult:
        arr = np.asarray(pcm)
        n = len(arr)
        dur_ms = int(n / max(1, sample_rate) * 1000)
        energy = rms(arr)
        if energy < 0.002:
            return ASRResult(text="", language=language, duration_ms=dur_ms, confidence=0.0)
        return ASRResult(
            text=f"[模拟转写 {dur_ms / 1000:.1f}s · 未接入真实 ASR]",
            language=language,
            duration_ms=dur_ms,
            confidence=0.0,
            meta={"mock": True, "rms": energy},
        )
