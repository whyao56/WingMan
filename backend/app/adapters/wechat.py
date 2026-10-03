"""微信聊天记录适配器。

微信没有官方导出，实际拿到的是各种第三方工具的产物，格式不统一。
这里覆盖三种主流排布：

- **A 时间在前**：`2024-01-01 12:00:00 昵称`
- **B 昵称在前**：`昵称 2024-01-01 12:00:00`   （WeChatMsg / 留痕 常见）
- **C 方括号包裹**：`[2024-01-01 12:00:00] 昵称`

解析时先统计整份文件里哪种排布命中更多，再按那种方式切块 ——
比写死一种格式稳得多。
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
    clean_sender,
    guess_msg_type,
    is_noise,
    read_text,
)

RE_WX_MARK = re.compile(r"微信|WeChat|wechat|留痕|WeChatMsg|聊天记录")
RE_BRACKET_TS = re.compile(
    r"^\s*[\[【]\s*(?P<y>\d{4})[-/](?P<mo>\d{1,2})[-/](?P<d>\d{1,2})\s+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*[\]】]\s*(?P<sender>.+?)\s*$"
)
# 微信桌面版导出的「昵称 时间」且中间可能有多个空格 / 制表符
RE_SENDER_TS_TAB = re.compile(
    r"^\s*(?P<sender>[^\t\d][^\t]{0,30})\t+"
    r"(?P<y>\d{4})[-/](?P<mo>\d{1,2})[-/](?P<d>\d{1,2})\s+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*$"
)
RE_WX_META = re.compile(r"^\s*(?:聊天对象|好友|微信号|昵称)[:：]\s*(?P<name>.+?)\s*$", re.M)

_SAMPLE_LINES = 400


class WeChatAdapter(ChatSourceAdapter):
    name = "wechat"
    display_name = "微信聊天记录"
    platform = "wechat"
    extensions = (".txt", ".csv", ".html", ".htm")

    # -------------------------------------------------------- 探测

    def _layout_scores(self, text: str) -> dict[str, int]:
        sample = "\n".join(text.splitlines()[:_SAMPLE_LINES])
        return {
            "ts_first": sum(1 for ln in sample.splitlines() if RE_TS_FIRST.match(ln)),
            "sender_first": sum(1 for ln in sample.splitlines() if RE_SENDER_FIRST.match(ln)),
            "bracket": sum(1 for ln in sample.splitlines() if RE_BRACKET_TS.match(ln)),
            "tab": sum(1 for ln in sample.splitlines() if RE_SENDER_TS_TAB.match(ln)),
        }

    def pick_order(self, text: str) -> str:
        s = self._layout_scores(text)
        # 方括号格式本质也是时间在前，但需要专用正则，这里归到 order 里单独处理
        if s["bracket"] >= 3 and s["bracket"] >= s["ts_first"]:
            return "bracket"
        if s["sender_first"] > s["ts_first"]:
            return "sender_first"
        return "ts_first"

    def sniff(self, path: Path, head: str) -> float:
        score = 0.0
        if RE_WX_MARK.search(head[:1500]):
            score = max(score, 0.8)
        s = self._layout_scores(head)
        best = max(s.values()) if s else 0
        if best >= 3:
            # 结构上像聊天记录，但和 QQ 同构，所以给的置信度低于 QQ
            score = max(score, 0.6)
        if (path.suffix.lower() in self.extensions) and score == 0.0 and best >= 1:
            score = 0.3
        return score

    # -------------------------------------------------------- 解析

    def parse(self, path: Path, options: dict[str, Any] | None = None) -> list[ParsedMsg]:
        text, _ = read_text(path)

        forced = (options or {}).get("order")
        if forced in ("ts_first", "sender_first", "bracket", "tab"):
            order = forced
        else:
            scores = self._layout_scores(text)
            order = max(scores, key=lambda k: scores[k]) if max(scores.values()) >= 1 else "ts_first"

        if order == "bracket":
            return self._parse_bracket(text)
        return self._parse_by_blocks(text, order)

    def _parse_by_blocks(self, text: str, order: str) -> list[ParsedMsg]:
        msgs: list[ParsedMsg] = []
        for m, buf in iter_blocks(text, order=order):
            p = block_to_parsed(m, buf)
            if p:
                msgs.append(p)
        return msgs

    def _parse_bracket(self, text: str) -> list[ParsedMsg]:
        msgs: list[ParsedMsg] = []
        cur: re.Match[str] | None = None
        buf: list[str] = []

        def flush() -> None:
            if cur is None:
                return
            body = "\n".join(buf).strip()
            if is_noise(body):
                return
            from datetime import datetime

            try:
                dt = datetime(
                    int(cur.group("y")), int(cur.group("mo")), int(cur.group("d")),
                    int(cur.group("h")), int(cur.group("mi")), int(cur.group("s") or 0),
                )
            except ValueError:
                return
            msgs.append(ParsedMsg(
                sender=clean_sender(cur.group("sender")) or "未知用户",
                ts=dt,
                text=body,
                msg_type=guess_msg_type(body),
            ))

        for raw in text.splitlines():
            line = raw.rstrip()
            m = RE_BRACKET_TS.match(line)
            if m:
                flush()
                cur, buf = m, []
            elif cur is not None:
                if line.strip() or buf:
                    buf.append(line)
        flush()
        return msgs

    # -------------------------------------------------------- 元信息

    def meta(self, path: Path) -> dict[str, Any]:
        try:
            text, _ = read_text(path)
        except OSError:
            return {}
        head = text[:4000]
        out: dict[str, Any] = {}
        if mm := RE_WX_META.search(head):
            out["peer_name"] = mm.group("name").strip()
        if RE_WX_MARK.search(head[:800]):
            out["source_hint"] = "微信导出"
        return out
