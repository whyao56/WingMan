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
    def _resolve_map(
        options: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None
    ) -> dict[str, str]:
        """按 DEFAULT_MAP 的候选顺序，在**这份数据真实的列名**里挑字段。

        这里曾经有个很隐蔽的错：候选人名表被直接当成了「可用列名」传进来，
        于是 `pick` 永远命中候选表的第一个名字，跟数据里到底有什么列毫无关系。
        表现是 —— 列的排布稍有不同就读不出来：JSON 里时间字段叫 `time`
        （候选表里排第二）选不中，中文表头 `时间 / 内容 / 发送者` 更是一个都不中。

        最坏的地方在于**嗅探和解析说两套话**：`sniff` 看到中文表头给 0.85 的高分，
        用户看到「匹配度很高」，然后一条都没导进去。所以必须拿真实列名去挑。
        """
        opts = options or {}
        explicit = opts.get("field_map") or {}

        # 列名出现过的都算，且保持列顺序（后面的兜底推断也依赖顺序）
        present: list[str] = []
        for r in (rows or [])[:50]:
            for k in r:
                if k not in present:
                    present.append(k)
        lowered = {k.lower(): k for k in present}

        def pick(role: str) -> str:
            if role in explicit:
                return str(explicit[role])
            for cand in DEFAULT_MAP[role]:
                if cand in present:
                    return cand
                if cand.lower() in lowered:
                    return lowered[cand.lower()]
            return ""

        return {"sender": pick("sender"), "ts": pick("ts"), "text": pick("text")}

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
        rows = self._rows_from_text(text, ext)
        fmap = self._resolve_map(options, rows)
        if not (fmap["ts"] and fmap["text"]):
            # 列名一个都没对上（用户自己的叫法），交给按值推断兜底。
            # 注意 `_resolve_map` 必须拿到 rows —— 没有真实列名它就是瞎猜。
            fmap = self._auto_map_from_rows(rows, options)
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
        """列名全都不认识时，按**值的形状**猜哪一列是什么。

        只在 `DEFAULT_MAP` 一个候选都没命中时才走到这里（用户自己的列名，
        比如 `A / B / C` 或 `日期 / 说话人 / 说了什么`）。

        这里原来有两个毛病，一起修掉了：
        1. 用 `set` 遍历列名 —— 顺序不确定，同一份文件两次跑可能得到不同结果。
           现在按列顺序走，并且**先定内容列再定说话人列**：反过来定的话，
           短的文本列会被先认成内容，把真正的消息正文挤掉。
        2. 推断出的「内容列」只要长度 ≥2 就算，于是 `小鹿 / 我` 这种说话人列
           很容易被当成正文，消息就变成了发送者的名字。所以内容列取**平均最长**
           的那一列，说话人列则在剩下的里取**重复度最高**的那一列。
        """
        if not rows:
            return {"sender": "", "ts": "", "text": ""}

        keys: list[str] = []
        for r in rows[:50]:
            for k in r:
                if k not in keys:
                    keys.append(k)  # 保持列顺序；集合的迭代顺序不可依赖

        def sample_of(k: str) -> list[Any]:
            return [r.get(k) for r in rows[:30] if r.get(k) not in (None, "")]

        def avg_len(k: str) -> float:
            vals = [str(v) for v in sample_of(k)]
            return sum(len(v) for v in vals) / max(1, len(vals))

        # 1. 时间列：能解析成 datetime 的取值最多的一列
        timed = [k for k in keys if any(_to_dt(v) for v in sample_of(k))]
        ts_key = max(
            timed, key=lambda k: sum(1 for v in sample_of(k) if _to_dt(v)), default=""
        )

        # 2. 内容列：剩下的列里平均最长的那一列
        rest = [k for k in keys if k != ts_key]
        text_key = max(rest, key=avg_len, default="") if rest else ""

        # 3. 说话人列：再剩下的列里，取值重复度高、且都不长的那个
        others = [k for k in rest if k != text_key]
        sender_key = ""
        if others:
            def distinct(k: str) -> int:
                return len({str(v) for v in sample_of(k)})

            cand = min(others, key=lambda k: (distinct(k), avg_len(k)))
            vals = [str(v) for v in sample_of(cand)]
            if vals and distinct(cand) <= 30 and all(len(v) <= 20 for v in vals):
                sender_key = cand

        return {"sender": sender_key, "ts": ts_key, "text": text_key}
