"""环境配置与路径常量。

优先级：控制台写入的运行时覆盖 > .env > 代码默认值。
运行时覆盖存在 SQLite 的 kv 表里，由 `context.AppContext.cfg()` 负责合并。

打包成 exe 后有两件事必须变，否则会出「用户的数据重启就没了」这种
最难查的 bug：

1. **资源从哪读**：源码在的时候资源就在仓库里；打包后它们被塞进
   PyInstaller 的 ``sys._MEIPASS``（onefile 是临时解压目录，onedir 是
   ``_internal``）。所以只能读 ``RESOURCE_DIR``，绝不能用 ``__file__`` 往上找。
2. **数据往哪写**：程序目录在 Program Files 下是只读的，onefile 的临时
   目录更是每次运行都不一样、退出即删。所以数据必须写到
   ``%LOCALAPPDATA%\\WingMan``。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "WingMan"

# ---------------------------------------------------------------- 冻结态判定

IS_FROZEN: bool = bool(getattr(sys, "frozen", False))
"""是否跑在 PyInstaller 打出来的 exe 里。"""


def _resource_dir() -> Path:
    """只读资源（前端页面、示例数据、文档）的根目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # 开发态：app/config.py → app → backend → 项目根
    return Path(__file__).resolve().parent.parent.parent


def _writable_root() -> Path:
    """可写根目录。冻结态用用户目录，开发态用仓库。"""
    if not IS_FROZEN:
        return Path(__file__).resolve().parent.parent.parent  # 项目根
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / APP_NAME


RESOURCE_DIR = _resource_dir()
APP_DIR = _writable_root()

# 开发态沿用仓库里的 backend/data，避免老用户数据搬家；
# 冻结态写到用户目录。
BACKEND_DIR = Path(__file__).resolve().parent.parent  # wingman/backend
PROJECT_DIR = RESOURCE_DIR if IS_FROZEN else BACKEND_DIR.parent
DATA_DIR = (APP_DIR / "data") if IS_FROZEN else (BACKEND_DIR / "data")
LOG_DIR = (APP_DIR / "logs") if IS_FROZEN else (BACKEND_DIR / "logs")

FRONTEND_DIR = RESOURCE_DIR / "frontend"
SAMPLES_DIR = RESOURCE_DIR / "samples"
DOCS_DIR = RESOURCE_DIR / "docs"
EXPORTS_DIR = APP_DIR / "exports"


def _migrate_loose_data() -> None:
    """把「exe 旁边」可能存在的旧数据搬到用户目录。

    只在冻结态生效。用户如果把 exe 放在以前的源码目录里直接双击，
    数据会落在 ``<exe目录>/backend/data``，搬一次免得他以为数据丢了。
    """
    if not IS_FROZEN:
        return
    target = DATA_DIR / "wingman.db"
    if target.exists():
        return
    exe_dir = Path(sys.executable).resolve().parent
    for candidate in (
        exe_dir / "backend" / "data" / "wingman.db",
        exe_dir / "_internal" / "backend" / "data" / "wingman.db",
        exe_dir / "data" / "wingman.db",
    ):
        if candidate.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                import shutil

                shutil.copy2(candidate, target)
            except OSError:
                return
            return

# ---------------------------------------------------------------- 设置模型


class Settings(BaseSettings):
    """从 .env / 环境变量读取的全局设置。字段名小写，即为 cfg() 的键名。"""

    model_config = SettingsConfigDict(
        # 冻结态的 .env 放在用户目录（程序目录只读），开发态仍在仓库里。
        env_file=(
            (APP_DIR / ".env", BACKEND_DIR / ".env")
            if IS_FROZEN
            else (PROJECT_DIR / ".env", BACKEND_DIR / ".env")
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 大模型 ----
    llm_provider: str = "mock"          # openai_compat | ollama | mock
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.8
    llm_max_tokens: int = 2048
    llm_timeout: float = 120.0

    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"

    # ---- 向量模型 ----
    embedder: str = "auto"              # auto | cloud | hash
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = "text-embedding-3-small"
    embed_dim: int = 512

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: str = "*"
    log_level: str = "info"

    # ---- 派生 ----
    @property
    def db_path(self) -> Path:
        return DATA_DIR / "wingman.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。修改 .env 后重启生效。"""
    return Settings()


# 允许前端修改并持久化的键（白名单，避免写入任意字段）
EDITABLE_KEYS: tuple[str, ...] = (
    "llm_provider",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "ollama_host",
    "ollama_model",
    "embedder",
    "embed_base_url",
    "embed_api_key",
    "embed_model",
    "embed_dim",
)

# 从不让前端读回的敏感键（读设置时做掩码）
SECRET_KEYS: frozenset[str] = frozenset(
    {"llm_api_key", "embed_api_key"}
)


def mask_secret(value: str) -> str:
    """把密钥掩码成 sk-abc***xyz 形式。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-3:]}"
