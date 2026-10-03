"""Ollama 本地模型 Provider。

跑本地模型的意义在于：**聊天记录一条都不出本机**。
代价是需要显存/内存，以及比云端弱一些的推理能力。

推荐起步模型：`qwen2.5:7b`（中文好、7B 能在 8G 显存跑）、
更好一点用 `qwen2.5:14b` 或 `glm4:9b`。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import ChatProvider, LLMError

log = logging.getLogger("wingman.llm.ollama")


class OllamaProvider(ChatProvider):
    name = "ollama"

    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:7b",
        *,
        temperature: float = 0.8,
        max_tokens: int = 2048,
        timeout: float = 180.0,
    ) -> None:
        self.host = (host or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or ""
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.host and self.model)

    @property
    def note(self) -> str:
        return f"{self.host} · {self.model}"

    async def chat_raw(self, messages: list[dict[str, str]], **kw: Any) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": kw.get("temperature", self.temperature),
                "num_predict": kw.get("max_tokens", self.max_tokens),
            },
        }
        if kw.get("json_mode"):
            payload["format"] = "json"

        url = f"{self.host}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
        except httpx.ConnectError as exc:
            raise LLMError(
                f"连不上 Ollama（{self.host}）。请确认 ollama serve 已在运行。"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"本地推理超时（{self.timeout:.0f}s），模型可能过大。") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"网络错误：{exc}") from exc

        if resp.status_code == 404:
            raise LLMError(
                f"模型 {self.model} 未找到。先执行：ollama pull {self.model}"
            )
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}：{resp.text[:300]}")

        data = resp.json()
        if data.get("error"):
            raise LLMError(str(data["error"])[:300])
        return str((data.get("message") or {}).get("content") or "")

    # ------------------------------------------------------ 模型列表

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.host}/api/tags")
            if resp.status_code >= 400:
                return []
            return [str(m.get("name")) for m in resp.json().get("models", []) if m.get("name")]
        except Exception:
            return []
