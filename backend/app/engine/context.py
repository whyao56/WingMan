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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..memory.retriever import HybridRetriever, Retrieved
from ..schemas import ChatInfo, Fact, Persona, Summary

log = logging.getLogger("wingman.engine.context")


def self_chat_name(ctx: Any, chat_id: str) -> str:
    """给提示文案用的渠道名；查不到就退回 chat_id，不抛错。"""
    try:
        chat = ctx.store.get_chat(chat_id)
    except Exception:      # pragma: no cover - 纯文案，失败不该影响分析
        chat = None
    return (chat.name if chat else "") or chat_id

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
    # 本次分析实际用了哪些渠道（第一个是主渠道）。呼应需求 8 的多选。
    chat_ids: list[str] = field(default_factory=list)

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


def _as_ids(chat_ids: str | Sequence[str]) -> list[str]:
    """把「一个渠道」与「一串渠道」统一成有序列表（第一个是主渠道）。"""
    if isinstance(chat_ids, str):
        ids = [chat_ids]
    else:
        ids = [str(c) for c in chat_ids]
    return [c for c in ids if c]


async def build_context(
    ctx: Any,
    chat_ids: str | Sequence[str],
    peer_message: str,
    *,
    top_k: int = 30,
    recent_n: int = MAX_RECENT,
    use_retrieval: bool = True,
) -> ContextPack:
    """组装一次分析能看到的上下文。

    `chat_ids` 可以是单个渠道，也可以是**同一个对象下被勾选的多个渠道**（需求 8）。
    多选时以第一个为「主渠道」（人格、昵称、检索锚点都取自它），其余渠道的
    事实、摘要与近期消息**合并进来** —— 因为对同一个人来说，微信上聊的与 QQ 上
    聊的是同一段关系，只喂一个渠道等于让模型看半张牌。
    """
    ids = _as_ids(chat_ids)
    if not ids:
        raise KeyError("没有选定任何会话")
    primary = ids[0]
    chat = ctx.store.get_chat(primary)
    if chat is None:
        raise KeyError(f"会话不存在：{primary}")

    persona = ctx.store.get_persona(primary)

    # 事实：有对象就用对象视野（对象级 + 该对象名下全部渠道级），没有则退回渠道级。
    # 再补上其余被勾选渠道的事实（正常情况下它们在同一个对象下，已被覆盖，这里只是兜底）。
    facts: list[Fact] = []
    pid = chat.person_id or ""
    if pid:
        facts = ctx.store.list_facts_for_person(pid)
    seen_fact = {(f.subject, f.key, f.value) for f in facts}
    for cid in ids:
        for f in ctx.store.list_facts(cid):
            key = (f.subject, f.key, f.value)
            if key not in seen_fact:
                seen_fact.add(key)
                facts.append(f)

    summaries: list[Summary] = []
    for cid in ids:
        summaries.extend(ctx.store.list_summaries(cid, limit=MAX_SUMMARIES))
    summaries.sort(key=lambda s: s.period or "", reverse=True)
    summaries = summaries[:MAX_SUMMARIES]

    # 多渠道的近期消息按时间归并 —— 不做「按渠道分段」，因为模型需要看到
    # 「她昨晚在 QQ 上说的」和「今天在微信上说的」之间的先后关系。
    recent: list[dict[str, Any]] = []
    for cid in ids:
        recent.extend(ctx.store.recent_messages(cid, recent_n))
    recent.sort(key=lambda r: str(r.get("ts") or ""))
    recent = recent[-recent_n:]

    pack = ContextPack(
        chat=chat, peer_message=peer_message, persona=persona,
        facts=facts, summaries=summaries, recent=recent,
    )
    pack.chat_ids = ids

    if len(ids) > 1:
        pack.warnings.append(
            f"本次合并了 {len(ids)} 个渠道的上下文，以「{chat.name}」为主。")
    if chat.peer_name:
        pass
    else:
        pack.warnings.append("这条会话没有设置对方昵称，分析里的主语会显示为「对方」。")

    # ---- L3 检索：按主渠道检索，逐渠道做 —— 每个渠道的向量索引是独立的
    if use_retrieval and peer_message.strip():
        retriever = HybridRetriever(ctx.store, ctx.embedder)
        per_chat_k = max(5, top_k // max(1, len(ids)))
        for cid in ids:
            stats = retriever.stats(cid)
            if stats["indexed"] == 0:
                pack.warnings.append(
                    f"「{self_chat_name(ctx, cid)}」还没建向量索引，本次只用了事实与最近对话。"
                    "可以在对象详情的「记忆」页重建索引。"
                )
                continue
            try:
                pack.retrieved.extend(await retriever.search(
                    cid, peer_message, top_k=per_chat_k,
                    exclude_id=(recent[-1]["id"] if recent else None),
                ))
            except Exception as exc:
                log.warning("检索失败：%s", exc)
                pack.warnings.append(f"历史检索失败（{exc}），已降级为仅用最近对话。")
        pack.retrieved.sort(key=lambda r: -float(getattr(r, "score", 0) or 0))
        pack.retrieved = pack.retrieved[:top_k]

    # ---- 她的真实说话风格样本（喂给模型做 few-shot，比自己描述风格有效得多）
    peer_rows: list[dict[str, Any]] = []
    for cid in ids:
        peer_rows.extend(r for r in ctx.store.all_messages(cid) if r.get("role") == "peer")
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
