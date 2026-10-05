"""读解密后的库，把「行」变成「消息」。

## 为什么是「认列」而不是「写死一份映射」

QQ 的库把列名写成 `40001` / `40050` 这样的数字，微信的列名是英文但库会随版本分片重建。
把某一份列号写死在代码里，等价于赌用户装的就是我见过的那个版本 ——
赌错的表现是**读写都不报错、但时间列取到了消息 ID**，垃圾就这么进了记忆。

所以这里换一条路：**按数据本身认列**。

| 要认的东西 | 判据 | 为什么这个判据成立 |
|---|---|---|
| 时间列 | 整数值，且采样值密集落在 2000~2100 年之间（秒或毫秒） | 秒级时间戳的值域极窄（约 9.4e8 ~ 4.1e9），消息 ID 通常更大或更小 |
| 内容列 | 能从中抽出自然文本（汉字/字母占比高）的 TEXT/BLOB 列 | 消息正文是我们唯一要求「是人话」的字段 |
| 说话人列 | 取值种类很少（2~50 种）的短 id 形状列，且**命中登录账号** | 一个会话里只会有我和对方两个说话人；账号目录名就是登录账号 |
| 会话列 | 短 id 形状、取值种类比说话人列多，且不等于说话人列 | 群/多会话共表时靠它区分这段记录属于谁 |

**认不出来不猜，如实报**：返回候选列 + 每条判据的分数，界面让用户点一下确认。
这条链路上「静默写错」比「多问一句」贵得多。

## 文本抽取的三档，以及为什么最多只到第二档

1. `plain` —— 列里就是纯文本，直接用。
2. `json` —— 列里是 `{"text":"..."}` 这样的元素数组，按顺序取文本字段。
3. `carved` —— 二进制里「捞」出可读片段。**默认关闭**。
   捞出来的东西必然是片段、会丢格式，只适合「总比什么都没有强」的场景，
   所以它必须由人显式打开，且结果会被标出来。

## 会话归属

认出来的会话 id（wxid / QQ 号 / uid）用 `aux names` 换成给人看的称呼：
微信的 `Name2Id` + `Contact`（`remark` 优先于 `nick_name`，因为备注才是用户认得的名字）、
QQ 的 `profile_info` / `group_info`。换不出来就用 id 本身当名字 ——
不编一个「未知联系人」这种看起来像人名的东西。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger("wingman.collect.reader")

# 时间戳值域：2000-01-01 ~ 2100-01-01（UTC 秒）。毫秒就是这个数 × 1000。
TS_MIN = 946_684_800
TS_MAX = 4_102_444_800

# 采样行数。列判据只需要「看几行」，不需要全表 —— 全表扫几十万行会让
# 「探测」这个本该秒回的动作变成几十秒。
SAMPLE_ROWS = 300

_HAS_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_CJK_ALL = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")
_PRINTABLE = re.compile(r"[\x20-\x7e\u00a0-\uffff]")

# JSON 里哪些键装的是「人话」。按这个顺序取，取不到再退到「所有像人话的字符串」。
_TEXT_KEYS = ("text", "content", "summary", "title", "desc", "description",
              "file_name", "filename", "msg", "message", "plain_text", "display")

# 名字候选键：把 id 换成称呼时用
_NAME_KEYS = ("remark", "nick_name", "nickname", "displayname", "display_name",
              "name", "group_name", "card", "alias", "user_name", "username")


# ================================================================ 数据结构


@dataclass
class Column:
    """一列的采样画像。判据全部落在它身上。"""

    name: str
    decl: str = ""
    kind: str = "other"          # int | text | blob | other
    filled: int = 0              # 采样里非空的行数
    distinct: int = 0
    samples: list[Any] = field(default_factory=list)

    def describe(self) -> str:
        head = ", ".join(_short(v) for v in self.samples[:3])
        return f"{self.name}（{self.kind}，{self.distinct} 种取值，如 {head}）"


@dataclass
class Scored:
    """一个候选列加上它的得分和理由。分数是给人看的，不是给人信的黑箱。

    `tags` 是给代码看的结构化标记。**不要用 `why` 的文本去判分支** ——
    这里踩过一次：「命中登录账号」正好是「未命中登录账号」的子串，
    于是「没认出我是谁」被一路当成「认出来了」，说话人归属静默全错。
    人看的文字和机器判的标记必须分开。
    """

    column: str
    score: float
    why: str = ""
    tags: set[str] = field(default_factory=set)


@dataclass
class Mapping:
    """一张表「哪个列是什么」的结论。

    `sender` / `peer` 可能是空的 —— 那代表**没认出来**，而不是「没有这个字段」。
    界面据此决定要不要让用户点一下。
    """

    table: str
    rows: int = 0
    ts: str = ""
    ts_unit: str = "s"           # s | ms
    text: str = ""
    text_kind: str = "plain"     # plain | json | carved
    sender: str = ""
    peer: str = ""
    ext_id: str = ""
    chat: str = ""
    # 说话人列里**确认出现过我自己**。这是「其余说话人都是别人」这条推理的前提：
    # 只有我能被认出来，才能说「不是我的都是对方」。这个前提不成立时
    # （比如根本没给登录账号），把剩下的都算成对方就等于把我的话也算过去了。
    me_confirmed: bool = False
    evidence: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, list[Scored]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.ts and self.text)

    @property
    def needs_sender(self) -> bool:
        return not self.sender

    def describe(self) -> str:
        bits = [f"表 {self.table}（{self.rows} 行）"]
        bits.append(f"时间={self.ts or '未认出'}({self.ts_unit})")
        bits.append(f"正文={self.text or '未认出'}({self.text_kind})")
        bits.append(f"说话人={self.sender or '未认出'}"
                    + ("（确认含我）" if self.me_confirmed else ""))
        if self.peer:
            bits.append(f"对方={self.peer}")
        return "，".join(bits)


@dataclass
class RawMessage:
    """一条从库里读出来的消息（还没落库）。"""

    ts: datetime
    text: str
    sender_id: str = ""
    sender_name: str = ""
    peer_id: str = ""
    role: str = ""               # me | peer | ""（没认出来）
    ext_id: str = ""
    msg_type: str = "text"
    text_kind: str = "plain"

    @property
    def unknown_role(self) -> bool:
        return not self.role


@dataclass
class ReadResult:
    messages: list[RawMessage] = field(default_factory=list)
    mapping: Mapping | None = None
    skipped_unknown_role: int = 0
    skipped_empty: int = 0
    skipped_bad_ts: int = 0
    carved: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.mapping is None:
            return "没有认出可用的消息表。"
        s = (f"{self.mapping.table}：读到 {len(self.messages)} 条"
             f"（跳过无正文 {self.skipped_empty}、"
             f"说话人未定 {self.skipped_unknown_role}、时间异常 {self.skipped_bad_ts}）")
        if self.carved:
            s += f"；其中 {self.carved} 条正文是从二进制里捞出来的"
        return s


# ================================================================ 工具


def _short(value: Any, limit: int = 28) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} 字节二进制>"
    s = str(value).replace("\n", "⏎")
    return s[:limit] + ("…" if len(s) > limit else "")


def _norm_id(value: Any) -> str:
    """id 归一化：转字符串、去空白、小写。

    大小写要归一：同一个 wxid 在不同表里大小写不一定一致，
    拿来比对时区分大小写会「明明是同一个人却认不出来」。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "ignore").strip().lower()
        except Exception:  # pragma: no cover - 解码不会抛，兜底
            return ""
    return str(value).strip().lower()


