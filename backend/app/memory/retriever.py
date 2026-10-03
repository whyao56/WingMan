"""混合检索器。

单纯向量检索在聊天场景有两个硬伤：
1. 专有名词（人名、店名、剧名）经常被语义向量"抹平"，搜不准；
2. 完全无视时间 —— 三年前的旧事和上周的事给出一样的权重。

所以做三路加权：

    score = 0.60 · 语义余弦
          + 0.25 · 关键词命中率
          + 0.15 · 时间新鲜度

权重都放在模块常量里，方便你按实际体验调。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from ..schemas import Msg
from .embedder import Embedder

log = logging.getLogger("chatwing.memory.retriever")

W_SEMANTIC = 0.60
W_KEYWORD = 0.25
W_RECENCY = 0.15

# 检索查询词里要过滤掉的高频虚词
_QUERY_STOP = set("的了 啊呀吧呢吗嘛哦嗯 是 在 有 和 就 都 也 很 不 没 我 你 他 她 什么 怎么 这个 那个")


@dataclass
class Retrieved:
    message_id: int
    sender: str
    role: str
    ts: str
    text: str
    score: float
    why: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.message_id, "sender": self.sender, "role": self.role,
            "ts": self.ts, "text": self.text, "score": round(self.score, 4),
            "why": {k: round(v, 4) for k, v in self.why.items()},
        }

    def line(self, limit: int = 300) -> str:
        t = self.text.replace("\n", " ")[:limit]
        who = "我" if self.role == "me" else self.sender
        return f"#{self.message_id}|{who}|{self.ts[:16]}|{t}"


def query_terms(query: str, limit: int = 8) -> list[str]:
    """从查询里抽关键词。中文用 2~4 字滑动窗 + 原词，英文按空格。"""
    q = (query or "").strip()
    if not q:
        return []
    terms: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", q):
        if tok not in _QUERY_STOP:
            terms.append(tok)
        if len(tok) >= 3:
            for n in (2, 3):
                for i in range(len(tok) - n + 1):
                    g = tok[i:i + n]
                    if g not in _QUERY_STOP:
                        terms.append(g)
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return seen[:limit]


class HybridRetriever:
    def __init__(self, store: Any, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    # ------------------------------------------------------------ 建索引

    async def index_chat(self, chat_id: str, batch_size: int = 256, max_items: int = 20000) -> int:
        """把还没向量化的消息补上。返回本次新增条数。"""
        model = self._model_key()
        total = 0
        while total < max_items:
            rows = self.store.unembedded(chat_id, model, limit=batch_size)
            if not rows:
                break
            texts = [self._index_text(r) for r in rows]
            vectors = await self.embedder.embed(texts)
            self.store.upsert_embeddings(
                model, [(int(r["id"]), vectors[i]) for i, r in enumerate(rows)]
            )
            total += len(rows)
            if len(rows) < batch_size:
                break
        if total:
            log.info("chat=%s 新增向量 %d 条", chat_id, total)
        return total

    def _model_key(self) -> str:
        # 模型名 + 维度一起做 key：换模型后旧向量自然失效，不会混用
        return f"{self.embedder.name}:{self.embedder.dim}"

    @staticmethod
    def _index_text(row: dict[str, Any]) -> str:
        who = "我" if row.get("role") == "me" else str(row.get("sender") or "")
        return f"{who}：{row.get('text') or ''}"

    # ------------------------------------------------------------ 检索

    async def search(
        self,
        chat_id: str,
        query: str,
        top_k: int = 30,
        exclude_id: int | None = None,
        min_score: float = 0.12,
    ) -> list[Retrieved]:
        if not query.strip():
            return []

        model = self._model_key()
        ids, mat, _dim = self.store.load_embeddings(chat_id, model)

        # --- 语义分
        sem: dict[int, float] = {}
        if len(ids) and mat.size:
            qv = await self.embedder.embed_one(query)
            if qv.shape[0] == mat.shape[1]:
                sims = mat @ qv
                for mid, s in zip(ids, sims):
                    sem[int(mid)] = float(s)
            else:
                log.warning("查询向量维度 %d 与索引 %d 不匹配，跳过语义召回", qv.shape[0], mat.shape[1])

        # --- 关键词分
        terms = query_terms(query)
        kw: dict[int, float] = {}
        if terms:
            hits = self.store.search_text(chat_id, terms, limit=400)
            for row in hits:
                text = str(row.get("text") or "")
                n = sum(1 for t in terms if t in text)
                if n:
                    kw[int(row["id"])] = min(1.0, n / max(1, min(len(terms), 4)))

        candidates = set(sem) | set(kw)
        if exclude_id is not None:
            candidates.discard(int(exclude_id))
        if not candidates:
            return []

        # --- 时间新鲜度：用消息自增 id 做代理，越新越接近 1
        max_id = max(candidates)

        scored: list[Retrieved] = []
        rows = {int(r["id"]): r for r in self.store.messages_by_ids(list(candidates))}
        for mid in candidates:
            row = rows.get(mid)
            if not row:
                continue
            s_sem = sem.get(mid, 0.0)
            s_kw = kw.get(mid, 0.0)
            # recency: 相对 max_id 的归一化，跨度越大衰减越快
            span = max(1, max_id)
            s_rec = float(mid) / span
            s_rec = s_rec ** 2  # 平方一下，让"最近"这件事更突出
            score = W_SEMANTIC * s_sem + W_KEYWORD * s_kw + W_RECENCY * s_rec
            if score < min_score:
                continue
            scored.append(Retrieved(
                message_id=mid,
                sender=str(row.get("sender") or ""),
                role=str(row.get("role") or "peer"),
                ts=str(row.get("ts") or ""),
                text=str(row.get("text") or ""),
                score=score,
                why={"semantic": s_sem, "keyword": s_kw, "recency": s_rec},
            ))

        scored.sort(key=lambda r: -r.score)
        return scored[:top_k]

    # ------------------------------------------------------------ 辅助

    async def search_multi(self, chat_id: str, queries: Sequence[str], top_k: int = 20) -> list[Retrieved]:
        """多个查询词各检索一遍再合并，用于画像抽取时提高覆盖率。"""
        merged: dict[int, Retrieved] = {}
        for q in queries:
            for r in await self.search(chat_id, q, top_k=top_k):
                cur = merged.get(r.message_id)
                if cur is None or r.score > cur.score:
                    merged[r.message_id] = r
        return sorted(merged.values(), key=lambda r: -r.score)[: top_k * 2]

    def stats(self, chat_id: str) -> dict[str, int]:
        model = self._model_key()
        ids, mat, dim = self.store.load_embeddings(chat_id, model)
        return {"indexed": len(ids), "dim": int(dim), "model": self._model_key()}
