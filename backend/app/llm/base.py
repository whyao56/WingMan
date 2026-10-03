"""LLM Provider 抽象 + 容错 JSON 解析。

引擎层只依赖 `complete_json()`。所有脏活的处理（去围栏、找边界、补括号）
都收在 `parse_json_loose()` 里 —— 因为各家模型在「输出纯 JSON」这件事上
可靠程度差别巨大，不兜住就没法做稳定产品。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger("chatwing.llm")


class LLMError(RuntimeError):
    """调用模型失败（网络、鉴权、配额等）。"""


class LLMFormatError(LLMError):
    """模型返回了无法解析成 JSON 的内容。"""


# ---------------------------------------------------------------- 解析工具

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_PREFIX = re.compile(r"^\s*(?:json|JSON|以下是|这是|结果[:：])\s*[\n:：]?\s*")


def _extract_balanced(s: str, start: int) -> str:
    """从 start 位置开始，按括号配平截出一段完整 JSON 文本（跳过字符串内的括号）。"""
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


def _repair(s: str) -> str:
    """常见小毛病的修补：尾逗号、中文引号、行内注释。"""
    out = s
    out = out.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    out = re.sub(r"//[^\n\"]*$", "", out, flags=re.M)
    out = re.sub(r",\s*([}\]])", r"\1", out)          # 尾逗号
    out = re.sub(r"([}\]])[\s,]*$", r"\1", out)        # 尾部多余分隔符
    return out


def parse_json_loose(text: str) -> dict[str, Any]:
    """尽最大努力把模型输出解析成 dict。"""
    if not text or not text.strip():
        raise LLMFormatError("模型返回了空内容")

    s = text.strip()
    if m := _FENCE.search(s):
        s = m.group(1).strip()
    s = _PREFIX.sub("", s, count=1)

    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        raise LLMFormatError(f"响应中找不到 JSON 起始符号：{s[:160]!r}")

    for start in sorted(starts):
        chunk = _extract_balanced(s, start)
        for candidate in (chunk, _repair(chunk)):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                return {"items": data}

    # 最后一搏：正规表达式抠出最外层花括号
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(_repair(s[a:b + 1]))
        except json.JSONDecodeError:
            pass

    raise LLMFormatError(f"无法解析为 JSON：{s[:220]!r}")


def task_tag(system: str) -> str:
    """从 system prompt 首行提取 [TASK:XXX] 标记，供 Mock 分派和日志追踪。"""
    m = re.match(r"\s*\[TASK:([A-Z_]+)\]", system or "")
    return m.group(1) if m else "UNKNOWN"


# ---------------------------------------------------------------- 抽象


class ChatProvider(ABC):
    name: str = "base"
    available: bool = True
    note: str = ""

    # ------------------------------------------------------ 原始调用

    @abstractmethod
    async def chat_raw(self, messages: list[dict[str, str]], **kw: Any) -> str:
        """messages 为 OpenAI 风格的 [{"role","content"}]。"""

    # ------------------------------------------------------ 便捷封装

    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await self.chat_raw(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        schema_hint: str = "",
        retries: int = 1,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """要求模型输出 JSON，并在解析失败时带着纠正提示重试一次。"""
        tag = task_tag(system)
        hint = f"\n\n【输出格式】只输出一个 JSON 对象，不要解释、不要 markdown 围栏。\n{schema_hint}" if schema_hint else ""
        raw = await self.complete(
            system, user + hint, json_mode=True,
            temperature=temperature, max_tokens=max_tokens,
        )
        try:
            return parse_json_loose(raw)
        except LLMFormatError as exc:
            log.warning("[%s] JSON 解析失败（%s），准备重试", tag, exc)
            if retries <= 0:
                raise
            fix = (
                user
                + hint
                + "\n\n【注意】你上一次的回复不是合法 JSON，解析器无法读取。"
                  "请重新输出，且**只**输出 JSON 对象本身，第一个字符必须是 {，最后一个字符必须是 }。"
            )
            raw2 = await self.complete(
                system, fix, json_mode=True,
                temperature=0.2, max_tokens=max_tokens,
            )
            return parse_json_loose(raw2)

    # ------------------------------------------------------ 连通性

    async def ping(self) -> tuple[bool, str]:
        """给「测试连接」按钮用。返回 (是否可用, 说明)。"""
        try:
            txt = await self.complete("你是一个测试助手。", "只回复两个字：正常", max_tokens=16)
            return True, (txt or "").strip()[:40] or "连接成功"
        except Exception as exc:
            return False, str(exc)[:300]