def _id_shape(value: Any) -> bool:
    """像不像一个 id：短的、没有空白的字符串；或是小整数。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value < 10 ** 13
    if isinstance(value, (bytes, bytearray)):
        return 0 < len(value) <= 64
    if isinstance(value, str):
        s = value.strip()
        if not s or len(s) > 64:
            return False
        return not any(ch.isspace() for ch in s)
    return False


def ts_from_number(value: Any) -> tuple[datetime | None, str]:
    """把一个整数解成时间。返回 (时间, 单位)。解不出来返回 (None, "")。"""
    if isinstance(value, bool):
        return None, ""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = int.from_bytes(value[:8], "big")
        except Exception:
            return None, ""
    if isinstance(value, str):
        s = value.strip()
        if not s.isdigit():
            return None, ""
        value = int(s)
    if not isinstance(value, int):
        return None, ""
    if TS_MIN <= value <= TS_MAX:
        return datetime.fromtimestamp(value, tz=timezone.utc), "s"
    if TS_MIN * 1000 <= value <= TS_MAX * 1000:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc), "ms"
    return None, ""


# ================================================================ 文本抽取


def _looks_like_text(s: str) -> float:
    """一段字符串「像人话」的程度，0~1。

    判据用「可打印且非控制字符」的占比，加上汉字的加成 ——
    汉字是决定性的信号：QQ/微信的消息正文里汉字很常见，
    而随手解出来的二进制里几乎不会有连续汉字。
    """
    if not s:
        return 0.0
    good = len(_PRINTABLE.findall(s))
    ratio = good / len(s)
    if _HAS_CJK.search(s):
        ratio = min(1.0, ratio + 0.35)
    return ratio


def _json_text(value: str) -> str:
    """从 JSON 里按顺序取「人话」字段。取不到返回空串。"""
    if not value or value[0] not in "[{":
        return ""
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return ""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in _TEXT_KEYS:
                if key in node and isinstance(node[key], str) and node[key].strip():
                    out.append(node[key])
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return "\n".join(x for x in out if x.strip())


def _carve_text(blob: bytes) -> str:
    """从二进制里捞出可读片段。

    **只认含汉字、且汉字占比够高的串**：二进制里 ASCII 短串遍地都是，
    按 ASCII 捞会捞出一堆 `sqlite` / `index` 之类的垃圾；连续汉字在二进制里
    基本只可能来自消息正文本身。这条限制同时也是「不许凭空造文本」的保证 ——
    随机字节按 UTF-16 解能凑出大段「像汉字」的东西（16 位的值落在
    0x3000~0x9FFF 太容易了），所以**只试 UTF-8**。真实的二进制正文
    （protobuf 字符串字段、JSON）本来就是 UTF-8。
    """
    try:
        text = blob.decode("utf-8", "ignore")
    except UnicodeDecodeError:  # pragma: no cover - errors=ignore 下不会抛
        return ""
    return _runs_with_cjk(text)


def _runs_with_cjk(text: str) -> str:
    """在文本里切出「成句」的片段。"""
    runs: list[str] = []
    cur: list[str] = []
    for ch in text:
        if ch.isprintable() or ch in "\n\t":
            cur.append(ch)
        else:
            if cur:
                runs.append("".join(cur))
                cur = []
    if cur:
        runs.append("".join(cur))

    keep: list[str] = []
    for raw in runs:
        run = raw.strip()
        if len(run) < 3:
            continue
        cjk = len(_HAS_CJK.findall(run))
        # 要求「有汉字」且「汉字占比不低」：这一条挡住随机字节凑出来的零星汉字
        if cjk >= 2 and cjk / len(run) >= 0.4:
            keep.append(run)
    return " ".join(keep).strip()


def extract_text(value: Any, *, allow_carve: bool = False) -> tuple[str, str]:
    """把一列的值变成消息正文。返回 (文本, 来源)。

    来源取值：`plain`（本来就能读）/ `json`（从元素里取出来的）/
    `carved`（从二进制里捞的，只保证「有东西」）/ `""`（取不出来）。
    """
    if value is None:
        return "", ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "", ""
        if s[0] in "[{":
            got = _json_text(s)
            if got:
                return got, "json"
        return s, "plain"
    if isinstance(value, (bytes, bytearray)):
        blob = bytes(value)
        if not blob:
            return "", ""
        # 先试着当 UTF-8 文本读。**必须同时要求「没有控制字符」**：
        # protobuf 正文里夹着 `\x0a\x12` 这种长度前缀，按可打印占比算仍然很高
        # （汉字占多数），不加这条就会把带控制字符的二进制当成纯文本来用。
        try:
            as_text = blob.decode("utf-8")
        except UnicodeDecodeError:
            as_text = ""
        if (as_text and as_text.strip() and not _has_control(as_text)
                and _looks_like_text(as_text) > 0.85):
            inner, kind = extract_text(as_text.strip(), allow_carve=False)
            if inner:
                return inner, kind or "plain"
        if allow_carve:
            got = _carve_text(blob)
            if got:
                return got, "carved"
        return "", ""
    return str(value), "plain"


def _has_control(s: str) -> bool:
    """有没有控制字符（换行/制表除外）。有的话它就不是一段人写的文本。"""
    return any(ch < " " and ch not in "\n\r\t" for ch in s)


# ================================================================ 表与列的画像


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [(r[1], (r[2] or "").upper()) for r in rows]


def table_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.DatabaseError:
        return 0


def profile_columns(conn: sqlite3.Connection, table: str, *,
                    sample: int = SAMPLE_ROWS) -> dict[str, Column]:
    """给每列做个采样画像。

    取**最新的 N 行**而不是最老的：库的格式是跟着客户端版本走的，
    最近的行比三年前的行更能代表当前格式。
    """
    cols: dict[str, Column] = {}
    for name, decl in table_columns(conn, table):
        kind = "int" if "INT" in decl else (
            "text" if any(k in decl for k in ("CHAR", "CLOB", "TEXT")) else (
                "blob" if "BLOB" in decl or decl == "" else "other"))
        cols[name] = Column(name=name, decl=decl, kind=kind)

    try:
        rows = conn.execute(
            f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT ?', (sample,)
        ).fetchall()
    except sqlite3.DatabaseError:
        # 没有 rowid 的表（WITHOUT ROWID）或视图，退化成不排序
        try:
            rows = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (sample,)).fetchall()
        except sqlite3.DatabaseError:
            return cols

    names = list(cols)
    for row in rows:
        for i, name in enumerate(names):
            if i >= len(row):
                break
            v = row[i]
            if v is None or v == "" or v == b"":
                continue
            col = cols[name]
            col.filled += 1
            # 声明类型不可全信：QQ 的库大量用 BLOB 声明装整数，也有干脆不声明的。
            # 所以「值说了算」——但也只在声明不明确时改，别把 TEXT 列改成 int。
            if isinstance(v, (bytes, bytearray)):
                if col.kind == "other":
                    col.kind = "blob"
            elif isinstance(v, bool):
                pass
            elif isinstance(v, int):
                if col.kind in ("other", "blob"):
                    col.kind = "int"
            elif isinstance(v, str):
                if col.kind == "other":
                    col.kind = "text"
            if len(col.samples) < SAMPLE_ROWS:
                col.samples.append(v)
    for col in cols.values():
        try:
            col.distinct = len({_norm_id(v) if _id_shape(v) else _short(v, 200)
                                for v in col.samples})
        except TypeError:  # pragma: no cover
            col.distinct = len(col.samples)
    return cols


def _score_ts(col: Column) -> Scored | None:
    if col.filled == 0 or col.kind not in ("int", "blob", "other"):
        return None
    hits = 0
    units: list[str] = []
    for v in col.samples:
        dt, unit = ts_from_number(v)
        if dt is not None:
            hits += 1
            units.append(unit)
    if not hits:
        return None
    ratio = hits / col.filled
    if ratio < 0.6:
        return None
    unit = "ms" if units.count("ms") > units.count("s") else "s"
    # 越纯越好：一个 0.6 的列很可能是「消息 ID 里恰好有一串像时间的数」
    return Scored(column=col.name, score=ratio,
                  why=f"{hits}/{col.filled} 个采样值落在 2000~2100 年区间（{unit}）")


_TOKEN_LIKE = re.compile(r"^[\w.:@/\\-]{4,}$")


def _is_token_like(s: str) -> bool:
    """像不像「标识符」而不是「一句话」。

    判据：没有空格、没有汉字、整段由标识符字符组成、且不短。
    用途是**挡住说话人 id 列被认成正文列** —— `u_meUidAAA` / `wxid_abc123`
    这类值按「可打印占比」算分数很高，短消息（「在吗」）反而低，
    不单独挡一下，正文列就会选错。
    汉字一律不算标识符，因为中文消息里本来就不带空格。
    """
    if _HAS_CJK.search(s) or any(ch.isspace() for ch in s):
        return False
    return bool(_TOKEN_LIKE.match(s))


def _score_text(col: Column, *, allow_carve: bool) -> Scored | None:
    """一个列有多像「消息正文」。

    三道否决（都是实测踩出来的）：
    - **整数列不可能是消息正文**。不挡这条的话，时间列会被选成正文列 ——
      时间是「能抽出 10 个字符、100% 可打印」的，按分数比正文还高，
      于是整库的消息正文全变成一串时间戳。
    - **常量列不算正文**：`settings.value` 这种一列只有一个值的表不是消息表。
    - **标识符形状的文本不算正文**：说话人 id 列长得比短消息还「像人话」。
    """
    if col.filled == 0 or col.kind == "int":
        return None
    if col.filled >= 3 and col.distinct <= 1:
        return None
    ok = 0
    cjk_hits = 0
    lengths: list[int] = []
    kinds: list[str] = []
    for v in col.samples:
        got, kind = extract_text(v, allow_carve=allow_carve)
        if not got or _looks_like_text(got) < 0.7:
            continue
        if not any(ch.isalpha() or _HAS_CJK.match(ch) for ch in got):
            continue          # 全是数字和标点的不是人话
        if _is_token_like(got):
            continue          # 像标识符，不像一句话
        ok += 1
        lengths.append(len(got))
        kinds.append(kind)
        if _HAS_CJK.search(got):
            cjk_hits += 1
    if not ok:
        return None
    ratio = ok / col.filled
    if ratio < 0.5:
        return None
    avg = sum(lengths) / len(lengths)
    kind = max(set(kinds), key=kinds.count)
    span = min(avg, 40) / 40
    cjk_share = cjk_hits / ok
    return Scored(
        column=col.name,
        score=ratio * 0.5 + span * 0.3 + cjk_share * 0.2,
        why=(f"{ok}/{col.filled} 个采样值能抽出正文，平均 {avg:.0f} 字，"
             f"形式 {kind}" + (f"，{cjk_share:.0%} 含中文" if cjk_share else "")),
    )


def _score_id(col: Column, me_ids: Sequence[str],
              resolve: "Callable[[Any], str] | None" = None,
              index: NameIndex | None = None,
              *, require_varying: bool = True) -> Scored | None:
    """一个列有多像「说话人列」。

    判据（按权重从大到小）：

    1. **这一列的值域是不是「人」**（`known_ratio`，权重最高）——
       取值里有多大比例能落到对照表确认过的人身上。
       这条是主判据，理由见 `NameIndex.is_person` 的注释：靠在登录账号上的
       「命中率」会被恒为 1 的标志位列骗过去。
    2. **有没有命中登录账号**（`me_ratio`）——
       有就说明这一列的取值空间包含「我」，是很强的正面信号。
    3. 取值种类少（<= 8）—— 一个会话里说话人只有两三种。

    还有三条否决：值必须是 id 形状；必须**不是时间列**（时间戳同样是
    「整数 + 取值种类不多」）；必须**至少有 2 种取值**（`local_type` 恒为 1，
    它是标志位不是说话人）。

    证据全无的列返回 None —— 一条依据都没有的时候，「猜一个最像的」
    等于把归属错误藏起来。
    """
    if col.filled == 0:
        return None
    shaped = sum(1 for v in col.samples if _id_shape(v))
    if shaped < col.filled * 0.8:
        return None
    if col.distinct == 0 or col.distinct > 512:
        return None
    if require_varying and col.distinct < 2:
        return None
    if _score_ts(col) is not None:
        return None

    me = {_norm_id(x) for x in me_ids if x}
    solved = [resolve(v) if resolve else _norm_id(v) for v in col.samples]
    solved = [_norm_id(x) for x in solved]
    me_ratio = sum(1 for x in solved if x in me) / col.filled if me else 0.0
    distinct_solved = set(solved)
    known_ids = (index.known if index is not None else set())
    known_ratio = (sum(1 for x in distinct_solved
                       if x in known_ids or x in me) / len(distinct_solved)
                   if distinct_solved else 0.0)
    if me_ratio == 0.0 and known_ratio == 0.0:
        return None

    score = 0.55 * known_ratio + 0.25 * me_ratio
    if col.distinct <= 8:
        score += 0.20
    why = (f"{col.distinct} 种取值，{known_ratio:.0%} 落在已知的人是值域里"
           + (f"，{me_ratio:.0%} 命中登录账号" if me else ""))
    tags = {"me_hit"} if me_ratio > 0 else set()
    return Scored(column=col.name, score=min(1.0, score), why=why, tags=tags)


def infer_mapping(
    conn: sqlite3.Connection,
    table: str,
    *,
    me_ids: Sequence[str] = (),
    name_index: NameIndex | None = None,
    allow_carve: bool = False,
    sample: int = SAMPLE_ROWS,
) -> Mapping:
    """认出这张表里哪个列是什么。**不猜**：认不出来就把候选和理由一起交出去。

    `name_index` 要在认列**之前**建好并传进来：微信那种「说话人存 rowid」的库，
    不先有对照表就永远认不出哪个列是说话人（对比不出账号）。
    """
    m = Mapping(table=table, rows=table_rows(conn, table))
    cols = profile_columns(conn, table, sample=sample)
    if not cols:
        m.problems.append("读不到这张表的列信息。")
        return m

    resolve = (name_index.resolve if name_index is not None else None)
    ts_scores = [s for s in (_score_ts(c) for c in cols.values()) if s]
    text_scores = [s for s in (_score_text(c, allow_carve=allow_carve)
                               for c in cols.values()) if s]
    id_scores = [s for s in (_score_id(c, me_ids, resolve, name_index)
                             for c in cols.values()) if s]

    ts_scores.sort(key=lambda s: s.score, reverse=True)
    text_scores.sort(key=lambda s: s.score, reverse=True)
    id_scores.sort(key=lambda s: s.score, reverse=True)
    m.candidates = {"ts": ts_scores, "text": text_scores, "id": id_scores,
                    "ext_id": []}

    if ts_scores:
        m.ts = ts_scores[0].column
        m.evidence["ts"] = ts_scores[0].why
        for v in cols[m.ts].samples:
            _, unit = ts_from_number(v)
            if unit:
                m.ts_unit = unit
                break
    else:
        m.problems.append("没有认出一条像样的时间列 —— 这张表可能不是消息表。")

    if text_scores:
        m.text = text_scores[0].column
        m.evidence["text"] = text_scores[0].why
        for v in cols[m.text].samples:
            got, kind = extract_text(v, allow_carve=allow_carve)
            if got:
                m.text_kind = kind or "plain"
                break
    else:
        m.problems.append("没有认出一条能抽出正文的列。")

    # 说话人：取证据最强的那一列
    if id_scores:
        m.sender = id_scores[0].column
        m.evidence["sender"] = id_scores[0].why
        m.me_confirmed = "me_hit" in id_scores[0].tags
        if not m.me_confirmed:
            m.problems.append(
                f"说话人列是 `{m.sender}`，但里面没出现过登录账号 —— "
                "「不是我说的就等于对方说的」这条推理因此不成立，"
                "先说清「哪个 id 是我」再采。")
    else:
        m.problems.append(
            "认不出哪一列是说话人：库里的 id 列没有一个能对上登录账号、"
            "也对不上任何已知联系人。请在这里指定说话人列"
            "（或先补上「我是谁」这个 id）。")

    # 「对方 / 会话对象」列：值域被说话人列包住、且翻译成人之后不是我自己的那一列
    if m.sender:
        m.peer, overlap_note = _pick_peer_column(cols, m.sender, me_ids, resolve)
        if overlap_note:
            m.evidence["peer"] = overlap_note
    if not m.sender and id_scores:
        # 认不出「我」，那就把最像说话人的一列放上去，但**留着 needs_sender**
        m.evidence["sender_hint"] = id_scores[0].why

    # 消息自身的 id（去重用）。挑一个高基数、整数、不在时间域里的列。
    ext_scores: list[Scored] = []
    for c in cols.values():
        if c.name in (m.ts, m.text, m.sender, m.peer):
            continue
        if c.kind != "int" or c.distinct < max(2, c.filled - 1):
            continue
        if _score_ts(c) is not None:
            continue
        ext_scores.append(Scored(column=c.name, score=0.7,
                                 why=f"每行都不同的整数（{c.distinct} 种），适合当消息 id"))
    ext_scores.sort(key=lambda s: s.score, reverse=True)
    m.candidates["ext_id"] = ext_scores
    if ext_scores:
        m.ext_id = ext_scores[0].column
        m.evidence["ext_id"] = ext_scores[0].why
    return m


def _pick_peer_column(cols: dict[str, Column], sender: str,
                      me_ids: Sequence[str] = (),
                      resolve: "Callable[[Any], str] | None" = None,
                      ) -> tuple[str, str]:
    """在剩下的列里挑出「对方 / 会话对象」那一列。挑不出来返回空串（不猜）。

    两条判据一起用：

    1. **这一列的取值至少有 90% 出现在说话人列的取值集合里。**
       理由：一个会话里说话人只可能是「我」和「对方」两个人，
       所以「对方」那一列的取值必然被说话人列的值域包住。
       留 10% 余量是因为两列各自按采样行取值，采样不全时会有零星值对不上。
    2. **翻译成人之后不能是我自己。** 这条是必须的：微信消息表里有个
       `local_type`，而 `Name2Id` 的 rowid 1 恰好是我，
       只看「值域包含」的话它会 100% 命中 —— 于是「对方」被认成我自己，
       所有消息会被归到同一个错误的会话里。

    **不走 `_score_id` 的证据门槛**：QQ 单聊表里的会话对象列在同一段记录里
    恒等于对方，是个只有一种取值的常量列，而常量列恰恰是被说话人判据否决的。
    这是两类不同的问题（「谁在说话」vs「这是跟谁的会话」），判据必须分开。
    """
    sender_vals = {_norm_id(v) for v in cols[sender].samples if _id_shape(v)}
    if not sender_vals:
        return "", ""
    me = {_norm_id(x) for x in me_ids if x}
    best, best_cover, best_note = "", 0.0, ""
    for name, col in cols.items():
        if name == sender or col.filled == 0:
            continue
        if _score_ts(col) is not None:
            continue
        shaped = sum(1 for v in col.samples if _id_shape(v))
        if shaped < col.filled * 0.8:
            continue
        vals = {_norm_id(v) for v in col.samples if _id_shape(v)}
        if not vals:
            continue
        if me:
            translated = {_norm_id(resolve(v) if resolve else _norm_id(v)) for v in vals}
            if len(translated & me) / len(translated) > 0.1:
                continue
        inside = len(vals & sender_vals)
        cover = inside / len(vals)
        if cover > best_cover:
            best, best_cover = name, cover
            best_note = (f"{len(vals)} 种取值中 {inside} 种出现在说话人列的取值集合里"
                         f"（{cover:.0%}）")
    if best_cover >= 0.9:
        return best, best_note
    return "", ""


def pick_message_tables(
    conn: sqlite3.Connection,
    *,
    me_ids: Sequence[str] = (),
    allow_carve: bool = False,
    limit: int = 8,
    min_rows: int = 5,
) -> tuple[list[Mapping], list[tuple[str, str]]]:
    """在库里挑出「像消息表」的表。

    返回 (认出来的映射按可用性排序, 被跳过的表及原因)。
    每张表都要能认出时间列 + 正文列才算数；只认出一半的表不算，
    但它的名字会被记下来 —— 用户报告问题时这个列表是最有用的线索。

    `min_rows` 这道门槛是为了挡住 `settings` 这种「有 `updated_at` 时间列、
    有几列短文本」的配置表：它能满足「认得出时间和正文」，
    但它只有一行、名字也不像消息表，装不下聊天记录。
    """
    good: list[Mapping] = []
    skipped: list[tuple[str, str]] = []
    # 顺序不能反：先有「id ↔ 称呼」对照表，才可能认出「说话人列」
    name_index = build_name_index(conn, me_ids)
    for t in list_tables(conn):
        # FTS 的影子表和索引表一定不是消息表，跳过能省很多时间
        if t.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
            continue
        rows = table_rows(conn, t)
        if rows == 0:
            continue
        if rows < min_rows and not _name_hints_message(t):
            skipped.append((t, f"只有 {rows} 行，名字也不像消息表"))
            continue
        m = infer_mapping(conn, t, me_ids=me_ids, name_index=name_index,
                          allow_carve=allow_carve)
        if m.usable:
            # 行数多的更像消息主表
            good.append(m)
        else:
            skipped.append((t, "；".join(m.problems) or "没有认出来"))
    good.sort(key=lambda m: (-m.rows, m.table))
    return good[:limit], skipped


# 表名里带这些字样的，才允许「行数少」也算消息表
_MESSAGE_HINTS = ("msg", "message", "chat", "c2c", "group", "buddy", "talk")


def _name_hints_message(table: str) -> bool:
    low = table.lower()
    return any(h in low for h in _MESSAGE_HINTS)


# ================================================================ 辅助表：id → 称呼


@dataclass
class NameIndex:
    """id → 规范 id → 人话称呼 的两级对照。

    为什么必须两级：微信的消息表里存的是**整数** `real_sender_id`，
    它指向 `Name2Id` 表的 rowid，而 `Name2Id` 里那一列才是 wxid。
    所以「消息里的说话人」到「wxid」要过一跳，到「用户认得的名字」还要再过一跳
    （`Contact.remark` 才是备注名，`nick_name` 只是对方自己起的昵称）。

    做成两级而不是直接拍平成「整数 → 备注名」，是因为判断「这句是不是我说的」
    必须拿 wxid 去比登录账号，拿备注名比是比不出来的。
    """

    canon: dict[str, str] = field(default_factory=dict)     # 原始 id → 规范 id
    display: dict[str, str] = field(default_factory=dict)   # 规范 id → 称呼
    known: set[str] = field(default_factory=set)            # 确认是「人」的规范 id
    sources: list[str] = field(default_factory=list)

    def resolve(self, raw: Any) -> str:
        norm = _norm_id(raw)
        return self.canon.get(norm, norm)

    def name_of(self, canonical: str) -> str:
        return self.display.get(canonical, "")

    def is_person(self, raw: Any, me_ids: Sequence[str] = ()) -> bool:
        """这个值翻译之后是不是一个「已知的人」。

        这是认说话人列的**主判据**：一个列的值是不是都落在「人」的值域里。
        为什么不用「命中登录账号的比例」当主判据：`local_type` 这种标志位列
        恒为 1，而 `Name2Id` 的 rowid 1 恰好是我 —— 它的命中率（75%）
        比真实说话人列（我只说了四句里的一句，25%）还高。
        换成「值域是不是人的值域」，标志位列立刻露馅：它还有一个值 3 谁都不认识。
        """
        solved = self.resolve(raw)
        return solved in self.known or solved in {_norm_id(x) for x in me_ids}

    def __len__(self) -> int:
        return len(self.display)


def _collect_name_pairs(conn: sqlite3.Connection, table: str) -> tuple[list[tuple[str, str]], str]:
    """从一张表里挖出「id ↔ 称呼」的关系。返回 (对, 形态)。

    两种形态，**语义完全不同**，混起来会让说话人归属全错：

    | 形态 | 表长什么样 | 每对的含义 |
    |---|---|---|
    | `rowid_map` | 只有**一列**（微信 `Name2Id`） | `(rowid, 那一列的值)` —— 右边的值才是 id |
    | `alias` | 两列，一列 id 一列名字（`Contact` / `profile_info`） | `(id, 名字)` —— 左边就是 id |

    为什么必须分开：微信消息表里存的是整数 `real_sender_id`，它指向 `Name2Id`
    的 rowid。如果把 `rowid_map` 当成 `alias` 处理，就会得出
    「id=1 的人叫 wxid_abc」这种反过来的结论 —— 于是每一句的说话人都错位，
    而且错得毫无迹象。
    """
    cols = profile_columns(conn, table, sample=200)
    if not cols:
        return [], ""
    names = list(cols)

    if len(names) == 1:
        col = cols[names[0]]
        if not col.filled:
            return [], ""
        try:
            rows = conn.execute(
                f'SELECT rowid, "{col.name}" FROM "{table}" LIMIT 4000').fetchall()
        except sqlite3.DatabaseError:
            return [], ""
        out: list[tuple[str, str]] = []
        for rid, val in rows:
            if isinstance(val, str) and val.strip() and _id_shape(val):
                out.append((str(rid), val.strip()))
        return (out, "rowid_map") if out else ([], "")

    id_cols = [c.name for c in cols.values()
               if c.filled and all(_id_shape(v) for v in c.samples[:40])]
    name_cols = [c.name for c in cols.values()
                 if c.filled and any(_has_name_hint(v) for v in c.samples[:40])]
    name_cols.sort(key=lambda n: _name_rank(n))
    if not id_cols or not name_cols:
        return [], ""
    # id 列要挑「能当主键」的那一列：取值种类多的更像 id，
    # 而 `type` / `status` 这种只有两三种取值的是标志位不是 id。
    id_cols.sort(key=lambda n: -cols[n].distinct)
    pick_id = id_cols[0]
    pick_name = next((n for n in name_cols if n != pick_id), "")
    if not pick_name:
        return [], ""
    try:
        rows = conn.execute(
            f'SELECT "{pick_id}", "{pick_name}" FROM "{table}" LIMIT 4000').fetchall()
    except sqlite3.DatabaseError:
        return [], ""
    pairs = [(str(a), str(b).strip()) for a, b in rows
             if a is not None and b is not None and str(b).strip()]
    return (pairs, "alias") if pairs else ([], "")


def build_name_index(conn: sqlite3.Connection, me_ids: Sequence[str] = ()) -> NameIndex:
    """扫一遍库里所有「id ↔ 称呼」的对照表，合成一个索引。

    为什么在整库里找而不是写死表名：微信有 `Contact`，QQ 有 `profile_info` /
    `group_info`，跨版本表名还会变。但它们的形状是稳定的 ——
    一个短 id 列配一个名字列（或者干脆单列 + rowid）。按形状找，比按表名找活得久。

    两遍扫描：先建立 `rowid → id` 的映射（否则后面拿 rowid 找不到人），
    再补 `id → 称呼`。顺序不能反 —— 消息表里的数字 id 只有先过了第一跳
    才可能被认出来。
    """
    index = NameIndex()
    me = {_norm_id(x) for x in me_ids if x}
    me.add("")
    found: list[tuple[str, list[tuple[str, str]], str]] = []
    for t in list_tables(conn):
        if t.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
            continue
        # **消息表本身绝不能进对照表**。它也有「短 id 列 + 文本列」的形状，
        # 而且它的文本列（消息正文）比真正的备注名「更像名字」。
        # 一旦把 `local_id → 某句话` 收进来，就会得出
        # 「id=3 的人叫『嗯，你呢？』」这种结论，并顺着把说话人列也认错。
        if _name_hints_message(t):
            continue
        pairs, kind = _collect_name_pairs(conn, t)
        if pairs:
            found.append((t, pairs, kind))

    # 第一遍：rowid → id
    for t, pairs, kind in found:
        index.sources.append(f"{t}({len(pairs)}:{kind})")
        if kind != "rowid_map":
            continue
        for rid, canon_raw in pairs:
            canon = _norm_id(canon_raw)
            key = _norm_id(rid)
            if canon and key:
                index.canon[key] = canon
                index.display.setdefault(canon, canon)
                index.known.add(canon)

    # 第二遍：id → 称呼
    for t, pairs, kind in found:
        if kind != "alias":
            continue
        for raw, name in pairs:
            canon = index.resolve(raw)
            if not canon:
                continue
            # alias 表本身就是「人的名单」：无论有没有备注名，这个 id 都算确认是人
            index.known.add(canon)
            if canon in me or name in ("", canon):
                continue
            if not index.display.get(canon, "") or index.display[canon] == canon:
                index.display[canon] = name
    return index


def _has_name_hint(value: Any) -> bool:
    """像不像一个「名字」：是有内容、可打印、且不是纯数字的短文本。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not (1 <= len(s) <= 64) or s.isdigit():
        return False
    return any(ch.isalnum() or _HAS_CJK.match(ch) for ch in s)


