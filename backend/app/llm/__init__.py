"""大模型接入层：云端 OpenAI 兼容 / 本地 Ollama / Mock。"""

from .base import ChatProvider, LLMError, LLMFormatError, parse_json_loose
from .factory import build_llm

__all__ = ["ChatProvider", "LLMError", "LLMFormatError", "parse_json_loose", "build_llm"]
