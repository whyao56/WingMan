"""按配置构造 LLM Provider。

原则：**永远返回一个能用的实例**。配置不全就退回 Mock 并说明原因，
而不是抛异常让整个应用起不来。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ChatProvider
from .mock import MockProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider

log = logging.getLogger("wingman.llm")


def build_llm(ctx: Any) -> ChatProvider:
    kind = str(ctx.cfg("llm_provider", "mock") or "mock").lower().strip()

    if kind in ("ollama", "local"):
        return OllamaProvider(
            host=str(ctx.cfg("ollama_host", "http://127.0.0.1:11434")),
            model=str(ctx.cfg("ollama_model", "qwen2.5:7b")),
            temperature=float(ctx.cfg("llm_temperature", 0.8)),
            max_tokens=int(ctx.cfg("llm_max_tokens", 2048)),
            timeout=float(ctx.cfg("llm_timeout", 180)),
        )

    if kind in ("openai", "openai_compat", "cloud", "api"):
        base_url = str(ctx.cfg("llm_base_url", "") or "")
        model = str(ctx.cfg("llm_model", "") or "")
        if not base_url or not model:
            log.warning(
                "llm_provider=%s 但 base_url/model 未配置完整，已退化为 Mock。", kind
            )
            return MockProvider()
        return OpenAICompatProvider(
            base_url=base_url,
            api_key=str(ctx.cfg("llm_api_key", "") or ""),
            model=model,
            temperature=float(ctx.cfg("llm_temperature", 0.8)),
            max_tokens=int(ctx.cfg("llm_max_tokens", 2048)),
            timeout=float(ctx.cfg("llm_timeout", 120)),
        )

    if kind != "mock":
        log.warning("未知的 llm_provider=%s，已退化为 Mock。", kind)
    return MockProvider()