def _name_rank(column_name: str) -> int:
    low = column_name.lower()
    for i, key in enumerate(_NAME_KEYS):
        if key in low:
            return i
    return len(_NAME_KEYS)


# ================================================================ 读消息


def read_messages(
    conn: sqlite3.Connection,
    mapping: Mapping,
    *,
    name_index: NameIndex | None = None,
    me_ids: Sequence[str] = (),
    since: "datetime | None" = None,
    until: "datetime | None" = None,
    limit: int = 20000,
    assume_peer_when_unknown: bool = False,
    allow_carve: bool = False,
) -> ReadResult:
    """按认出来的映射读消息。

    `assume_peer_when_unknown` 的默认值是 **False**：认不出说话人时，
    这条消息会被跳过并计数，而不是默认算成对方说的。因为「把我说的话算到
    对方头上」会让后续所有的关系分析都反着来，而用户看不出来。
    预览界面上会显示跳过了多少条，用户确认「剩下的都是对方说的」时才打开它。
    """
    res = ReadResult(mapping=mapping)
    if not mapping.usable:
        res.problems.append("这张表没有认出时间列或正文列，读不了。")
        return res

    sel = [f'"{mapping.ts}"', f'"{mapping.text}"']
    for c in (mapping.sender, mapping.peer, mapping.ext_id, mapping.chat):
        if c and c not in sel:
            sel.append(f'"{c}"')
    keys = [s.strip('"') for s in sel]
    sql = f'SELECT {", ".join(sel)} FROM "{mapping.table}" ORDER BY "{mapping.ts}"'
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.DatabaseError as exc:
        res.problems.append(f"读表失败：{exc}")
        return res

    index = name_index or NameIndex()
    me = {_norm_id(x) for x in me_ids if x}

    for row in rows:
        if len(res.messages) >= limit:
            res.problems.append(f"达到单次上限 {limit} 条，剩下的下次再采。")
            break
        rec = dict(zip(keys, row))
        dt, _ = ts_from_number(rec.get(mapping.ts))
        if dt is None:
            res.skipped_bad_ts += 1
            continue
        if since and dt < since:
            continue
        if until and dt > until:
            continue
        text, kind = extract_text(rec.get(mapping.text), allow_carve=allow_carve)
        if not text.strip():
            res.skipped_empty += 1
            continue
        if kind == "carved":
            res.carved += 1

        # 说话人要**过一级翻译**：消息里存的可能是 Name2Id 的 rowid
        raw_sender = _norm_id(rec.get(mapping.sender)) if mapping.sender else ""
        sender_id = index.resolve(raw_sender) if raw_sender else ""
        peer_id = _norm_id(rec.get(mapping.chat)) if mapping.chat else ""
        if not peer_id and mapping.peer:
            peer_id = index.resolve(rec.get(mapping.peer))
        if not peer_id:
            peer_id = _norm_id(rec.get(mapping.peer)) if mapping.peer else ""

        role = ""
        if sender_id and sender_id in me:
            role = "me"
        elif sender_id and (sender_id == peer_id
                            or (mapping.me_confirmed and sender_id not in me)):
            # 判「这是对方说的」有两条依据：
            #   ① 说话人就是这一行的会话对象列（单聊表里那一列恒等于对方）；
            #   ② **我已经确认在这张表的说话人列里出现过**，而这一行的说话人不是我。
            #      一个会话的参与者只有我和对方，既然这张表的说话人值域里确定有「我」，
            #      那不是我的人就只能是对方。前提不成立时（没给登录账号）这条路走不通，
            #      消息会被跳过并计数 —— 宁可少采，也不要把我的话算到别人头上。
            role = "peer"
        if not role and sender_id and assume_peer_when_unknown:
            role = "peer"
        if not role:
            res.skipped_unknown_role += 1
            continue

        if role == "peer" and not peer_id:
            peer_id = sender_id
        # 「我」在界面上就叫「我」。显示成 wxid / uid 对用户毫无意义，
        # 而且他会以为没认出来。
        sender_name = ("我" if role == "me"
                       else index.name_of(sender_id) or sender_id or "对方")

        res.messages.append(RawMessage(
            ts=dt, text=text.strip(),
            sender_id=sender_id, sender_name=sender_name,
            peer_id=peer_id, role=role,
            ext_id=_norm_id(rec.get(mapping.ext_id)) if mapping.ext_id else "",
            msg_type=_guess_msg_type(text),
            text_kind=kind or "plain",
        ))

    log.debug("读到 %d 条消息（me_ids=%s）", len(res.messages), sorted(me))
    return res


