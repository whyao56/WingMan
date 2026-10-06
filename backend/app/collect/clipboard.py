"""剪贴板读取 + 聊天记录解析 —— 半自动采集的主通道。

## 为什么剪贴板是主通道而不是兜底

实测（2026-10，本机）：

| 客户端 | 窗口框架 | UIA 能读到的节点 | 结论 |
|---|---|---|---|
| 微信 4.1.13.12 | Qt 5.15.14，聊天区是 `MMUIRenderSubWindowHW`（自绘硬件渲染） | 全窗口只有 **2 个**节点 | 读不到任何消息文本 |
| QQ 9.9.20.37051 | Chromium（Electron） | 内容区只有 `Chrome_RenderWidgetHostHWND`，无子节点 | 同样读不到 |

所以「点哪条抓哪条」只能靠**复制**这条用户主动触发的通道：
用户在客户端里选中消息 → 复制 → 本程序读剪贴板。

## 时间怎么办（要诚实）

剪贴板里的时间是**不一定有**的：
- QQ 复制多条时通常带 `昵称 时间` 这样的头行；
- 微信复制时一般只有正文。

所以解析器把「时间」当成可选，并把**每条消息的时间来源标出来**
（`clipboard` / `assumed` / 空），界面上一眼能看出哪些时间是推断的。
不猜一个看起来合理的时间然后当成真数据 —— 那会让分析结果建立在假时间轴上。

## 说话人怎么定（不猜）

不靠「行首是不是像名字」这种猜测，而是**拿会话里已知的两个说话人名字去匹配**：
会话记录里有 `me_name`（我）和 `peer_name`（对方），
复制出来的文本如果以其中之一开头，就确定是那个人说的；
都匹配不上时，**交由用户指定**，不自动乱认。

三种块头写法都要认（都是实测会出现的）：

| 写法 | 例子 | 出现在哪 |
|---|---|---|
| 称呼 + 冒号 + 正文 | `小鹿: 在吗` | 微信 / 手工整理的文本 |
| 称呼 + 空格 + 时间戳 | `小鹿 2026-09-28 21:03:15`（正文在下一行） | **QQ NT 多选复制的主形态** |
| 称呼 + 空格 + 时间戳 + 正文 | `小鹿 2026-09-28 21:03:15 在吗` | QQ 部分版本 |

第二种的判据是「剥掉称呼后，剩下的东西必须**只有**一个时间戳」——
这样 `我觉着吧：这事儿得再想想` 不会被当成 `我` 说的：
剥掉「我」之后剩的是「觉着吧：…」，里面既没有时间戳、剩下的也不是空的。
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .winapi import wintypes

# 注意：这里**不能**直接写 `from ctypes import wintypes`。
# `ctypes.wintypes` 里的 `VARIANT_BOOL` 用了 `'v'` 类型码，非 Windows 上导不进来
# （`ValueError: _type_ 'v' not supported`），而这是模块级语句 —— 会让「只是 import
# 一下」都失败，CI（Ubuntu）上整个测试矩阵会红，本地 Windows 却全绿。
# `winapi` 里已经把这件事处理好了（真身优先、非 Windows 才用替身），
# 所以这里统一从它取，**只留一份实现**，别再各自写一遍。

IS_WINDOWS = sys.platform == "win32"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _u32():
    if not IS_WINDOWS:
        raise OSError("剪贴板读取只在 Windows 上可用")
    dll = ctypes.WinDLL("user32", use_last_error=True)
    # 句柄在 64 位下是 8 字节，是**指针宽度**。不声明 argtypes/restype 时
    # ctypes 会按 C int（4 字节）处理，句柄一旦超过 2^31 就抛
    # 「int too long to convert」—— 而句柄是高地址是很正常的。
    dll.OpenClipboard.argtypes = [wintypes.HWND]
    dll.OpenClipboard.restype = wintypes.BOOL
    dll.CloseClipboard.restype = wintypes.BOOL
    dll.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    dll.IsClipboardFormatAvailable.restype = wintypes.BOOL
    dll.GetClipboardData.argtypes = [wintypes.UINT]
    dll.GetClipboardData.restype = wintypes.HANDLE
    dll.GetClipboardSequenceNumber.restype = wintypes.DWORD
    dll.EmptyClipboard.restype = wintypes.BOOL
    dll.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    dll.SetClipboardData.restype = wintypes.HANDLE
    return dll


def _k32():
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GlobalLock.argtypes = [wintypes.HGLOBAL]
    dll.GlobalLock.restype = ctypes.c_void_p
    dll.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    dll.GlobalUnlock.restype = wintypes.BOOL
    dll.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    dll.GlobalAlloc.restype = wintypes.HGLOBAL
    dll.GlobalFree.argtypes = [wintypes.HGLOBAL]
    dll.GlobalSize.argtypes = [wintypes.HGLOBAL]
    dll.GlobalSize.restype = ctypes.c_size_t
    return dll


def sequence_number() -> int:
    """剪贴板序号：每次内容变化都会 +1。

    用这个而不是「比对内容」来判断有没有新东西：内容是会被重复复制的
    （用户可能故意再复制一次同一条），而序号只在真的发生变化时才动。
    """
    if not IS_WINDOWS:
        return 0
    try:
        return int(_u32().GetClipboardSequenceNumber())
    except OSError:
        return 0


def read_text(retries: int = 4) -> str:
    """读剪贴板里的文本。剪贴板可能被别人占着，所以重试几次再放弃。"""
    if not IS_WINDOWS:
        return ""
    u32, k32 = _u32(), _k32()
    for attempt in range(retries):
        if not u32.OpenClipboard(None):
            time.sleep(0.05 * (attempt + 1))
            continue
        try:
            if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = u32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k32.GlobalUnlock(handle)
        finally:
            u32.CloseClipboard()
    return ""


def write_text(text: str, retries: int = 4) -> bool:
    """写剪贴板。

    生产代码不需要它（我们只读）—— 它的存在是为了让解析器**可以被测**：
    合成几段「像从微信复制出来的」文本，验证解析对不对。
    没有这个能力就只能靠真人在客户端里手工复制来测，那不叫测试。
    """
    if not IS_WINDOWS:
        return False
    u32, k32 = _u32(), _k32()
    data = (text or "") + "\x00"
    size = len(data) * ctypes.sizeof(ctypes.c_wchar)
    for attempt in range(retries):
        if not u32.OpenClipboard(None):
            time.sleep(0.05 * (attempt + 1))
            continue
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return False
            ptr = k32.GlobalLock(h)
            if not ptr:
                k32.GlobalFree(h)
                return False
            try:
                ctypes.memmove(ptr, data, size)
            finally:
                k32.GlobalUnlock(h)
            if not u32.SetClipboardData(CF_UNICODETEXT, h):
                k32.GlobalFree(h)
                return False
            return True
        finally:
            u32.CloseClipboard()
    return False


# ---------------------------------------------------------------- 解析

# 时间戳：2024-01-01 12:00:00 / 2024/01/01 12:00 / 01-01 12:00
_TS_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/年](?P<mo>\d{1,2})[-/月](?P<d>\d{1,2})日?"
               r"[\s,，]+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"),
    re.compile(r"(?P<mo>\d{1,2})[-/月](?P<d>\d{1,2})日?"
               r"\s+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"),
)

# ---------------------------------------------------------------- 相对时间
#
# 客户端复制出来的时间**大量是相对的**：「小鹿 21:03」「小鹿 昨天 21:03」
# 「小鹿 下午 3:30」。这些恰恰是消息本身显示的时刻 —— 也就是用户要的
# 「回复时刻」。只认绝对日期的话，这些一律落进「剪贴板没带时间」，
# 最后被顶替成「你按下 Ctrl+C 的那一刻」，那是**采集时刻**，不是消息时刻，
# 两者可以差好几天（补录旧消息时差得更远）。
#
# 所以这里补一层相对时间解析，把「今天/昨天/前天/周几 + 时段 + 钟点」
# 解析到具体日期，并**标记成 `relative`** —— 钟点是消息给的，日期是推出来的，
# 界面要能区分这两件事。
_REL_DAY = {"今天": 0, "今晚": 0, "昨天": -1, "昨晚": -1, "前天": -2, "前晚": -2}
_REL_PM = {"下午", "傍晚", "晚上", "夜里", "深夜"}
_REL_AM = {"凌晨", "早上", "早晨", "上午"}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_REL_TS = re.compile(
    r"(?:(?P<wk>周|星期|礼拜)(?P<wd>[一二三四五六日天])\s*)?"
    r"(?:(?P<day>今天|今晚|昨天|昨晚|前天|前晚)\s*)?"
    r"(?:(?P<part>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|深夜)\s*)?"
    r"(?P<h>\d{1,2})[:：](?P<mi>\d{2})(?:[:：](?P<s>\d{2}))?"
)


def _parse_relative_ts(
    text: str, *, now: datetime | None = None, allow_bare_clock: bool = False
) -> tuple[str, int, int]:
    """解析相对时间，返回 `(ISO, 起点, 终点)`；找不到返回 `("", -1, -1)`。

    `allow_bare_clock` 控制**裸钟点**（只有一个 `21:03`，前面既没有「昨天」
    也没有「下午」）要不要认。这是一种弱信号：`我们 3:1 赢了` 里的 `3:1`
    不是时间。所以只在「钟点紧跟在称呼后面」这种位置才允许（由调用方判断）。
    """
    now = now or datetime.now()
    for m in _REL_TS.finditer(text):
        g = m.groupdict()
        wk, day, part = g.get("wk"), g.get("day"), g.get("part")
        if not (wk or day or part) and not allow_bare_clock:
            continue
        try:
            h, mi = int(g["h"]), int(g["mi"])
            s = int(g.get("s") or 0)
        except (TypeError, ValueError):
            continue
        if h > 23 or mi > 59 or s > 59:
            continue
        if part in _REL_PM and h < 12:
            h += 12
        elif part in _REL_AM and h == 12:
            h = 0
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if wk:
            # 「周三」指最近一个已经过去的周三；今天正是周三时取上周三
            delta = (midnight.weekday() - _WEEKDAY.get(g["wd"] or "一", 0)) % 7 or 7
            midnight -= timedelta(days=delta)
        elif day:
            midnight += timedelta(days=_REL_DAY.get(day, 0))
        else:
            cand = midnight.replace(hour=h, minute=mi, second=s)
            # 没说哪一天：当成今天；但如果算出来还在未来（比如刚过午夜复制了
            # 一条 23:50 的消息），那就是昨天 —— 消息不可能来自未来。
            if cand > now + timedelta(minutes=5):
                midnight -= timedelta(days=1)
        stamp = midnight + timedelta(hours=h, minutes=mi, seconds=s)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S"), m.start(), m.end()
    return "", -1, -1


# 说话人与正文之间的分隔：`昵称: 内容` / `昵称：内容`
_SENDER_SEP = re.compile(r"^\s*(?P<who>[^:：\n]{1,24})\s*[:：]\s*(?P<body>.*)$", re.S)

# 称呼后面、时间戳之前可能夹一个附注（QQ 会带 QQ 号或备注）
_HEAD_TAG = re.compile(r"^\s*[（(\[【][^）)\]】]{0,32}[）)\]】]")

# 称呼与时间戳之间允许出现的分隔符
_HEAD_SEPS = " \t:：,，-—|/、"


@dataclass
class Captured:
    """从剪贴板解出来的一条消息。"""

    sender: str = ""
    text: str = ""
    ts: str = ""                  # 空表示剪贴板里没带时间
    role: str = ""                # me | peer | ""（未定）
    ts_source: str = ""           # clipboard | assumed | ""
    raw_line: str = ""

    @property
    def has_sender(self) -> bool:
        return bool(self.sender)

    @property
    def needs_sender(self) -> bool:
        """这条还没确定是谁说的。

        界面必须为此问用户。**不猜** —— 把「老王」当成「小鹿」写进记录，
        错是静默的，之后的分析全建立在错误的说话人上。
        """
        return not self.role


@dataclass
class ParseResult:
    items: list[Captured] = field(default_factory=list)
    shape: str = "plain"          # structured | plain | empty
    note: str = ""
    needs_sender: bool = False    # 解析不出说话人，要用户指定


def _parse_ts(
    text: str, *, allow_bare_clock: bool = False, now: datetime | None = None
) -> tuple[str, str, int, int, str]:
    """在一行里找时间。

    返回 `(ISO 时间, 剥掉时间后的剩余文本, 时间起点, 时间终点, 来源)`；
    找不到时返回 `("", 原文, -1, -1, "")`。

    **来源（`ts_source`）区分了两件不该混为一谈的事**：

    - `clipboard` —— 客户端原样给了年月日时分，这是消息的确切时刻；
    - `relative` —— 客户端只给了「昨天 21:03」这类相对写法；钟点是消息给的，
      日期由解析时推出来。它仍然是**消息自身的时刻**，但日期可能差一天，
      界面要标出来让人能核对。

    起止下标是给调用方判断**时间戳处在什么位置**用的：
    - 块头要求时间紧跟在称呼后面（否则 `我 今天 2026-… 说过` 会被当成头行）；
    - 单条形态要求时间在整段的头或尾（否则会把正文中间的日期偷走）。
    """
    for pat in _TS_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groupdict()
        try:
            ref = now or datetime.now()
            y = int(g.get("y") or ref.year)
            mo, d = int(g["mo"]), int(g["d"])
            h, mi = int(g["h"]), int(g["mi"])
            s = int(g.get("s") or 0)
        except (TypeError, ValueError):
            continue
        if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31
                and h <= 23 and mi <= 59 and s <= 59):
            continue
        iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"
        rest = (text[:m.start()] + " " + text[m.end():]).strip()
        return iso, rest, m.start(), m.end(), "clipboard"

    iso, start, end = _parse_relative_ts(
        text, now=now, allow_bare_clock=allow_bare_clock)
    if iso:
        rest = (text[:start] + " " + text[end:]).strip()
        return iso, rest, start, end, "relative"

    return "", text, -1, -1, ""


def _match_known(who: str, me_names: list[str], peer_names: list[str]) -> str:
    """把行首那个称呼对到『我』或『对方』。只做精确匹配（忽略大小写与空白）。"""
    key = who.strip().lower()
    if not key:
        return ""
    if any(key == n.strip().lower() for n in me_names if n):
        return "me"
    if any(key == n.strip().lower() for n in peer_names if n):
        return "peer"
    return ""


def _known_pairs(me_names: list[str], peer_names: list[str]) -> list[tuple[str, str]]:
    """已知说话人 → [(名字, role)]，**长名字排前面**。

    排序不是装饰：同一个人可能同时以「小鹿」和「小鹿儿」被记录，
    短名字先试会把长名字的前缀吃掉，于是「小鹿儿: 在吗」被认成「小鹿」说的。
    """
    pairs = [(n.strip(), "me") for n in me_names if n and n.strip()]
    pairs += [(n.strip(), "peer") for n in peer_names if n and n.strip()]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _head_of(
    line: str, pairs: list[tuple[str, str]], now: datetime | None = None
) -> tuple[str, str, str, str, str] | None:
    """判断一行是不是「块头」。返回 (称呼, role, 时间, 同行正文, 时间来源) 或 None。"""
    m = _SENDER_SEP.match(line)
    if m:
        who = m.group("who").strip()
        for name, role in pairs:
            if who.lower() == name.lower():
                ts, body, _, _, src = _parse_ts(m.group("body"),
                                                allow_bare_clock=True, now=now)
                return name, role, ts, body, src

    low = line.lower()
    for name, role in pairs:
        if not low.startswith(name.lower()):
            continue
        rest = line[len(name):]
        rest = _HEAD_TAG.sub("", rest, count=1)
        rest = rest.lstrip(_HEAD_SEPS)
        if not rest:
            # 只有称呼的孤行：正文在下面几行（少见但合法）
            return name, role, "", "", ""
        ts, body, at, _, src = _parse_ts(rest, allow_bare_clock=True, now=now)
        # 关键判据：时间戳必须紧跟在称呼后面。
        # `我觉着吧：这事儿得再想想` → rest="觉着吧：…"，没有时间戳 → 不是块头；
        # `我 今天 2026-09-28 21:03:15 说过` → 时间戳前面还压着「今天」→ 也不是块头。
        if ts and not rest[:at].strip(_HEAD_SEPS).strip():
            return name, role, ts, body, src
    return None


def parse_copied(
    text: str,
    *,
    me_names: list[str] | None = None,
    peer_names: list[str] | None = None,
    now: datetime | None = None,
) -> ParseResult:
    """把剪贴板文本解析成消息列表。

    识别两种形态：
    - **块状**：每段由「称呼 [+ 时间]」起头，后面是正文。
      这是 QQ / 微信「多选后复制」的常见产物。
    - **单条**：只有正文，没有称呼也没有时间。此时不假装知道是谁说的，
      `needs_sender=True`，让界面去问用户。
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ParseResult(shape="empty", note="剪贴板里没有文本。")

    me_names = [n for n in (me_names or []) if n]
    peer_names = [n for n in (peer_names or []) if n]
    pairs = _known_pairs(me_names, peer_names)

    lines = raw.split("\n")
    # (行号, 称呼, role, ts, 同行正文, ts_source)
    heads: list[tuple[int, str, str, str, str, str]] = []
    for i, line in enumerate(lines):
        h = _head_of(line, pairs, now)
        if h:
            heads.append((i, *h))

    if heads:
        items: list[Captured] = []

        # 第一个块头之前若还有内容，单独留一条并标成「待指定」。
        # 默默丢掉用户复制进来的文字，是这类工具里最不该犯的错。
        lead = "\n".join(lines[:heads[0][0]]).strip()
        if lead:
            lts, lbody, _, _, lsrc = _parse_ts(lead, now=now)
            items.append(Captured(text=lbody.strip() or lead, ts=lts,
                                  ts_source=lsrc,
                                  raw_line=lines[0]))

        for idx, (ln, who, role, ts, body_first, src) in enumerate(heads):
            end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
            block = [body_first] + lines[ln + 1:end]
            items.append(Captured(sender=who, text="\n".join(block).strip(),
                                  ts=ts, role=role,
                                  ts_source=src,
                                  raw_line=lines[ln]))

        items = [it for it in items if it.text.strip()]
        if items:
            missing = sum(1 for it in items if it.needs_sender)
            note = f"识别出 {len(items)} 条消息"
            note += (f"，其中 {missing} 条没说清是谁说的。" if missing
                     else "，说话人已对上。")
            return ParseResult(items=items, shape="structured", note=note,
                               needs_sender=missing > 0)
        return ParseResult(shape="empty",
                           note="剪贴板里只有说话人和时间，没有正文。")

    # 单条形态：整段当一条，时间从文本里找（找到就剥掉）
    ts, stripped, at, end, src = _parse_ts(raw, now=now)
    if ts and raw[:at].strip() and raw[end:].strip():
        # 时间戳夹在一句话中间 —— 它是正文的一部分，不是这条消息的时间。
        # 把它抽走会把正文切出一道口子，还谎报这是消息时间。
        ts, stripped, src = "", raw, ""
    body = stripped.strip() or raw.strip()
    return ParseResult(
        items=[Captured(text=body, ts=ts, ts_source=src)],
        shape="plain",
        note="剪贴板里没有说话人信息，请指定这条是谁说的。",
        needs_sender=True,
    )


