"""结构化只读健康路由：`GET /api/health/details`。

和 `routes_admin.py` 里的 `GET /api/health` 是两个用途：

- `/api/health` —— 既有契约（version / db / counts / providers），
  scripts/e2e_check.py 依赖它，**不许改动**；
- `/api/health/details` —— 新增的结构化自证健康报告：配置来源、数据路径、
  DB 统计、各 provider 状态、可选依赖状态、运行时覆盖的键名。

只读 GET，不改任何状态；密钥安全是硬约束：

- `config.SECRET_KEYS`（以及名字里带 api_key / token / secret / password 的键）
  只以掩码形式出现，并且**不外泄长度**（见 `_mask_value`）；
- 运行时覆盖（数据库 kv 表）**只列键名、不列值** —— 那里的值可能是明文密钥。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import __version__
from ..config import (
    BACKEND_DIR,
    DATA_DIR,
    FRONTEND_DIR,
    PROJECT_DIR,
    SAMPLES_DIR,
    SECRET_KEYS,
    get_settings,
)
from ..context import get_ctx

router = APIRouter(prefix="/api", tags=["health"])

# 报告里逐键交代来源的配置项（其余键不展开，避免把设置页整张表搬过来）
WATCH_KEYS: tuple[str, ...] = (
    "host",
    "port",
    "llm_provider",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "ollama_host",
    "ollama_model",
    "embedder",
    "embed_base_url",
    "embed_api_key",
    "embed_model",
)

OPTIONAL_DEPS: tuple[tuple[str, str, str], ...] = ()

_SECRET_NAME_HINTS = ("api_key", "apikey", "secret", "token", "password", "passwd")

_SOURCE_LABELS = {
    "runtime": "控制台写入的运行时覆盖（数据库 kv 表）",
    "env": "进程环境变量",
    "dotenv": ".env 文件",
    "default": "代码默认值",
    "unknown": "未找到",
}


# ---------------------------------------------------------------- 工具


def _is_secret_key(key: str) -> bool:
    if key in SECRET_KEYS:
        return True
    low = key.lower()
    return any(hint in low for hint in _SECRET_NAME_HINTS)


def _mask_value(value: str) -> str:
    """密钥掩码：形如 sk-abc***xyz，且不泄漏长度。

    config.mask_secret 对长度 <= 8 的值返回 "*" * len(value)，等于把密钥长度说出来；
    这里对短密钥统一给固定的 "***"，长密钥才沿用 mask_secret 的展示形式。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}{'*' * 6}{value[-3:]}"


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析，只取 KEY=VALUE（与 pydantic-settings 的口径一致）。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        out[key.upper()] = value.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def _env_file_rows() -> tuple[list[dict[str, Any]], list[tuple[Path, dict[str, str]]]]:
    """列出 Settings 实际会读的 .env 文件及其中的键名（键名可以给，值不给）。"""
    settings_cls = type(get_settings())
    raw_files = settings_cls.model_config.get("env_file") or ()
    if isinstance(raw_files, (str, Path)):
        raw_files = (raw_files,)
    parsed: list[tuple[Path, dict[str, str]]] = []
    rows: list[dict[str, Any]] = []
    for item in raw_files:
        path = Path(item)
        values = _parse_env_file(path)
        parsed.append((path, values))
        rows.append({
            "path": str(path),
            "exists": path.is_file(),
            "variables": sorted(values.keys()),
            "variable_count": len(values),
        })
    return rows, parsed


