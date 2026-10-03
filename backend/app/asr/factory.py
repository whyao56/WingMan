"""按配置构造 ASR 引擎，永远返回可用实例。"""

from __future__ import annotations

import logging
from typing import Any

from .base import ASREngine
from .cloud import CloudASR
from .local_whisper import LocalWhisperASR
from .mock import MockASR

log = logging.getLogger("wingman.asr")


def build_asr(ctx: Any) -> ASREngine:
    kind = str(ctx.cfg("asr_engine", "mock") or "mock").lower()

    if kind in ("local", "whisper", "faster_whisper"):
        engine = LocalWhisperASR(
            model_size=str(ctx.cfg("whisper_model", "small")),
            device=str(ctx.cfg("whisper_device", "cpu")),
            compute_type=str(ctx.cfg("whisper_compute_type", "int8")),
        )
        if engine.available:
            return engine
        log.warning("选择本地 whisper 但未安装 faster-whisper，已退化为 Mock ASR。")
        return MockASR()

    if kind in ("cloud", "api", "openai"):
        base_url = str(ctx.cfg("asr_base_url", "") or "")
        if not base_url:
            log.warning("asr_engine=cloud 但未配置 asr_base_url，已退化为 Mock ASR。")
            return MockASR()
        return CloudASR(
            base_url=base_url,
            api_key=str(ctx.cfg("asr_api_key", "") or ""),
            model=str(ctx.cfg("asr_model", "whisper-1")),
        )

    return MockASR()
