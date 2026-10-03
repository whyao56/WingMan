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
from .generic import GenericAdapter
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
    return get_adapter(probes[0].name)


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
) -> ImportResult:
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
    warnings.extend(role_warnings)

    # 优先级：用户显式传入 > 文件元信息 > 自动推断
    peer_name = clean_sender(str(opts.get("peer_name") or "")) or meta.get("peer_name") or peer_name
    me_name = clean_sender(str(opts.get("me_name") or "")) or me_name
    name = (chat_name or "").strip() or peer_name or path.stem or "未命名会话"

    chat_id = make_chat_id(adapter.platform, name)
    store.upsert_chat(chat_id, adapter.platform, name, peer_name, me_name)

    msgs: list[Msg] = []
    for p in parsed:
        sender = p.sender
        role = "me" if (p.role_hint == "me" or sender == me_name) else "peer"
        if sender != me_name and peer_name and sender != peer_name:
            role = "peer"  # 群聊里的第三人，暂按对方处理
        msgs.append(Msg(
            chat_id=chat_id, platform=adapter.platform, sender=sender, role=role,
            ts=p.ts, text=p.text, msg_type=p.msg_type,
        ))

    first, last = 0, 0
    if msgs:
        first, last = 0, 0
    inserted, skipped = store.insert_messages(msgs)
    if not msgs:
        warnings.append("解析结果为空：可能是格式不匹配，或文件被加密/压缩过。")
    if not peer_name:
        warnings.append("未能确定对方昵称，请在后端或前端补充，否则画像会缺少主语。")

    return ImportResult(
        chat_id=chat_id, adapter=adapter.name, parsed=len(parsed),
        inserted=inserted, skipped=skipped,
        speakers=adapter.speakers(parsed), warnings=warnings,
    )


def import_text(
    store: Store,
    text: str,
    *,
    chat_name: str,
    adapter_name: str = "generic",
    options: dict[str, Any] | None = None,
    suffix: str = ".txt",
) -> ImportResult:
    """把用户直接粘进来的文本当记录导入。内部写临时文件复用同一条解析链路。"""
    tmp = Path(tempfile.gettempdir()) / f"wingman_paste_{hashlib.md5(text.encode('utf-8', 'ignore')).hexdigest()[:8]}{suffix}"
    tmp.write_text(text, encoding="utf-8")
    try:
        return import_file(
            store, tmp, chat_name=chat_name,
            adapter_name=adapter_name, options=options,
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
