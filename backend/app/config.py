"""环境配置与路径常量。

优先级：控制台写入的运行时覆盖 > .env > 代码默认值。
运行时覆盖存在 SQLite 的 kv 表里，由 `context.AppContext.cfg()` 负责合并。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------- 路径常量

BACKEND_DIR = Path(__file__).resolve().parent.parent  # chatwing/backend
PROJECT_DIR = BACKEND_DIR.parent                       # chatwing
DATA_DIR = BACKEND_DIR / "data"
FRONTEND_DIR = PROJECT_DIR / "frontend"
SAMPLES_DIR = PROJECT_DIR / "samples"
DOCS_DIR = PROJECT_DIR / "docs"

# ---------------------------------------------------------------- 设置模型


class Settings(BaseSettings):
    """从 .env / 环境变量读取的全局设置。字段名小写，即为 cfg() 的键名。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
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

    # ---- 语音识别 ----
    asr_engine: str = "mock"            # mock | cloud | local
    asr_language: str = "zh"
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = "whisper-1"

    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # ---- 音频采集 ----
    audio_sample_rate: int = 16000
    vad_threshold: float = 0.012
    vad_silence_ms: int = 700
    vad_max_segment_ms: int = 15000

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: str = "*"
    log_level: str = "info"

    # ---- 派生 ----
    @property
    def db_path(self) -> Path:
        return DATA_DIR / "chatwing.db"


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
    "asr_engine",
    "asr_language",
    "asr_base_url",
    "asr_api_key",
    "asr_model",
    "whisper_model",
    "whisper_device",
    "whisper_compute_type",
    "audio_sample_rate",
    "vad_threshold",
    "vad_silence_ms",
    "vad_max_segment_ms",
)

# 从不让前端读回的敏感键（读设置时做掩码）
SECRET_KEYS: frozenset[str] = frozenset(
    {"llm_api_key", "embed_api_key", "asr_api_key"}
)


def mask_secret(value: str) -> str:
    """把密钥掩码成 sk-abc***xyz 形式。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-3:]}"
