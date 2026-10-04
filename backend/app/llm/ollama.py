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

# 本机 Ollama 走回环时不能被系统代理劫持（否则报「网关错误 502」而不是「连不上 Ollama」）。
_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def _trust_env_for(url: str) -> bool:
    """这个地址要不要继承环境代理（httpx 的 trust_env）。回环一律 False。

    httpx 默认 trust_env=True 会继承 HTTP_PROXY 环境变量与 Windows 注册表里的系统代理；
    本机代理软件开全局模式时，发往 127.0.0.1:11434 的请求会被代理接管 —— Ollama 明明在跑，
    应用却报「网关错误（502）/连不上」。非回环地址保持 True，不影响用户走代理访问云端。
    （与 llm/openai_compat.py、memory/embedder.py、asr/cloud.py 里的同名实现保持一致，
      测试 test_loopback_proxy.py 会断言四份行为相同。）
    """
    raw = (url or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]                        # 去掉 scheme
    authority = raw.split("/", 1)[0].rsplit("@", 1)[-1]     # 去掉 user:pass@
    if authority.startswith("["):                           # [::1]:11434 这种 IPv6 字面量
        host = authority.split("]", 1)[0][1:]
    elif authority.count(":") == 1:
        host = authority.rsplit(":", 1)[0]                  # host:port
    else:
        host = authority                                    # 没写端口，或没写方括号的 IPv6
    host = host.strip().rstrip(".")
    if not host:
        return True
    return not (host in _LOOPBACK_HOSTS or host.startswith("127."))


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
            async with httpx.AsyncClient(timeout=self.timeout,
                                         trust_env=_trust_env_for(self.host)) as client:
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
            async with httpx.AsyncClient(timeout=10.0,
                                         trust_env=_trust_env_for(self.host)) as client:
                resp = await client.get(f"{self.host}/api/tags")
            if resp.status_code >= 400:
                return []
            return [str(m.get("name")) for m in resp.json().get("models", []) if m.get("name")]
        except Exception:
            return []
