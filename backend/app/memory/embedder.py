"""文本向量化。

两种实现：

- **CloudEmbedder** —— 走 OpenAI 兼容的 `/v1/embeddings`，语义质量好
- **HashEmbedder**  —— 纯本地，字符 n-gram 哈希 + log-TF，零依赖零配置

HashEmbedder 不是玩具：中文里字面重合和语义相关高度相关
（"喜欢猫" vs "养了只橘猫" 会因为"猫"而命中），
在脚手架的规模下召回够用，而且**永远可用** —— 没有 Key、断网、离线都能跑。
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Sequence

import numpy as np

log = logging.getLogger("chatwing.memory.embed")

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")
# 高频虚词对区分度几乎没贡献，还容易制造假匹配
_STOP = set("的了啊呀吧呢吗嘛哦嗯是的在和有就都也很不没要会能一个这那我你他她它们于与之其为以") 


class Embedder(ABC):
    name: str = "base"
    dim: int = 256
    available: bool = True
    note: str = ""

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        """返回 (N, dim) 的 float32 矩阵，且每行已 L2 归一化。"""

    async def embed_one(self, text: str) -> np.ndarray:
        mat = await self.embed([text])
        return mat[0]

    # -------------------------------------------------- 工具

    @staticmethod
    def normalize(mat: np.ndarray) -> np.ndarray:
        if mat.size == 0:
            return mat
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (mat / norms).astype(np.float32)


def _clean(text: str) -> str:
    return _PUNCT.sub(" ", _WS.sub(" ", (text or "").lower())).strip()


def _stable_hash(token: str, dim: int) -> int:
    # 必须用稳定哈希：Python 内置 hash() 每个进程都不同，
    # 那样存进库里的向量下次启动就对不上了。
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % dim


class HashEmbedder(Embedder):
    name = "hash"

    def __init__(self, dim: int = 512) -> None:
        self.dim = max(64, int(dim))

    @property
    def note(self) -> str:
        return f"本地字符 n-gram 哈希，dim={self.dim}（无语义泛化，但永远可用）"

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            out[i] = self._one(text)
        return self.normalize(out)

    def embed_sync(self, texts: Sequence[str]) -> np.ndarray:
        """索引大批量数据时用，省掉事件循环开销。"""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i] = self._one(t)
        return self.normalize(out)

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        clean = _clean(text)
        if not clean:
            return vec
        tokens = [t for t in clean.split() if t and t not in _STOP]

        grams: list[str] = []
        for tok in tokens:
            # 整词本身
            grams.append(f"w:{tok}")
            # 中文按字做 1/2/3-gram
            if len(tok) >= 2:
                for n in (2, 3):
                    for j in range(len(tok) - n + 1):
                        g = tok[j:j + n]
                        if g not in _STOP:
                            grams.append(f"{n}:{g}")
            # 英文/数字整词已够
        # 整句层面的二元组，捕捉语序信息（弱）
        joined = "".join(tokens)
        for j in range(0, max(0, len(joined) - 3), 3):
            grams.append(f"p:{joined[j:j + 4]}")

        for g in grams:
            vec[_stable_hash(g, self.dim)] += 1.0

        np.log1p(vec, out=vec)
        return vec


class CloudEmbedder(Embedder):
    name = "cloud"

    def __init__(self, base_url: str, api_key: str, model: str, dim: int = 1024,
                 timeout: float = 60.0, batch: int = 64) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "text-embedding-3-small"
        self.dim = int(dim) if dim else 1024
        self.timeout = timeout
        self.batch = max(1, min(128, batch))

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def note(self) -> str:
        return f"{self.base_url} · {self.model}" if self.available else "未配置 embedding 服务"

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        import httpx

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if not self.available:
            raise RuntimeError("CloudEmbedder 未配置 base_url / model")

        chunks = [list(texts[i:i + self.batch]) for i in range(0, len(texts), self.batch)]
        vectors: list[list[float]] = []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for chunk in chunks:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": chunk},
                    headers=headers,
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"embedding 服务返回 {resp.status_code}：{resp.text[:200]}")
                data = resp.json()
                items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
                vectors.extend([it.get("embedding") or [] for it in items])

        if not vectors:
            raise RuntimeError("embedding 服务返回空结果")
        mat = np.asarray(vectors, dtype=np.float32)
        if mat.ndim != 2:
            raise RuntimeError(f"embedding 维度异常：{mat.shape}")
        self.dim = int(mat.shape[1])
        return self.normalize(mat)


# ---------------------------------------------------------------- 工厂


def build_embedder(ctx: Any) -> Embedder:
    mode = str(ctx.cfg("embedder", "auto") or "auto").lower()
    dim = int(ctx.cfg("embed_dim", 512) or 512)

    if mode == "hash":
        return HashEmbedder(dim)

    base_url = str(ctx.cfg("embed_base_url", "") or "")
    api_key = str(ctx.cfg("embed_api_key", "") or "")

    # auto：优先复用主模型的端点（多数云服务 embedding 和 chat 同域）
    if mode == "auto" and not base_url:
        prov = str(ctx.cfg("llm_provider", "") or "").lower()
        if prov in ("openai", "openai_compat", "cloud", "api"):
            base_url = str(ctx.cfg("llm_base_url", "") or "")
            api_key = api_key or str(ctx.cfg("llm_api_key", "") or "")

    if mode in ("auto", "cloud") and base_url:
        return CloudEmbedder(
            base_url=base_url,
            api_key=api_key,
            model=str(ctx.cfg("embed_model", "text-embedding-3-small")),
            dim=dim,
        )

    if mode == "cloud":
        log.warning("embedder=cloud 但未配置 base_url，已退化为本地哈希向量。")
    return HashEmbedder(dim)
