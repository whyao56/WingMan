"""聊天记录接入插件层。"""

from .base import ChatSourceAdapter, read_text, clean_sender, guess_msg_type
from .registry import ADAPTERS, detect, import_file, preview_file, get_adapter

__all__ = [
    "ChatSourceAdapter",
    "read_text",
    "clean_sender",
    "guess_msg_type",
    "ADAPTERS",
    "detect",
    "import_file",
    "preview_file",
    "get_adapter",
]
