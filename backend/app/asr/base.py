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


def to_float_mono(pcm: np.ndarray, *, scale_guard: bool = True) -> np.ndarray:
    """把各种常见形式的 PCM 规整成单通道 float32，值域 ±1.0。

    为什么要一个专门的函数：**「int16 量级的 float32」是个静默陷阱**。
    调用方先 ``np.frombuffer(raw, np.int16).astype(np.float32)`` 再传进来，
    dtype 是 float32 但数值还在 ±32768 —— 如果只按 dtype 判断要不要归一化，
    就会把放大三万倍的波形喂给模型，输出一堆乱码，而且**不报任何错**。

    所以这里不只看 dtype，还用峰值兜底：float32 但峰值明显超过 1.5 的，
    一律按 int16 量级处理。
    """
    arr = np.asarray(pcm)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)

    if arr.dtype == np.int16:
        return (arr.astype(np.float32) / 32768.0)
    if arr.dtype in (np.int32, np.int64):
        return (arr.astype(np.float32) / 2147483648.0)

    out = arr.astype(np.float32)
    if scale_guard and out.size:
        peak = float(np.max(np.abs(out)))
        # 正常的浮点音频不会超过 1.0 太多；超了基本就是没归一化
        if peak > 1.5:
            out = out / (32768.0 if peak > 256 else 127.0)
    return out


# ---------------------------------------------------------------- 抽象


class ASREngine(ABC):
    name: str = "base"
    available: bool = True
    note: str = ""

    @property
    def ready(self) -> bool:
        """现在这一刻**真的能转写**吗？

        和 ``available`` 的区别很关键，别混用：

        - ``available`` = 「这个引擎对象构造得出来」。对本地 whisper 来说，
          只要 ``faster-whisper`` 库在，它就是 True —— 哪怕模型权重一个字节
          都还没下载。
        - ``ready`` = 「现在拿一段音频进来就能出字」。

        自检面板必须看 ``ready``。只看 ``available`` 会把「模型还没下载」
        报成绿色的「已完成」，用户配完发现没反应又不知道卡在哪 ——
        这正是本项目最想消灭的那类问题。
        """
        return bool(self.available)

    @property
    def not_ready_reason(self) -> str:
        """``ready`` 为假时，用人话说明差在哪。自检面板直接展示这句。"""
        return ""

    @abstractmethod
    async def transcribe(
        self, pcm: np.ndarray, sample_rate: int = 16000, language: str = "zh"
    ) -> ASRResult:
        """pcm 是单通道 float32（-1~1）或 int16 数组。"""

    def supports_stream(self) -> bool:
        return False
