"""全局运行时上下文：唯一的 Store 实例、配置覆盖、各 Provider 的缓存。

所有模块通过 `get_ctx()` 拿依赖，不要自己 new Store —— 否则会拿到不同的连接池设置。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import (
    APP_DIR,
    DATA_DIR,
    EDITABLE_KEYS,
    IS_FROZEN,
    LOG_DIR,
    Settings,
    _migrate_loose_data,
    get_settings,
)
from .store import Store

log = logging.getLogger("wingman")


class AppContext:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        # 冻结态下用户目录才是可写的；顺手把可能落在 exe 旁边的旧数据搬过来
        _migrate_loose_data()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.settings.db_path)
        self.store.init()
        self.started_at = time.time()

        # 运行时覆盖（控制台写入的），key 与 Settings 字段名一致
        raw = self.store.kv_all()
        self._overrides: dict[str, str] = {
            k: v for k, v in raw.items() if k in EDITABLE_KEYS
        }

        self._llm: Any = None
        self._embedder: Any = None
        self._asr: Any = None

    # -------------------------------------------------------- 配置读取

    def cfg(self, name: str, default: Any = None) -> Any:
        """运行时覆盖 > .env > 默认值。空字符串视为未设置。"""
        v = self._overrides.get(name)
        if v is None or v == "":
            v = getattr(self.settings, name, None)
        if v is None or v == "":
            return default
        # 按目标类型做一次轻量转换
        target = getattr(self.settings, name, None)
        if isinstance(target, bool):
            return str(v).lower() in ("1", "true", "yes", "on")
        if isinstance(target, int) and not isinstance(target, bool):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default if default is not None else target
        if isinstance(target, float):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default if default is not None else target
        return v

    # -------------------------------------------------------- 配置写入

    def update_cfg(self, patch: dict[str, Any]) -> dict[str, str]:
        """写入运行时覆盖，并让 Provider 缓存失效。"""
        applied: dict[str, str] = {}
        for k, v in patch.items():
            if k not in EDITABLE_KEYS:
                continue
            sval = "" if v is None else str(v)
            self._overrides[k] = sval
            self.store.kv_set(k, sval)
            applied[k] = sval
        self.invalidate()
        return applied

    def reset_cfg(self) -> None:
        """清空所有运行时覆盖，回到 .env。"""
        for k in list(self._overrides):
            self.store.kv_delete(k)
        self._overrides.clear()
        self.invalidate()

    def overrides(self) -> dict[str, str]:
        return dict(self._overrides)

    # -------------------------------------------------------- Provider

    def invalidate(self) -> None:
        self._llm = None
        self._embedder = None
        self._asr = None

    @property
    def llm(self):
        if self._llm is None:
            from .llm.factory import build_llm

            self._llm = build_llm(self)
            log.info("LLM provider ready: %s", self._llm.name)
        return self._llm

    @property
    def embedder(self):
        if self._embedder is None:
            from .memory.embedder import build_embedder

            self._embedder = build_embedder(self)
            log.info("Embedder ready: %s (dim=%s)", self._embedder.name, self._embedder.dim)
        return self._embedder

    @property
    def asr(self):
        if self._asr is None:
            from .asr.factory import build_asr

            self._asr = build_asr(self)
            log.info("ASR engine ready: %s", self._asr.name)
        return self._asr

    def provider_summary(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for kind, obj in (("llm", self.llm), ("embedder", self.embedder), ("asr", self.asr)):
            out.append({
                "kind": kind,
                "name": getattr(obj, "name", "unknown"),
                "available": getattr(obj, "available", True),
                "note": getattr(obj, "note", ""),
            })
        return out

    # -------------------------------------------------------- 自检

    def self_check(self) -> dict[str, Any]:
        """完整自检结果。包含会阻塞的设备枚举，调用方请用 to_thread。"""
        from .selfcheck import run

        return run(self).as_dict()

    def runtime_info(self) -> dict[str, Any]:
        return {
            "frozen": IS_FROZEN,
            "app_dir": str(APP_DIR),
            "data_dir": str(DATA_DIR),
            "log_dir": str(LOG_DIR),
            "uptime_sec": round(time.time() - self.started_at, 1),
        }


_CTX: AppContext | None = None


def get_ctx() -> AppContext:
    global _CTX
    if _CTX is None:
        _CTX = AppContext()
    return _CTX
