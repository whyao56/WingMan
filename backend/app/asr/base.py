"""ASR 抽象与音频工具。"""

from __future__ import annotations

import io
import struct
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class ASRResult:
    text: str = ""
    language: str = "zh"
    duration_ms: int = 0
    confidence: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


# ---------------------------------------------------------------- 音频工具


def pcm_to_int16(pcm: np.ndarray) -> np.ndarray:
    """把 float32 [-1,1] 的采样转成 int16。已经是 int16 就原样返回。"""
    arr = np.asarray(pcm)
    if arr.dtype == np.int16:
        return arr
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    arr = np.clip(arr.astype(np.float32), -1.0, 1.0)
    return (arr * 32767.0).astype(np.int16)


def wav_bytes(pcm: np.ndarray, sample_rate: int, channels: int = 1) -> bytes:
    """打包成 WAV 字节流 —— 绝大多数云 ASR 接口都吃这个。"""
    data = pcm_to_int16(pcm)
    if data.ndim > 1:
        channels = data.shape[1]
        data = data.reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(max(1, channels))
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(data.tobytes())
    return buf.getvalue()


def wav_header(byte_len: int, sample_rate: int, channels: int = 1) -> bytes:
    """手工构造 44 字节 WAV 头，用于流式场景（避免写临时文件）。"""
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + byte_len, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        byte_rate, block_align, bits,
        b"data", byte_len,
    )


def rms(pcm: np.ndarray) -> float:
    """均方根能量，0~1。VAD 和电平显示都用它。"""
    if pcm is None or len(pcm) == 0:
        return 0.0
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    return float(np.sqrt(np.mean(arr * arr)))


# ---------------------------------------------------------------- 抽象


class ASREngine(ABC):
    name: str = "base"
    available: bool = True
    note: str = ""

    @abstractmethod
    async def transcribe(
        self, pcm: np.ndarray, sample_rate: int = 16000, language: str = "zh"
    ) -> ASRResult:
        """pcm 是单通道 float32（-1~1）或 int16 数组。"""

    def supports_stream(self) -> bool:
        return False
