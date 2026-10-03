"""OpenAI 兼容协议的通用 Provider。

只要服务商提供 `/v1/chat/completions`，把 base_url 填对就能用：
DeepSeek、通义千问、Kimi、智谱、硅基流动、OpenAI、各类中转站、vLLM、LM Studio……
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import ChatProvider, LLMError

log = logging.getLogger("chatwing.llm.openai")


class OpenAICompatProvider(ChatProvider):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        temperature: float = 0.8,
        max_tokens: int = 2048,
        timeout: float = 120.0,
        supports_json_mode: bool | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        # 有些自建端点不支持 response_format，第一次报错后自动关掉
        self._json_mode_ok = supports_json_mode

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def note(self) -> str:
        return f"{self.base_url} · {self.model}" if self.available else "未配置 base_url 或 model"

    # ------------------------------------------------------ 调用

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ChatWing/0.1",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def chat_raw(self, messages: list[dict[str, str]], **kw: Any) -> str:
        json_mode = bool(kw.get("json_mode"))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": kw.get("temperature", self.temperature),
            "max_tokens": kw.get("max_tokens", self.max_tokens),
            "stream": False,
        }
        if json_mode and self._json_mode_ok is not False:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise LLMError(f"请求超时（{self.timeout:.0f}s）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"网络错误：{exc}") from exc

        if resp.status_code == 400 and json_mode and self._json_mode_ok is None:
            # 大概率是该端点不认 response_format，关掉重试一次
            log.info("端点不接受 response_format，自动降级重试：%s", resp.text[:160])
            self._json_mode_ok = False
            payload.pop("response_format", None)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())

        if resp.status_code >= 400:
            raise LLMError(self._explain(resp.status_code, resp.text))

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"响应不是 JSON：{resp.text[:200]}") from exc

        if err := data.get("error"):
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError(f"服务端返回错误：{msg}")

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices：{str(data)[:200]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content is None:
            content = msg.get("reasoning_content") or ""
        if isinstance(content, list):
            # 少数服务返回分段内容数组
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        usage = data.get("usage") or {}
        if usage:
            log.debug("usage=%s", usage)
        return str(content or "")

    @staticmethod
    def _explain(status: int, body: str) -> str:
        snippet = (body or "")[:300]
        mapping = {
            401: "鉴权失败（401）：请检查 API Key 是否正确、是否已过期。",
            403: "无权访问（403）：可能是 Key 权限不足或模型未开通。",
            404: "地址不存在（404）：请检查 base_url，通常应以 /v1 结尾，且不要带 /chat/completions。",
            429: "请求过于频繁或额度用尽（429）。",
            500: "服务端错误（500），稍后重试。",
            502: "网关错误（502），中转服务可能不稳定。",
            503: "服务不可用（503）。",
        }
        return f"{mapping.get(status, f'HTTP {status}')} 原始响应：{snippet}"

    # ------------------------------------------------------ 模型列表

    async def list_models(self) -> list[str]:
        if not self.base_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
            if resp.status_code >= 400:
                return []
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else data
            return [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]
        except Exception:
            return []
