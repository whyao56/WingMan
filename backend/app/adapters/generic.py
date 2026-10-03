"""通用结构化适配器：JSON / JSONL / CSV。

给你一条「自己写脚本导出」的路 —— 不管数据从哪来，
整理成下面任一形式就能进系统：

```json
[
  {"time": "2024-01-01 12:00:00", "who": "小鹿", "content": "哈哈哈今天好累啊"},
  {"time": "2024-01-01 12:00:05", "who": "我",   "content": "怎么啦"}
]
```

字段名不用改，用 options 里的 `field_map` 映射即可：
`{"sender": "who", "ts": "time", "text": "content"}`

CSV 同理，首行是表头。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..schemas import ParsedMsg
from .base import ChatSourceAdapter, clean_sender, guess_msg_type, is_noise, read_text

DEFAULT_MAP = {
    "sender": ("sender", "who", "from", "name", "speaker", "昵称", "发送者", "用户"),
    "ts": ("ts", "time", "timestamp", "datetime", "date", "时间", "日期"),
    "text": ("text", "content", "message", "body", "msg", "内容", "消息"),
}

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
)


def _to_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        # 10 位秒 / 13 位毫秒
        try:
            sec = float(v) / 1000 if float(v) > 1e11 else float(v)
            return datetime.fromtimestamp(sec)
        except (OSError, OverflowError, ValueError):
            return None
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class GenericAdapter(ChatSourceAdapter):
    name = "generic"
    display_name = "通用 JSON / CSV"
    platform = "generic"
    extensions = (".json", ".jsonl", ".ndjson", ".csv", ".tsv")

    # -------------------------------------------------------- 探测

    def sniff(self, path: Path, head: str) -> float:
        ext = path.suffix.lower()
        if ext in (".json", ".jsonl", ".ndjson"):
            s = head.lstrip()
            if s.startswith("[") or s.startswith("{"):
                # 一眼看出带时间字段的才给高分
                if any(k in head[:2000] for k in ('"time"', '"ts"', '"timestamp"', '"时间"')):
                    return 0.85
                return 0.55
        if ext in (".csv", ".tsv"):
            first = head.splitlines()[0] if head else ""
            sep = "\t" if ext == ".tsv" else None
            try:
                cols = next(csv.reader([first], delimiter=sep or ","))
            except Exception:
                return 0.3
            flat = [c.strip().lower() for c in cols]
            if any(c in flat for c in DEFAULT_MAP["ts"]) and any(c in flat for c in DEFAULT_MAP["text"]):
                return 0.85
            return 0.4
        return 0.0

    # -------------------------------------------------------- 字段映射

    @staticmethod
    def _resolve_map(options: dict[str, Any] | None) -> dict[str, str]:
        opts = options or {}
        explicit = opts.get("field_map") or {}

        def pick(role: str, keys: list[str]) -> str | None:
            if role in explicit:
                return explicit[role]
            lowered = {k.lower(): k for k in keys}
            for cand in DEFAULT_MAP[role]:
                if cand in keys:
                    return cand
                if cand.lower() in lowered:
                    return lowered[cand.lower()]
            return None

        return {"sender": pick("sender", list(DEFAULT_MAP["sender"])) or "",
                "ts": pick("ts", list(DEFAULT_MAP["ts"])) or "",
                "text": pick("text", list(DEFAULT_MAP["text"])) or ""}

    def _row_to_parsed(self, row: dict[str, Any], fmap: dict[str, str],
                       me_names: tuple[str, ...]) -> ParsedMsg | None:
        sender = clean_sender(str(row.get(fmap["sender"], "") or ""))
        dt = _to_dt(row.get(fmap["ts"]))
        text = str(row.get(fmap["text"], "") or "").strip()
        if dt is None or not text:
            return None
        if is_noise(text):
            return None
        role = "me" if sender in me_names else None
        return ParsedMsg(sender=sender or "未知用户", ts=dt, text=text,
                         msg_type=guess_msg_type(text), role_hint=role)

    # -------------------------------------------------------- 解析

    def parse(self, path: Path, options: dict[str, Any] | None = None) -> list[ParsedMsg]:
        ext = path.suffix.lower()
        text, _ = read_text(path)
        me_names = self.hint_me_names(options)
        fmap = self._resolve_map(options)
        if not (fmap["ts"] and fmap["text"]):
            # 没有可用的字段映射，交给自动推断兜底
            fmap = self._auto_map_from_rows(self._rows_from_text(text, ext), options)
        rows = self._rows_from_text(text, ext)
        out: list[ParsedMsg] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            p = self._row_to_parsed(row, fmap, me_names)
            if p:
                out.append(p)
        return out

    @staticmethod
    def _rows_from_text(text: str, ext: str) -> list[dict[str, Any]]:
        t = text.lstrip("\ufeff \r\n\t")
        if ext in (".jsonl", ".ndjson"):
            rows: list[dict[str, Any]] = []
            for line in t.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
            return rows
        if ext == ".json":
            try:
                data = json.loads(t)
            except json.JSONDecodeError:
                # 容错：文件里前后有杂字符时，截取第一个 [ 到最后一个 ]
                a, b = t.find("["), t.rfind("]")
                if a == -1 or b == -1:
                    return []
                try:
                    data = json.loads(t[a:b + 1])
                except json.JSONDecodeError:
                    return []
            return GenericAdapter._flatten_json(data)
        # CSV / TSV
        sample = t[:8000]
        delim = "\t" if ext == ".tsv" else ("," if sample.count(",") >= sample.count("\t") else "\t")
        reader = csv.DictReader(io.StringIO(t), delimiter=delim)
        return [dict(r) for r in reader]

    @staticmethod
    def _flatten_json(data: Any) -> list[dict[str, Any]]:
        """支持 [{...}] / {"messages":[...]} / {"chat":{...,"messages":[...]}} 几种嵌套。"""
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("messages", "records", "data", "items", "list", "msgs"):
                v = data.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
            # 单层 dict of dict
            if all(isinstance(v, dict) for v in data.values()):
                return [v for v in data.values()]
        return []

    @staticmethod
    def _auto_map_from_rows(rows: list[dict[str, Any]], options: dict[str, Any] | None) -> dict[str, str]:
        if not rows:
            return {"sender": "", "ts": "", "text": ""}
        # 用全量键名再试一次
        keys = set()
        for r in rows[:50]:
            keys.update(r.keys())
        opts = dict(options or {})
        opts["field_map"] = {}
        adapter = GenericAdapter()
        m = adapter._resolve_map({})
        # 逐键判断类型：值能变 datetime 的是 ts，最长文本的是 text
        ts_key, text_key, sender_key = m["ts"], m["text"], m["sender"]
        for k in keys:
            sample = [r.get(k) for r in rows[:30] if r.get(k) not in (None, "")]
            if not sample:
                continue
            if not ts_key and any(_to_dt(v) for v in sample):
                ts_key = k
                continue
            if not text_key and all(isinstance(v, str) for v in sample):
                if max((len(str(v)) for v in sample), default=0) >= 2:
                    text_key = k
            if not sender_key:
                uniq = {str(v) for v in sample}
                if 1 < len(uniq) <= 30 and all(len(str(v)) <= 20 for v in sample):
                    sender_key = k
        return {"sender": sender_key or "", "ts": ts_key or "", "text": text_key or ""}
