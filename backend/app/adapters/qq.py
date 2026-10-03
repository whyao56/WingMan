"""QQ 聊天记录适配器。

覆盖两种最常见的导出方式：

1. **消息管理器导出（.txt / .bak）**
   ```
   ================================================================
   消息记录（此消息记录为文本格式，不支持重新导入）
   ================================================================
   消息分组:我的好友
   消息对象:小鹿(10001)
   ================================================================

   2024-01-01 12:00:00 小鹿(10001)
   哈哈哈今天好累啊

   ```

2. **第三方工具导出（.txt）**
   ```
   2024-01-01 12:00:00 小鹿<10001@qq.com>
   ...
   ```

两种都是「时间在前」，所以复用基类的 `ts_first` 切块器。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..schemas import ParsedMsg
from .base import (
    ChatSourceAdapter,
    RE_SENDER_FIRST,
    RE_TS_FIRST,
    block_to_parsed,
    iter_blocks,
    read_text,
)

RE_META_OBJ = re.compile(
    r"^\s*消息对象[:：]\s*(?P<name>.+?)(?:[\(（]<?\d+[\)）>]?)?\s*$", re.M
)
RE_META_GROUP = re.compile(r"^\s*消息分组[:：]\s*(?P<name>.+?)\s*$", re.M)
RE_MAIL_SENDER = re.compile(r"<\d{4,}@qq\.com>")
RE_QQ_HEADER = re.compile(r"消息记录（此消息记录为文本格式|消息管理器|不支持重新导入")


class QQAdapter(ChatSourceAdapter):
    name = "qq"
    display_name = "QQ 聊天记录"
    platform = "qq"
    extensions = (".txt", ".log", ".bak", ".mht")

    def sniff(self, path: Path, head: str) -> float:
        score = 0.0
        if RE_QQ_HEADER.search(head):
            score = max(score, 0.95)
        if "消息对象" in head or "消息分组" in head:
            score = max(score, 0.9)
        if RE_MAIL_SENDER.search(head) or RE_MAIL_SENDER.search(path.name):
            score = max(score, 0.85)
        if score == 0.0 and path.suffix.lower() in self.extensions:
            # 没有特征词，看头部像不像「时间 昵称」这种结构
            hits = sum(1 for ln in head.splitlines() if RE_TS_FIRST.match(ln))
            if hits >= 3:
                score = 0.45
        return score

    def parse(self, path: Path, options: dict[str, Any] | None = None) -> list[ParsedMsg]:
        opts = dict(options or {})
        text, enc = read_text(path)
        opts["encoding"] = enc

        msgs: list[ParsedMsg] = []
        for m, buf in iter_blocks(text, order="ts_first"):
            p = block_to_parsed(m, buf)
            if p:
                msgs.append(p)
        return msgs

    # -------------------------------------------------------- 元信息

    def meta(self, path: Path) -> dict[str, Any]:
        """读取导出文件头部，拿到「消息对象」这类结构化信息。"""
        try:
            text, _ = read_text(path)
        except OSError:
            return {}
        head = text[:4000]
        out: dict[str, Any] = {}
        if mo := RE_META_OBJ.search(head):
            out["peer_name"] = mo.group("name").strip()
        if mg := RE_META_GROUP.search(head):
            out["group_name"] = mg.group("name").strip()
        return out
