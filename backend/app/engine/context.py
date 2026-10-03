"""上下文组装 —— 决定"模型能看到什么"，是效果好坏的第一决定因素。

三层记忆在这里汇合：

    L1 事实层（全量注入，几十条，几百 token）
  + L2 摘要层（最近几条时间线）
  + L3 检索层（按当前消息动态召回 top-K 原文）
  + 最近对话（完整来回，保证连贯）
  + 人物卡（对方画像 + 雷区 + 用户目标）

事实层之所以全量注入：几十条事实的 token 成本可以忽略，
但漏掉一条"她讨厌被叫宝贝"就可能直接毁掉一件事。
**漏掉的代价和多余的成本，完全不对称。**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..memory.retriever import HybridRetriever, Retrieved
from ..schemas import ChatInfo, Fact, Persona, Summary

log = logging.getLogger("wingman.engine.context")

MAX_FACTS = 60
MAX_SUMMARIES = 5
MAX_RECENT = 24
MAX_STYLE_SAMPLES = 8


@dataclass
class ContextPack:
    chat: ChatInfo
    peer_message: str
    persona: Persona
    facts: list[Fact] = field(default_factory=list)
    summaries: list[Summary] = field(default_factory=list)
    retrieved: list[Retrieved] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    style_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- 渲染后的文本块 ----
    profile_text: str = ""
    facts_text: str = ""
    summaries_text: str = ""
    retrieved_text: str = ""
    recent_text: str = ""
    style_text: str = ""

    # ---- 给前端的上下文引用 ----
    context_used: list[dict[str, Any]] = field(default_factory=list)

    @property
    def peer_name(self) -> str:
        return self.chat.peer_name or "对方"

    @property
    def me_name(self) -> str:
        return self.chat.me_name or "我"

    def who(self, row: dict[str, Any]) -> str:
        return self.me_name if row.get("role") == "me" else self.peer_name

    def render(self) -> None:
        self.profile_text = self.persona.peer_profile or ""
        self.facts_text = "\n".join(
            f"[{'对方' if f.subject == 'peer' else ('我' if f.subject == 'me' else '关系')}] "
            f"{f.key}: {f.value}"
            for f in self.facts[:MAX_FACTS]
        )
        self.summaries_text = "\n".join(
            f"- {s.period}：{s.content}" for s in self.summaries[:MAX_SUMMARIES]
        )
        self.retrieved_text = "\n".join(r.line() for r in self.retrieved)
        self.recent_text = "\n".join(
            f"[{str(r.get('ts') or '')[5:16]}] {self.who(r)}：{str(r.get('text') or '')[:200]}"
            for r in self.recent
        )
        self.style_text = "\n".join(f'- "{s}"' for s in self.style_samples)
        self.context_used = [r.to_dict() for r in self.retrieved]


async def build_context(
    ctx: Any,
    chat_id: str,
    peer_message: str,
    *,
    top_k: int = 30,
    recent_n: int = MAX_RECENT,
    use_retrieval: bool = True,
) -> ContextPack:
    chat = ctx.store.get_chat(chat_id)
    if chat is None:
        raise KeyError(f"会话不存在：{chat_id}")

    persona = ctx.store.get_persona(chat_id)
    facts = ctx.store.list_facts(chat_id)
    summaries = ctx.store.list_summaries(chat_id, limit=MAX_SUMMARIES)
    recent = ctx.store.recent_messages(chat_id, recent_n)

    pack = ContextPack(
        chat=chat, peer_message=peer_message, persona=persona,
        facts=facts, summaries=summaries, recent=recent,
    )

    if chat.peer_name:
        pass
    else:
        pack.warnings.append("这条会话没有设置对方昵称，分析里的主语会显示为「对方」。")

    # ---- L3 检索
    if use_retrieval and peer_message.strip():
        retriever = HybridRetriever(ctx.store, ctx.embedder)
        stats = retriever.stats(chat_id)
        if stats["indexed"] == 0:
            pack.warnings.append(
                "这条会话还没建向量索引，本次分析只用了事实与最近对话。"
                "建议在「记忆」页点一次「重建索引」。"
            )
        else:
            try:
                pack.retrieved = await retriever.search(
                    chat_id, peer_message, top_k=top_k,
                    exclude_id=(recent[-1]["id"] if recent else None),
                )
            except Exception as exc:
                log.warning("检索失败：%s", exc)
                pack.warnings.append(f"历史检索失败（{exc}），已降级为仅用最近对话。")

    # ---- 她的真实说话风格样本（喂给模型做 few-shot，比自己描述风格有效得多）
    peer_rows = [r for r in ctx.store.all_messages(chat_id) if r.get("role") == "peer"]
    picks = [
        str(r.get("text") or "").strip()
        for r in peer_rows
        if r.get("msg_type") == "text" and 4 <= len(str(r.get("text") or "").strip()) <= 60
    ]
    if picks:
        step = max(1, len(picks) // MAX_STYLE_SAMPLES)
        pack.style_samples = picks[-MAX_STYLE_SAMPLES * step::step][:MAX_STYLE_SAMPLES]

    pack.render()
    return pack
