"""适配器注册表：自动嗅探、预览、落库。

对外只暴露四个函数：
- `detect(path)`        → 各适配器的置信度排序，用于自动选型 + 前端二次确认
- `preview_file(path)`  → 只解析不落库，返回前 30 条和说话人列表
- `import_file(...)`    → 解析 + 去重落库
- `import_text(...)`    → 直接把用户粘进来的文本当记录导入
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

from ..schemas import AdapterProbe, ImportPreview, ImportResult, Msg, ParsedMsg
from ..store import Store
from .base import ENCODINGS, ChatSourceAdapter, clean_sender
from .generic import DEFAULT_MAP, GenericAdapter
from .qq import QQAdapter
from .wechat import WeChatAdapter

ADAPTERS: list[ChatSourceAdapter] = [QQAdapter(), WeChatAdapter(), GenericAdapter()]
_BY_NAME: dict[str, ChatSourceAdapter] = {a.name: a for a in ADAPTERS}

DEFAULT_ME_NAMES: tuple[str, ...] = ("我", "自己", "本人", "me", "Me", "ME", "myself")


def get_adapter(name: str) -> ChatSourceAdapter:
    return _BY_NAME.get(name) or GenericAdapter()


def read_head(path: Path, nbytes: int = 65536) -> str:
    """只读文件头部，供嗅探使用，避免为了判定类型把几百 MB 全读进来。"""
    try:
        raw = Path(path).read_bytes()[:nbytes]
    except OSError:
        return ""
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 探测


def detect(path: Path, forced: str | None = None) -> list[AdapterProbe]:
    path = Path(path)
    head = read_head(path)
    probes: list[AdapterProbe] = []
    for a in ADAPTERS:
        try:
            score = float(a.sniff(path, head))
        except Exception as exc:  # 嗅探失败不应影响其它适配器
            score = 0.0
            _ = exc
        note = ""
        if a.extensions and path.suffix.lower() not in a.extensions:
            note = f"扩展名 {path.suffix or '无'} 不在其支持范围"
        probes.append(AdapterProbe(
            name=a.name, display_name=a.display_name,
            confidence=round(max(0.0, min(1.0, score)), 3), note=note,
        ))
    probes.sort(key=lambda p: -p.confidence)
    if forced and forced in _BY_NAME:
        chosen = _BY_NAME[forced]
        probes = [p for p in probes if p.name != forced]
        probes.insert(0, AdapterProbe(
            name=chosen.name, display_name=chosen.display_name,
            confidence=1.0, note="用户手动指定",
        ))
    return probes


def _pick(probes: list[AdapterProbe]) -> ChatSourceAdapter:
    if not probes:
        return GenericAdapter()
    best = probes[0]
    # 一个都不像（最高分 0）时用 `generic` 兜底，而不是听凭注册顺序落到 QQ 上。
    # 落到 QQ 的后果不只是「猜错」，而是**提示词也错**：用户会看到
    # 「「QQ 聊天记录」读不了这份内容」—— 可问题根本不在于它是 QQ，
    # 而 generic 那句会告诉他能改走剪贴板。选错适配器会连带着说错话。
    if best.confidence <= 0:
        return GenericAdapter()
    return get_adapter(best.name)


def _guess_paste_suffix(text: str) -> str:
    """从粘贴内容猜一个后缀，让适配器能进对分支。

    粘贴过来的东西**没有文件名**，而 `generic` 适配器是靠扩展名分支的
    （只有 `.json` / `.csv` / `.tsv` 才走结构化解析）。一律按 `.txt` 落盘的话，
    用户粘一段 JSON 或一张表进来会一条都读不出来 —— 数据他明明给全了。

    判断刻意保守，拿不准就退回 `.txt`（交给行式解析器）：

    - 首个非空字符是 `[` / `{` → `.json`
    - 首行能切出 ≥2 列，且**表头里出现了已知列名**（时间 / 发送者 / 内容…），
      或者**每一行的列数都一样**（表格的形状，散文几乎不可能满足）→ `.csv` / `.tsv`
    - 其余 → `.txt`
    """
    head = text.lstrip("\ufeff \t\r\n")
    if not head:
        return ".txt"
    if head[0] in ("[", "{"):
        return ".json"

    lines = [ln for ln in head.splitlines() if ln.strip()][:20]
    first = lines[0]
    delim = "\t" if "\t" in first else ("," if "," in first else "")
    if not delim:
        return ".txt"

    def cells(line: str) -> list[str]:
        return [c.strip().lower() for c in line.split(delim)]

    header = cells(first)
    if len(header) < 2:
        return ".txt"
    known = {c.lower() for column_names in DEFAULT_MAP.values() for c in column_names}
    tabular = bool(set(header) & known) or all(
        len(cells(ln)) == len(header) for ln in lines
    )
    if not tabular:
        return ".txt"
    return ".tsv" if delim == "\t" else ".csv"


# ---------------------------------------------------------------- 角色判定


def _merge_me_names(options: dict[str, Any] | None) -> tuple[str, ...]:
    opts = dict(options or {})
    extra = opts.get("me_names") or []
    if isinstance(extra, str):
        extra = [x.strip() for x in re.split(r"[,，、\s]+", extra) if x.strip()]
    extra = [clean_sender(x) for x in extra if x]
    return tuple(extra) + DEFAULT_ME_NAMES


def _resolve_roles(
    msgs: list[ParsedMsg], options: dict[str, Any] | None
) -> tuple[str, str, list[str]]:
    """返回 (me_name, peer_name, warnings)。"""
    warnings: list[str] = []
    me_names = _merge_me_names(options)
    counts: dict[str, int] = {}
    for m in msgs:
        counts[m.sender] = counts.get(m.sender, 0) + 1

    if not counts:
        return "", "", ["没有解析出任何消息，请检查文件格式或手动指定适配器。"]

    matched = [s for s in counts if s in me_names]
    # 单个说话人的自述型记录（比如只有自己一边的备忘）
    if len(counts) == 1:
        only = next(iter(counts))
        if only in me_names:
            warnings.append("仅识别到一个说话人（你自己），这条记录里没有对方的发言。")
            return only, "", warnings
        warnings.append(f"仅识别到一个说话人「{only}」，已默认其为对方。")
        return "我", only, warnings

    if matched:
        me_name = max(matched, key=len)  # 取更长者，避免"我"和"我(手机)"共存时选错
    else:
        # 没有任何说话人叫「我」这类名字 —— 需要猜
        opts = dict(options or {})
        explicit_peer = clean_sender(str(opts.get("peer_name") or ""))
        if explicit_peer and explicit_peer in counts:
            peer_name = explicit_peer
            others = [s for s in counts if s != peer_name]
            me_name = max(others, key=lambda s: counts[s]) if others else "我"
        else:
            # 默认把消息条数更多的当自己 —— 概率上聊天记录里主动方发言常略多
            ordered = sorted(counts, key=lambda s: -counts[s])
            me_name = ordered[0]
            warnings.append(
                f"没能识别出「我」这个称呼，已默认「{me_name}」是你自己。"
                f"如果不对，请在导入时手动指定你的昵称。"
            )

    peers = [s for s in counts if s != me_name]
    peer_name = max(peers, key=lambda s: counts[s]) if peers else ""
    if len(peers) > 1:
        warnings.append(
            f"检测到 {len(peers)} 个其他说话人，已把消息最多的「{peer_name}」当成主要对象，"
            f"其余 ({', '.join(p for p in peers if p != peer_name)}) 仍会入库但标记为对方。"
        )
    return me_name, peer_name, warnings


# ---------------------------------------------------------------- chat_id


def make_chat_id(platform: str, name: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", name).strip("-")[:32]
    if not slug:
        slug = "chat"
    h = hashlib.md5(f"{platform}:{name}".encode("utf-8")).hexdigest()[:6]
    return f"{platform}-{slug}-{h}"


# ---------------------------------------------------------------- 预览


def preview_file(
    path: Path,
    *,
    adapter_name: str | None = None,
    options: dict[str, Any] | None = None,
    sample_size: int = 30,
) -> ImportPreview:
    probes = detect(Path(path), forced=adapter_name)
    adapter = _pick(probes)
    warnings: list[str] = []
    try:
        parsed = adapter.parse(Path(path), options)
    except Exception as exc:
        parsed = []
        warnings.append(f"{adapter.display_name} 解析失败：{exc}")

    # meta（仅 QQ / 微信支持）可能给出更准的对方昵称
    meta: dict[str, Any] = {}
    if hasattr(adapter, "meta"):
        try:
            meta = adapter.meta(Path(path)) or {}
        except Exception:
            meta = {}

    speakers = adapter.speakers(parsed)
    me_name, peer_name, role_warnings = _resolve_roles(parsed, options)
    warnings.extend(role_warnings)
    for p in probes[:3]:
        if p.confidence < 0.3:
            warnings.append(f"「{p.display_name}」匹配度较低（{p.confidence}），解析结果可能不准。")
            break

    sample: list[ParsedMsg] = []
    for p in parsed[:sample_size]:
        role = "me" if p.sender == me_name else "peer"
        sample.append(p.model_copy(update={"role_hint": role}))

    return ImportPreview(
        adapter=adapter.name,
        probes=probes,
        total_parsed=len(parsed),
        speakers=speakers,
        sample=sample,
        warnings=warnings + ([f"元信息：{meta}"] if meta else []),
    )


# ---------------------------------------------------------------- 导入


def import_file(
    store: Store,
    path: Path,
    *,
    chat_name: str | None = None,
    adapter_name: str | None = None,
    options: dict[str, Any] | None = None,
    platform: str | None = None,
) -> ImportResult:
    """把一份文件导入成一段渠道记录。

    `platform` 是**渠道来源**的显式声明（qq / wechat / other / call / offline），
    不传就跟着适配器走。

    为什么需要它：适配器认的是**文件长什么样**，用户知道的是**话是在哪说的**。
    这两件事不等价 —— 一份 Telegram 导出的文本很可能被文本排布识别成「微信」，
    于是明明是「其他聊天」的内容被贴上微信的标签。机器猜不出来的事就别猜，
    让用户在一开始说一句，比事后在渠道设置里返工便宜得多。
    """
    path = Path(path)
    probes = detect(path, forced=adapter_name)
    adapter = _pick(probes)
    opts = dict(options or {})

    meta: dict[str, Any] = {}
    if hasattr(adapter, "meta"):
        try:
            meta = adapter.meta(path) or {}
        except Exception:
            meta = {}

    parsed = adapter.parse(path, opts)
    warnings: list[str] = []
    me_name, peer_name, role_warnings = _resolve_roles(parsed, opts)
    # 一条都没解析出来时，`_resolve_roles` 也会报一句「没解析出任何消息」——
    # 但那是它视角下的话（不知道用的是哪个适配器）。下面 pending 的那句会点名
    # 「这个适配器吃什么、另一条路怎么走」，两句一起出现只会互相稀释。
    if parsed:
        warnings.extend(role_warnings)

    # 优先级：用户显式传入 > 文件元信息 > 自动推断
    peer_name = clean_sender(str(opts.get("peer_name") or "")) or meta.get("peer_name") or peer_name
    me_name = clean_sender(str(opts.get("me_name") or "")) or me_name
    name = (chat_name or "").strip() or peer_name or path.stem or "未命名会话"

    plat = (str(platform or "").strip() or adapter.platform) or "other"
    chat_id = make_chat_id(plat, name)
    store.upsert_chat(chat_id, plat, name, peer_name, me_name)

    msgs: list[Msg] = []
    for p in parsed:
        sender = p.sender
        role = "me" if (p.role_hint == "me" or sender == me_name) else "peer"
        if sender != me_name and peer_name and sender != peer_name:
            role = "peer"  # 群聊里的第三人，暂按对方处理
        msgs.append(Msg(
            chat_id=chat_id, platform=plat, sender=sender, role=role,
            ts=p.ts, text=p.text, msg_type=p.msg_type,
        ))

    first, last = 0, 0
    if msgs:
        first, last = 0, 0
    inserted, skipped = store.insert_messages(msgs)
    if not msgs:
        # 「解析结果为空」是用户最容易停在这里的地方：他明明把内容贴进来了，
        # 界面却说一条都没读到，而旧文案只说「格式不匹配」—— 等于让他自己猜。
        # 所以这里点名**这个适配器到底吃什么**，并给出另一条确定能走通的路。
        warnings.append(
            f"没解析出消息：「{adapter.display_name}」读不了这份内容。"
            "它认的是结构化格式（要有时间、发送者、内容三列 / 三个字段）；"
            "要是手里只有整段文字，改走「采集 → 半自动（剪贴板）」："
            "在客户端里选中聊天内容按 Ctrl+C，它接到什么就存什么。"
        )
    if not peer_name:
        warnings.append("未能确定对方昵称，可以在对象或渠道设置里补上，否则画像会缺少主语。")

    # 渠道名从库里读回来，而不是在这里再算一遍 —— 映射规则只有 store 那一份，
    # 复制一份出来就等着两边慢慢分叉（`generic → other` 就是这么分叉过的一次）。
    info = store.get_chat(chat_id)
    return ImportResult(
        chat_id=chat_id, adapter=adapter.name, parsed=len(parsed),
        inserted=inserted, skipped=skipped,
        speakers=adapter.speakers(parsed), warnings=warnings,
        platform=plat, channel=(info.channel if info else ""),
    )


def import_text(
    store: Store,
    text: str,
    *,
    chat_name: str,
    adapter_name: str = "auto",
    options: dict[str, Any] | None = None,
    platform: str | None = None,
    suffix: str | None = None,
) -> ImportResult:
    """把用户直接粘进来的文本当记录导入。内部写临时文件复用同一条解析链路。

    默认适配器是 `auto`（交给嗅探决定），**不是 `generic`**。这里踩过一个坑：
    粘贴框旁边写着的示范格式是「`2024-01-01 12:00:00 昵称` + 内容」，
    而 `generic` 只认 JSON / CSV —— 照着自己界面上的说明粘贴，结果一条都读不出来。
    界面上写的格式必须真的能用，所以默认改成让嗅探去认。

    后缀也从内容推（见 `_guess_paste_suffix`）：`generic` 是靠扩展名分支的
    （`.json` 才会走 JSON 解析），一律写成 `.txt` 的话，用户粘一段 JSON
    或一张 CSV 进来同样读不出来。
    """
    if suffix is None:
        suffix = _guess_paste_suffix(text)
    tmp = Path(tempfile.gettempdir()) / f"wingman_paste_{hashlib.md5(text.encode('utf-8', 'ignore')).hexdigest()[:8]}{suffix}"
    tmp.write_text(text, encoding="utf-8")
    try:
        return import_file(
            store, tmp, chat_name=chat_name,
            adapter_name=adapter_name, options=options, platform=platform,
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
