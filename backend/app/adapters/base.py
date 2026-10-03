"""适配器抽象 + 文本解析工具。

导出格式千奇百怪，但九成以上都长这样：

    2024-01-01 12:00:00 昵称
    消息正文（可能多行）

    2024-01-01 12:00:05 我
    消息正文

区别只在两个地方：**时间在昵称前面还是后面**，以及**有没有方括号包裹**。
所以基类把「按头部行切块」这件事抽出来，子类只需要声明 `order` 和
`sniff` 的置信度规则。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..schemas import ParsedMsg

# ---------------------------------------------------------------- 编码

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "big5")


def read_text(path: Path) -> tuple[str, str]:
    """尽力猜编码读取。返回 (文本, 实际使用的编码)。

    中文聊天记录导出十有八九是 GBK 系列，直接 utf-8 读会炸。
    """
    raw = Path(path).read_bytes()
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 解码成功但出现大量替换字符，说明猜错了
        if text.count("\ufffd") <= len(text) * 0.001:
            return text, enc
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


# ---------------------------------------------------------------- 时间戳

_TS = (
    r"(?P<y>\d{4})[-/年](?P<mo>\d{1,2})[-/月](?P<d>\d{1,2})日?"
    r"[\s,，]+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
)

# 时间在前：2024-01-01 12:00:00 昵称 / [2024-01-01 12:00:00] 昵称
RE_TS_FIRST = re.compile(rf"^\s*[\[\(【]?\s*{_TS}\s*[\]\)】]?\s+(?P<sender>\S.{{0,60}}?)\s*$")

# 昵称在前：昵称 2024-01-01 12:00:00
RE_SENDER_FIRST = re.compile(
    rf"^\s*(?P<sender>[^\s\d\[\(【][^\n]{{0,40}}?)\s*[\[\(【]?\s*{_TS}\s*[\]\)】]?\s*$"
)

RE_ANY_TS = re.compile(_TS)


def _mk_dt(m: re.Match[str]) -> datetime | None:
    try:
        y, mo, d = int(m.group("y")), int(m.group("mo")), int(m.group("d"))
        h, mi = int(m.group("h")), int(m.group("mi"))
        s = int(m.group("s") or 0)
    except (TypeError, ValueError):
        return None
    if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h <= 23 and mi <= 59 and s <= 59):
        return None
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 昵称清洗

_RE_ANGLE = re.compile(r"<[^>]{0,120}>")
_RE_QQID = re.compile(r"[\(（]\s*\d{4,}\s*[\)）]\s*$")
_RE_MAILID = re.compile(r"[\(（][^)）]{0,60}@[^)）]{0,60}[\)）]\s*$")
_RE_TAIL = re.compile(r"[\s:：,，]+$")


def clean_sender(raw: str) -> str:
    """把 `昵称(123456)` / `昵称<xx@qq.com>` 归一成 `昵称`。

    注意：输入为空时返回空串，**不要**在这里兜底成「未知用户」——
    调用方常用 `clean_sender(x) or 默认值` 的写法，
    如果这里就返回了非空字符串，那个 `or` 永远不会生效，
    空配置会把真实的昵称覆盖成「未知用户」。
    """
    s = (raw or "").strip()
    s = _RE_ANGLE.sub("", s)
    s = _RE_MAILID.sub("", s)
    s = _RE_QQID.sub("", s)
    s = _RE_TAIL.sub("", s)
    return s.strip()


# ---------------------------------------------------------------- 消息类型

_NOISE_PATTERNS = (
    "撤回了一条消息",
    "以上是打招呼的内容",
    "请使用最新版本手机",
    "你已添加了",
    "你们已成为好友",
    "开启了朋友验证",
    "消息已发出，但被对方拒收",
    "对方已开启好友验证",
    "本条消息已被删除",
    "对方正在输入",
)

_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("image", ("[图片]", "[照片]", "[闪照]", "[image]", "[Photo]", "[相册]")),
    ("voice", ("[语音]", "[voice]", "[音频]", "[Video]")),
    ("video", ("[视频]",)),
    ("sticker", ("[表情]", "[动画表情]", "[贴纸]", "[sticker]")),
    ("file", ("[文件]", "[File]")),
    ("card", ("[名片]", "[链接]", "[分享]", "[音乐]", "[QQ红包]", "[微信红包]", "[转账]")),
    ("location", ("[位置]", "[定位]")),
    ("call", ("[语音通话]", "[视频通话]", "[通话]")),
    ("system", ("[系统消息]", "【系统消息】")),
)


def guess_msg_type(text: str) -> str:
    t = (text or "").strip()
    for kind, markers in _TYPE_RULES:
        for mk in markers:
            if t.startswith(mk) or mk in t[:24]:
                return kind
    return "text"


def is_noise(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    return any(p in t for p in _NOISE_PATTERNS)


# ---------------------------------------------------------------- 切块

_SKIP_LINE = re.compile(r"^\s*(={3,}|-{3,}|_{3,}|\*{3,})\s*$")
_META_LINE = re.compile(
    r"^\s*(消息记录|消息分组|消息对象|日期|导出时间|聊天记录|"
    r"以下为|说明|备注|QQ消息记录|微信消息记录)"
)


def iter_blocks(
    text: str,
    *,
    order: str = "ts_first",
    keep_noise: bool = False,
) -> Iterable[tuple[re.Match[str], list[str]]]:
    """按「头部行 + 后续正文」切块。Yields (头部正则匹配, 正文行列表)。"""
    head_re = RE_TS_FIRST if order == "ts_first" else RE_SENDER_FIRST
    cur: re.Match[str] | None = None
    buf: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        stripped = line.strip()

        if _SKIP_LINE.match(line):
            continue

        m = head_re.match(line)
        if m and _mk_dt(m):
            if cur is not None:
                yield cur, _trim(buf)
            cur, buf = m, []
            continue

        if cur is None:
            # 头部行之前的元信息（消息对象、导出时间等）直接跳过
            continue

        if not stripped and not buf:
            continue

        buf.append(line)

    if cur is not None:
        yield cur, _trim(buf)


def _trim(buf: list[str]) -> list[str]:
    while buf and not buf[0].strip():
        buf.pop(0)
    while buf and not buf[-1].strip():
        buf.pop()
    return buf


def block_to_parsed(
    m: re.Match[str],
    buf: list[str],
    *,
    keep_noise: bool = False,
    max_len: int = 4000,
) -> ParsedMsg | None:
    dt = _mk_dt(m)
    if dt is None:
        return None
    sender = clean_sender(m.group("sender")) or "未知用户"
    text = "\n".join(buf).strip()
    if not text:
        text = ""
    if is_noise(text) and not keep_noise:
        return None
    if len(text) > max_len:
        text = text[:max_len] + "…"
    mtype = guess_msg_type(text)
    if not text:
        text = {"image": "[图片]", "voice": "[语音]", "video": "[视频]",
                "sticker": "[表情]", "file": "[文件]", "call": "[通话]"}.get(mtype, "")
    return ParsedMsg(sender=sender, ts=dt, text=text, msg_type=mtype)


# ---------------------------------------------------------------- 抽象基类


class ChatSourceAdapter(ABC):
    """所有接入插件的基类。新增平台只需实现 sniff 与 parse。"""

    name: str = "base"
    display_name: str = "基础适配器"
    platform: str = "unknown"
    extensions: tuple[str, ...] = ()
    order: str = "ts_first"          # ts_first | sender_first
    default_me_names: tuple[str, ...] = ("我", "me", "Me", "ME", "自己", "本人")

    # ------------------------------------------------------ 探测

    def sniff(self, path: Path, head: str) -> float:
        """返回 0~1 的置信度。registry 用它自动挑适配器。"""
        if self.extensions and path.suffix.lower() in self.extensions:
            return 0.5
        return 0.0

    # ------------------------------------------------------ 解析

    @abstractmethod
    def parse(self, path: Path, options: dict[str, Any] | None = None) -> list[ParsedMsg]:
        ...

    # ------------------------------------------------------ 便捷

    def hint_me_names(self, options: dict[str, Any] | None) -> tuple[str, ...]:
        opts = options or {}
        extra = opts.get("me_names") or []
        if isinstance(extra, str):
            extra = [x.strip() for x in re.split(r"[,，\s]+", extra) if x.strip()]
        return tuple(extra) + self.default_me_names

    def speakers(self, msgs: Iterable[ParsedMsg]) -> list[str]:
        seen: dict[str, int] = {}
        for m in msgs:
            seen[m.sender] = seen.get(m.sender, 0) + 1
        return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]

    def warn_unsupported(self, ext: str) -> str:
        return f"{self.display_name} 不支持 {ext} 扩展名，请检查文件类型或手动指定适配器。"