_MEDIA_HINT = re.compile(r"\[(语音|视频|图片|表情|文件|链接|位置|名片|转账|红包|动画表情)\]")


def _guess_msg_type(text: str) -> str:
    """粗分类型。分不出来的一律算 text。

    猜细分类型没有收益：标错类型不会让分析变差，但会让用户以为程序看懂了
    他发的表情包。只有「文本里明确带着 [语音] 这种标记」时才顺手标一下。
    """
    m = _MEDIA_HINT.search(text or "")
    if not m:
        return "text"
    return {"语音": "voice", "视频": "video", "图片": "image",
            "表情": "sticker", "动画表情": "sticker", "文件": "file",
            "链接": "link", "位置": "location", "名片": "card",
            "转账": "transfer", "红包": "redpacket"}.get(m.group(1), "text")


# ================================================================ 探测报告


def probe(db_path: Path, *, me_ids: Sequence[str] = (),
          allow_carve: bool = False) -> dict[str, Any]:
    """给一个解密后的库出一份「我看到了什么」的报告。

    这份报告是给用户看的：他拿到「认不出来」的结论时，需要知道下一步做什么
    （把 `candidates` 里的列名报给我 / 手工指定列）。
    """
    p = Path(db_path)
    if not p.is_file():
        return {"ok": False, "message": f"文件不存在：{p}"}
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "message": f"打不开：{exc}"}
    try:
        tables = list_tables(conn)
        maps, skipped = pick_message_tables(conn, me_ids=me_ids,
                                            allow_carve=allow_carve)
        names = build_name_index(conn, me_ids)
        return {
            "ok": True,
            "file": str(p),
            "size": p.stat().st_size,
            "tables": len(tables),
            "table_names": tables[:60],
            "message_tables": [{
                "table": m.table, "rows": m.rows, "usable": m.usable,
                "ts": m.ts, "ts_unit": m.ts_unit,
                "text": m.text, "text_kind": m.text_kind,
                "sender": m.sender, "peer": m.peer, "ext_id": m.ext_id,
                "me_confirmed": m.me_confirmed,
                "needs_sender": m.needs_sender,
                "evidence": m.evidence,
                "candidates": {k: [{"column": s.column, "score": round(s.score, 3),
                                    "why": s.why} for s in v]
                               for k, v in m.candidates.items()},
                "problems": m.problems,
                "describe": m.describe(),
            } for m in maps],
            "skipped_tables": [{"table": t, "reason": r} for t, r in skipped],
            "known_names": len(names),
        }
    finally:
        conn.close()