def _resolve_key(
    key: str, overrides: dict[str, str], env_parsed: list[tuple[Path, dict[str, str]]]
) -> tuple[Any, str, str]:
    """运行时覆盖 > 进程环境变量 > .env（后一个文件覆盖前一个） > 代码默认值。"""
    override = overrides.get(key)
    if override:
        return override, "runtime", _SOURCE_LABELS["runtime"]

    env_value = os.environ.get(key.upper())
    if env_value:
        return env_value, "env", f"{_SOURCE_LABELS['env']} {key.upper()}"

    for path, values in reversed(env_parsed):
        if key.upper() in values:
            return values[key.upper()], "dotenv", f"{_SOURCE_LABELS['dotenv']} {path}"

    field = type(get_settings()).model_fields.get(key)
    if field is not None and field.default is not None:
        return field.default, "default", _SOURCE_LABELS["default"]
    return None, "unknown", _SOURCE_LABELS["unknown"]


def _optional_dep_state() -> dict[str, Any]:
    """可选依赖只做 find_spec 探测，不 import（避免拖慢健康检查、避免副作用）。"""
    out: dict[str, Any] = {}
    for module, dist, purpose in OPTIONAL_DEPS:
        installed = False
        try:
            installed = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            installed = False
        version = ""
        if installed:
            try:
                version = importlib.metadata.version(dist)
            except Exception:
                version = ""
        out[module] = {
            "installed": installed,
            "version": version,
            "optional": True,
            "purpose": purpose,
        }
    return out


def _db_stats() -> dict[str, Any]:
    """数据库统计（只读）。同步的 sqlite 调用交给 to_thread，别阻塞事件循环。"""
    ctx = get_ctx()
    path = Path(ctx.store.db_path)
    stats: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "counts": {},
    }
    if path.is_file():
        stats["counts"] = ctx.store.counts()
    return stats


# ---------------------------------------------------------------- 路由


@router.get("/health/details")
async def health_details() -> dict[str, Any]:
    """结构化自证健康：路径、配置来源、provider 状态、可选依赖、DB 统计。

    只读：不写任何状态；密钥一律掩码且不泄漏长度。
    """
    ctx = get_ctx()
    env_rows, env_parsed = _env_file_rows()
    overrides = ctx.overrides()

    config: dict[str, Any] = {}
    for key in WATCH_KEYS:
        value, source, source_label = _resolve_key(key, overrides, env_parsed)
        secret = _is_secret_key(key)
        entry: dict[str, Any] = {
            "source": source,
            "source_label": source_label,
            "secret": secret,
            "set": bool(value not in (None, "", False)),
        }
        entry["value"] = _mask_value(str(value)) if (secret and value) else ("" if secret else value)
        config[key] = entry

    providers = ctx.provider_summary()
    db_stats = await asyncio.to_thread(_db_stats)
    optional = await asyncio.to_thread(_optional_dep_state)

    notes: list[str] = []
    for provider in providers:
        if not provider.get("available", True):
            notes.append(f"{provider['kind']} provider「{provider['name']}」不可用：{provider.get('note', '')}")
        elif provider["kind"] == "llm" and provider["name"] == "mock":
            notes.append("当前 LLM 是 Mock：能跑通全流程，但回复只是流程演示，不是真模型输出。")
    for module, state in optional.items():
        if not state["installed"]:
            notes.append(f"可选依赖 {module} 未安装（{state['purpose']}），不影响主服务。")
    if not db_stats["exists"]:
        notes.append("数据库文件尚不存在，将在首次写入时创建。")

    return {
        "version": __version__,
        "ok": all(p.get("available", True) for p in providers) and db_stats["exists"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "paths": {
            "project": str(PROJECT_DIR),
            "backend": str(BACKEND_DIR),
            "data": str(DATA_DIR),
            "frontend": str(FRONTEND_DIR),
            "samples": str(SAMPLES_DIR),
            "db": db_stats["path"],
        },
        "env_files": env_rows,
        "env_files_hit": [row["path"] for row in env_rows if row["exists"]],
        # 只列键名：kv 表里的值可能是明文密钥，一概不回显
        "runtime_override_keys": sorted(k for k, v in overrides.items() if v),
        "config": config,
        "providers": providers,
        "db": db_stats,
        "optional_deps": optional,
        "notes": notes,
    }