# ---------------------------------------------------------------- 监听


@dataclass
class Capture:
    """一次剪贴板变化事件。"""

    at: float
    items: list[Captured]
    shape: str
    note: str
    needs_sender: bool


class ClipboardWatcher:
    """轮询剪贴板，内容变了就产出一条 `Capture`。

    为什么轮询而不是 `AddClipboardFormatListener`：后者要求有一个窗口和
    消息循环，而我们是在一个 Web 服务里跑，为了收一条通知去开一个隐藏窗口
    并跑消息泵，代价和复杂度都不划算。剪贴板序号这个 API 让轮询变得很便宜 ——
    只需要读取一个整数做比较，真正读文本只在序号变化时发生。
    """

    def __init__(self, interval: float = 0.5, min_length: int = 1):
        self.interval = interval
        self.min_length = min_length
        self._last_seq = sequence_number()

    def poll(self, *, me_names=None, peer_names=None) -> Capture | None:
        seq = sequence_number()
        if seq == self._last_seq:
            return None
        self._last_seq = seq
        text = read_text()
        if len((text or "").strip()) < self.min_length:
            return None
        parsed = parse_copied(text, me_names=me_names, peer_names=peer_names)
        if parsed.shape == "empty":
            return None
        return Capture(at=time.time(), items=parsed.items, shape=parsed.shape,
                       note=parsed.note, needs_sender=parsed.needs_sender)

    def reset(self) -> None:
        self._last_seq = sequence_number()
