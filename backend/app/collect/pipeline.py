"""自动采集的流程编排：从「客户端装着」走到「消息进了库」。

## 一次采集要过六道关，每道都可能合理地失败

```
① 探测      装了吗 / 开着吗 / 版本在支持范围内吗 / 数据目录在哪
② 取密钥    缓存 → 用户粘贴 → 自动搜内存（有预算、有结论）
③ 解密      剥自定义头、逐页解密、套用 WAL（不套会漏掉最近的消息）
④ 认列      这个库里哪一列是时间、哪一列是正文、哪一列是说话人
⑤ 读消息    按游标取增量，判「这是我说的还是对方说的」
⑥ 入库      会话归属到人、消息去重写入、记下游标
```

**每一道失败都返回「为什么 + 下一步做什么」，而不是抛异常或返回空**。
这条链路上最贵的事不是慢，是「看起来成功了」：
用户以为采到了，其实库里一条都没有；或者更糟，采进了一堆错归属的消息。
所以 `CollectReport` 里的每个字段都带人的解释。

## 为什么解密结果要缓存

解密一个几十上百 MB 的库要逐页做 HMAC 校验，几百毫秒到几秒。
而消息库在「客户端没写新消息时」是不变的。所以按
`(主库大小, mtime, WAL 大小, WAL mtime, salt)` 做指纹，
指纹没变就直接用上次解密好的副本 —— 第二次点「采集」是秒回的。

指纹里带 WAL 是有用的：客户端在跑的时候，最新的消息**只在 WAL 里**，
只比主库的 mtime 会漏掉「刚聊完就来采」这种情况。

## 为什么认列结果也要缓存

`reader` 的认列是采样推理，同一次采集里对每个会话分片重复做一遍纯属浪费；
而且用户可能手工改过列映射（`schema_overrides`），那个改动必须被记住 ——
否则每次采集都要重新指定一遍列，等于没做这个功能。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import DATA_DIR
from ..schemas import Msg
from . import detect as detect_mod
from . import reader
from .detect import Detected
from .keys import KeyAttempt, obtain_key
from .matrix import SUPPORT_MATRIX
from .sqlcipher import decrypt_database, salt_of

log = logging.getLogger("wingman.collect.pipeline")

CACHE_DIR = DATA_DIR / "collect_cache"
META_FILE = CACHE_DIR / "index.json"

# 一次采集单库的上限。太大了会把内存和界面卡住，而且用户第一次跑
# 通常只是想看看能不能通，不是真想一次吞十万条。
DEFAULT_MAX_MESSAGES = 20000

# 「在客户端进程内存里搜密钥」的默认时间预算。它是个上限不是目标：
# 命中就立刻返回。实测本机这两个版本的密钥不在常见形态里，
# 所以这个预算基本会被跑满（见 `keys.py` 里的实测记录）。
DEFAULT_MEMORY_BUDGET_S = 60.0


# ================================================================ 入参 / 出参


@dataclass
class CollectRequest:
    client: str
    account: str = ""                       # 多账号时指定；空表示全都采
    # db 路径（或文件名）→ 用户粘贴的密钥。空则用 pasted_key
    keys: dict[str, str] = field(default_factory=dict)
    pasted_key: str = ""
    # 用户手工指定的列映射：db 名 → {ts, text, sender, peer, ext_id}
    schema_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    # 「我」的 id。库里认不出来时可以在这里补（比如 QQ 的 uid）
    me_ids: list[str] = field(default_factory=list)
    since: datetime | None = None
    until: datetime | None = None
    max_messages: int = DEFAULT_MAX_MESSAGES
    dry_run: bool = False                   # 只看报告，不写库
    allow_carve: bool = False               # 允许从二进制里捞正文
    assume_peer: bool = False               # 判不出说话人时算作对方
    allow_memory_scan: bool = True
    memory_budget_s: float = DEFAULT_MEMORY_BUDGET_S
    keep_temp: bool = False


@dataclass
class ChatOutcome:
    peer_key: str = ""
    name: str = ""
    chat_id: str = ""
    read: int = 0
    inserted: int = 0
    skipped: int = 0
    person_id: str = ""
    person_created: bool = False
    first_ts: str = ""
    last_ts: str = ""


@dataclass
class DbOutcome:
    db: str = ""
    account: str = ""
    ok: bool = False
    key_method: str = ""
    key_detail: str = ""
    # 校验通过的密钥。**故意不进 `as_dict()`** —— 它是能解开用户聊天库的东西，
    # 只在本进程内用来省掉「下一个库再搜一遍内存」。同一个客户端下每个库的
    # salt 不同，缓存键也不同，不在这里传递的话，7 个库就是 7 次全内存扫描。
    key_hex: str = ""
    # 这个库的自动搜密钥是「到点收工」结束的（没搜完）。同一次采集里，
    # 同一个客户端的下一个库就没必要再花一份同样的预算。
    budget_hit: bool = False
    decrypt_note: str = ""
    mapping_note: str = ""
    chats: list[ChatOutcome] = field(default_factory=list)
    carved: int = 0
    problems: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def read(self) -> int:
        return sum(c.read for c in self.chats)

    @property
    def inserted(self) -> int:
        return sum(c.inserted for c in self.chats)


@dataclass
class CollectReport:
    client: str = ""
    display_name: str = ""
    dry_run: bool = False
    version: str = ""
    running: bool = False
    dbs: list[DbOutcome] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    # 关于这次报告本身的交代（比如「预演压缩了搜内存的预算」）。
    # 单独一个字段而不是塞进 `problems`：它不是出错，但会让人误读结论。
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def read(self) -> int:
        return sum(d.read for d in self.dbs)

    @property
    def inserted(self) -> int:
        return sum(d.inserted for d in self.dbs)

    @property
    def ok(self) -> bool:
        return any(d.ok for d in self.dbs)

    def describe(self) -> str:
        if not self.dbs:
            return self.problems[0] if self.problems else "没有可采的库。"
        head = "预演" if self.dry_run else "采集"
        verb = "将写入" if self.dry_run else "已写入"
        return (f"{self.display_name} {self.version or ''} {head}完成："
                f"读到 {self.read} 条，{verb} {self.inserted} 条，"
                f"涉及 {sum(len(d.chats) for d in self.dbs)} 个会话，"
                f"耗时 {self.elapsed_s:.1f}s")

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "display_name": self.display_name,
            "version": self.version,
            "running": self.running,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "read": self.read,
            "inserted": self.inserted,
            "elapsed_s": round(self.elapsed_s, 2),
            "describe": self.describe(),
            "problems": self.problems,
            "next_steps": self.next_steps,
            "notes": self.notes,
            "dbs": [{
                "db": d.db, "account": d.account, "ok": d.ok,
                "key_method": d.key_method, "key_detail": d.key_detail,
                "decrypt_note": d.decrypt_note, "mapping_note": d.mapping_note,
                "read": d.read, "inserted": d.inserted, "carved": d.carved,
                "problems": d.problems, "next_steps": d.next_steps,
                "chats": [{
                    "peer_key": c.peer_key, "name": c.name, "chat_id": c.chat_id,
                    "read": c.read, "inserted": c.inserted, "skipped": c.skipped,
                    "person_id": c.person_id, "person_created": c.person_created,
                    "first_ts": c.first_ts, "last_ts": c.last_ts,
                } for c in d.chats],
            } for d in self.dbs],
        }


# ================================================================ 解密缓存


def _fingerprint(db: Path) -> dict[str, Any]:
    """判断「库有没有变」的指纹。**WAL 必须算进去**。

    客户端在跑的时候，最新的消息只在 WAL 里。只比主库的 mtime，
    「刚聊完就来采」会采到一段旧数据而且完全看不出来。
    """
    out: dict[str, Any] = {}
    for suffix, key in (("", "main"), ("-wal", "wal")):
        p = Path(str(db) + suffix)
        if p.is_file():
            st = p.stat()
            out[key] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        else:
            out[key] = None
    return out


def _cache_paths(db: Path, client: str, account: str) -> tuple[Path, Path]:
    tag = hashlib.sha256(f"{client}|{account}|{db.resolve()}".encode()).hexdigest()[:16]
    stem = f"{client}_{account}_{db.stem}_{tag}"
    return CACHE_DIR / f"{stem}.db", CACHE_DIR / f"{stem}.json"


def _load_meta(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_meta(path: Path, data: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as exc:      # pragma: no cover - 磁盘满之类
        log.warning("写缓存元数据失败：%s", exc)


def cached_plain_db(db: Path, client: str, account: str) -> Path | None:
    """上次解密好的副本还在不在（且和现在的库一致）。"""
    plain, meta_path = _cache_paths(db, client, account)
    if not plain.is_file():
        return None
    meta = _load_meta(meta_path)
    if meta.get("fingerprint") != _fingerprint(db):
        return None
    return plain


# ================================================================ 会话分片 → 一个 chat


def _group_key(msg: reader.RawMessage) -> str:
    """这条消息属于哪个会话。

    优先用「对方」的 id：单聊里对方就是会话本身。
    实在没有（有些库的每条记录不带会话字段）就退到 `"unknown"`，
    全部归到一个会话里 —— 归错会话比归到一个笼统的会话里更坏，
    因为用户会以为是两段独立的对话。
    """
    return msg.peer_id or msg.sender_id or "unknown"


def _chat_id_of(client: str, account: str, peer_key: str) -> str:
    """稳定的会话 id。

    用 `平台:账号:对方` 而不是用数据库的文件名：同一段对话跨版本
    可能从 `message_0.db` 挪到 `message_1.db`，而「我和小鹿的聊天」没变。
    用文件名做 id 会在升级后凭空多出一个会话，历史记忆跟着断掉。
    """
    return f"{client}:{account}:{peer_key}"


def _name_of(messages: Sequence[reader.RawMessage], peer_key: str,
             name_index: reader.NameIndex | None) -> str:
    """会话的显示名：能换成备注名就用备注名，换不出来就用 id 本身。

    不编「未知联系人」这种看起来像人名的东西 —— 用户看到它不会知道
    那条记录到底属于谁，也就没法判断要不要采。
    """
    if name_index is not None:
        got = name_index.name_of(peer_key)
        if got:
            return got
    return peer_key or "未命名会话"


# ================================================================ 主流程


def _is_message_db(path: Path) -> bool:
    """这个文件是不是「能读出消息」的库。

    不是所有名字里带 msg 的库都装着消息。客户端还有一批**全文索引库**
    （`msg_fts.db`、`buddy_msg_fts.db` …）—— 里面只有分词索引，没有消息行，
    读它永远不会读到东西。可它**同样要过一次取密钥 + 解密**，而取密钥这条路
    最贵的一步是「把客户端所有进程的内存读一遍」。

    实测（本机 QQ 9.9.20.37051）：名字过滤会挑出 7 个「消息库」，其中 4 个是
    全文索引；每个索引库白跑一遍取密钥，用户等的时间就是原来的 7 倍。
    """
    name = path.name.lower()
    if not any(k in name for k in ("msg", "message")):
        return False
    if "_fts" in name:          # 全文索引：只有分词表，没有消息行
        return False
    return True


def _dbs_to_collect(det: Detected, account: str) -> tuple[list[tuple[str, Path]], list[str]]:
    """要采的库，以及「哪些被跳过了」—— 跳过必须是可见的，不能悄悄少采几个。"""
    out: list[tuple[str, Path]] = []
    skipped: list[str] = []
    for acc in det.accounts:
        if account and acc.account != account:
            continue
        if not acc.has_messages:
            continue
        for p in acc.message_dbs:
            path = Path(p)
            # 媒体库 / 表情库这类本来就没有正文；全文索引库单独说明
            if _is_message_db(path):
                out.append((acc.account, path))
            elif "_fts" in path.name.lower():
                skipped.append(path.name)
    return out, skipped


def run(store, req: CollectRequest) -> CollectReport:
    """跑一次采集。`store` 传 None 时等价于 `dry_run`（不写库）。"""
    t0 = time.perf_counter()
    spec = SUPPORT_MATRIX.get(req.client)
    rep = CollectReport(client=req.client, dry_run=req.dry_run)
    if spec is None:
        rep.problems.append(f"不认识这个客户端：{req.client}")
        return rep
    rep.display_name = spec.display_name

    det = detect_mod.detect_client(req.client)
    if det is None:
        rep.problems.append(f"探测 {spec.display_name} 失败。")
        return rep
    rep.version = det.version
    rep.running = det.running

    if not det.installed:
        rep.problems.append(f"没有找到 {spec.display_name}，先确认它装好并登录过。")
        rep.next_steps.extend(spec.guide)
        return rep
    if det.support and not det.support.supported:
        rep.problems.append(det.support.headline)
        rep.next_steps.extend(det.support.actions)
        return rep

    targets, skipped_fts = _dbs_to_collect(det, req.account)
    if skipped_fts:
        rep.notes.append(
            f"跳过了 {len(skipped_fts)} 个全文索引库（{'、'.join(skipped_fts[:3])}"
            f"{'…' if len(skipped_fts) > 3 else ''}）：里面只有分词索引、没有消息行，"
            "读它采不到东西，只会白跑一遍取密钥。")
    if not targets:
        rep.problems.append(f"{spec.display_name} 这边没有找到可采的消息库。")
        rep.next_steps.append(
            "在客户端里逐一点开要采集的会话 —— 消息库是按需建立的，"
            "没打开过的会话可能还没有本地文件。")
        return rep

    # 这个路径最贵的一步是「在客户端进程内存里找密钥」，而它一次就能定下来
    # （见 `Budget` 的说明：2.9 GB 内存，逐进程 2~12 秒）。所以日志必须有头有尾：
    # 只看到「开始」没有「结束」，就说明这段时间里进程没能活着走完 ——
    # 这条链路上没有别的线索可查了（请求日志是关的）。
    log.info("采集开始：%s，%d 个库，密钥预算 %.0fs，dry_run=%s",
             spec.display_name, len(targets), req.memory_budget_s, req.dry_run)

    write = store is not None and not req.dry_run
    found_keys: dict[str, str] = {}
    scanned_clients: set[str] = set()   # 已经为这个客户端搜过一遍内存了
    for account, db in targets:
        outcome = _collect_one(store if write else None, spec, account, db, req,
                              shared_key=found_keys.get(spec.key, ""),
                              skip_memory_scan=spec.key in scanned_clients)
        if outcome.key_hex:
            found_keys[spec.key] = outcome.key_hex
        if outcome.budget_hit:
            scanned_clients.add(spec.key)
        rep.dbs.append(outcome)
        rep.problems.extend(f"{db.name}：{p}" for p in outcome.problems)
        rep.next_steps.extend(outcome.next_steps)

    rep.elapsed_s = time.perf_counter() - t0
    log.info("采集结束：%s，读到 %d 条，写入 %d 条，耗时 %.1fs",
             spec.display_name, rep.read, rep.inserted, rep.elapsed_s)
    return rep


def _collect_one(store, spec, account: str, db: Path,
                 req: CollectRequest, shared_key: str = "",
                 skip_memory_scan: bool = False) -> DbOutcome:
    t0 = time.perf_counter()
    out = DbOutcome(db=str(db), account=account)

    # ---------- 密钥
    profiles = _profiles_for(spec)
    pasted = req.keys.get(str(db)) or req.keys.get(db.name) or req.pasted_key
    cached_key = _cached_key(db, spec)
    # 同一次采集里，前一个库已经验证通过的密钥先试 —— 它对别的库不一定对
    # （每个库有自己的 salt），但 `obtain_key` 会用这个库的第 1 页真验一遍，
    # 验不过才退回去搜内存。省掉的是「每个库都重搜一次内存」。
    skip_note = ""
    if skip_memory_scan:
        skip_note = ("同一个客户端的密钥刚刚已经按预算搜过一遍、没找到；"
                     "内存内容这几秒内不会变，所以后面的库不再重复搜。"
                     "手动粘贴的密钥不受影响。")
        out.next_steps.append(skip_note)
    attempt: KeyAttempt = obtain_key(
        db, profiles, exe_names=spec.exe_names,
        pasted=pasted, cached=shared_key or cached_key,
        allow_memory=req.allow_memory_scan and not skip_memory_scan,
        budget_s=req.memory_budget_s,
        memory_skip_note=skip_note)
    out.key_method = attempt.method
    out.key_detail = attempt.detail
    out.budget_hit = attempt.budget_hit
    if not attempt.ok:
        out.problems.append(f"取不到密钥：{attempt.detail}")
        out.next_steps.extend(_key_next_steps(spec, attempt))
        out.elapsed_s = time.perf_counter() - t0
        return out
    _remember_key(db, spec, attempt.key_hex)
    out.key_hex = attempt.key_hex
    profile = next((p for p in profiles if p.name == attempt.profile), profiles[0])

    # ---------- 解密（带缓存）
    plain = cached_plain_db(db, spec.key, account)
    if plain is None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target, _ = _cache_paths(db, spec.key, account)
        rep = decrypt_database(db, target, attempt.key_bytes, profile, apply_wal=True)
        out.decrypt_note = rep.describe()
        if not rep.ok:
            out.problems.append(f"解密失败：{rep.message}")
            out.next_steps.append(
                "解密失败的常见原因：密钥对应的是另一个库、"
                "或这个版本的加密参数变了。把这条消息连同客户端版本一起反馈。")
            out.elapsed_s = time.perf_counter() - t0
            return out
        _save_meta(_cache_paths(db, spec.key, account)[1], {
            "fingerprint": _fingerprint(db),
            "profile": profile.name,
            "fingerprint_of_plain": rep.bytes_written,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        plain = target
    else:
        out.decrypt_note = "库没有变化，复用上次解密好的副本。"

    # ---------- 认列 + 读消息
    me_ids = _me_ids_for(account, req)
    try:
        conn = sqlite3.connect(f"file:{plain.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        out.problems.append(f"打不开解密后的副本：{exc}")
        out.elapsed_s = time.perf_counter() - t0
        return out
    try:
        maps, _skipped = reader.pick_message_tables(
            conn, me_ids=me_ids, allow_carve=req.allow_carve)
        override = req.schema_overrides.get(db.name) or req.schema_overrides.get(str(db))
        if override:
            maps = _apply_override(conn, maps, override, me_ids, req.allow_carve)
        if not maps:
            out.problems.append(
                "解密成功了，但在这个库里没认出可用的消息表 —— "
                f"一共 {len(reader.list_tables(conn))} 张表。")
            out.next_steps.append(
                "这通常意味着客户端版本换了表结构。把「探测报告」里的"
                "表名列表反馈过来就能适配。")
            out.elapsed_s = time.perf_counter() - t0
            return out

        name_index = reader.build_name_index(conn, me_ids)
        for m in maps:
            _collect_table(store, out, conn, m, name_index, me_ids, spec, account, req)
    finally:
        conn.close()

    out.ok = bool(out.chats)
    if not out.ok:
        out.problems.append(
            "库读通了，但按当前条件一条消息都没取到"
            "（可能都被跳过了：说话人判不出来、正文为空、时间不在范围内）。")
        out.next_steps.append(
            "在「候选列」里手工指定说话人列，或者打开「判不出来的算对方说的」。")
    out.elapsed_s = time.perf_counter() - t0
    return out


def _apply_override(conn, maps: list[reader.Mapping], override: dict[str, str],
                    me_ids, allow_carve: bool) -> list[reader.Mapping]:
    """把用户手工指定的列套上去。

    用户指定了就**不再自动认列**：这是「程序认错了，我来改」的出口，
    再跑一遍启发式去覆盖用户的选择，等于这个出口是假的。
    """
    table = override.get("table") or (maps[0].table if maps else "")
    if not table:
        return maps
    m = reader.infer_mapping(conn, table, me_ids=me_ids, allow_carve=allow_carve)
    for key in ("ts", "text", "sender", "peer", "ext_id", "chat"):
        if override.get(key):
            setattr(m, key, override[key])
    if override.get("ts_unit"):
        m.ts_unit = override["ts_unit"]
    if override.get("me_confirmed"):
        m.me_confirmed = override["me_confirmed"] in (True, "true", "1", 1)
    m.evidence["override"] = "列表由用户手工指定"
    return [m]


def _collect_one_chat(store, msgs: list[reader.RawMessage],
                      spec, account: str, req: CollectRequest,
                      name_index: reader.NameIndex) -> ChatOutcome:
    peer_key = _group_key(msgs[0])
    name = _name_of(msgs, peer_key, name_index)
    chat_id = _chat_id_of(spec.key, account, peer_key)
    co = ChatOutcome(peer_key=peer_key, name=name, chat_id=chat_id, read=len(msgs))

    if store is None:
        co.first_ts = min(m.ts.isoformat() for m in msgs)
        co.last_ts = max(m.ts.isoformat() for m in msgs)
        return co

    # 归属：先按别名找已有的人，找不到再建。跨次采集必须落到同一个人身上，
    # 否则每采一次就多出一个「小鹿」，人物列表越用越乱。
    person = store.find_person_by_alias(name) or store.find_person_by_alias(peer_key)
    if person is None:
        person_id = store.create_person(
            name=name, aliases=[a for a in {name, peer_key} if a])
        co.person_created = True
    else:
        person_id = person.id
    co.person_id = person_id

    store.upsert_chat(chat_id, spec.key, name=name, peer_name=name,
                      me_name="我", channel=spec.key, source="collect",
                      person_id=person_id)

    rows = [Msg(chat_id=chat_id, platform=spec.key, sender=m.sender_name,
                role=m.role, ts=m.ts, text=m.text, msg_type=m.msg_type,
                ext_id=m.ext_id or None) for m in msgs]
    inserted, skipped = store.insert_messages(rows)
    co.inserted, co.skipped = inserted, skipped

    ts_all = sorted(m.ts.isoformat() for m in msgs)
    co.first_ts, co.last_ts = ts_all[0], ts_all[-1]
    # 指纹从**库里实际存下的内容**算，不从「本次读到的」算 —— 见
    # `Store.messages_fingerprint` 的说明。
    fingerprint = store.messages_fingerprint(chat_id)
    store.save_cursor(
        spec.key, account, peer_key,
        person_id=person_id, chat_id=chat_id,
        last_ts=co.last_ts, last_ext_id=msgs[-1].ext_id,
        fingerprint=fingerprint, merged_count=store.merged_message_count(chat_id),
        collected_from=req.since.isoformat() if req.since else "",
        status="ok",
        message=f"读到 {co.read} 条，新写入 {inserted} 条")
    return co


def _fingerprint_of(rows: Sequence[Msg]) -> str:
    """内容指纹：条数 + 最后一条的 (ext_id, 时间, 文本)。

    和 `store._fingerprint_of` 同构（那边吃 dict，这边吃 Msg），
    只用于**预演**（不写库时没有库可以读，只能拿手上的数据算）。
    正式采集走 `Store.messages_fingerprint`，那边从库里读。
    """
    if not rows:
        return "empty"
    tail = rows[-1]
    body = f"{len(rows)}|{tail.ext_id or ''}|{tail.ts.isoformat()}|{tail.text}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _collect_table(store, out: DbOutcome, conn, m: reader.Mapping,
                   name_index: reader.NameIndex, me_ids: Sequence[str],
                   spec, account: str, req: CollectRequest) -> None:
    if m.needs_sender and not req.assume_peer:
        out.problems.append(f"{m.table}：{m.problems[0] if m.problems else '说话人未认出'}")
        out.next_steps.append(
            f"给 `{m.table}` 指定说话人列，或打开「判不出来的算对方说的」。")
        return
    res = reader.read_messages(
        conn, m, name_index=name_index, me_ids=me_ids,
        since=req.since, until=req.until, limit=req.max_messages,
        assume_peer_when_unknown=req.assume_peer, allow_carve=req.allow_carve)
    out.mapping_note = m.describe()
    out.carved += res.carved
    out.problems.extend(f"{m.table}：{p}" for p in res.problems)
    if not res.messages:
        out.problems.append(f"{m.table}：{res.summary()}")
        return

    # 按会话分组。保持首次出现的顺序，报告读起来才像时间顺序。
    groups: dict[str, list[reader.RawMessage]] = {}
    for msg in res.messages:
        groups.setdefault(_group_key(msg), []).append(msg)
    for key, msgs in groups.items():
        out.chats.append(_collect_one_chat(store, msgs, spec, account, req,
                                           name_index))
        log.debug("会话 %s：%d 条", key, len(msgs))


# ================================================================ 辅助


def _profiles_for(spec):
    from .sqlcipher import GENERIC_PROFILES, QQ_NT_PROFILES, WECHAT4_PROFILES

    if spec.key == "qq":
        return QQ_NT_PROFILES
    if spec.key == "wechat":
        return WECHAT4_PROFILES
    return GENERIC_PROFILES


def _key_cache_key(db: Path, spec) -> str:
    """密钥缓存的键：客户端 + 库的 salt。

    用 salt 而不是文件路径：库被重建（换密钥）时 salt 必然变，
    缓存自动失效 —— 不会出现「拿着旧密钥一直解密失败，还找不到原因」。
    """
    try:
        tag = salt_of(db, _profiles_for(spec)[0]).hex()
    except OSError:
        tag = ""
    return f"{spec.key}.{db.name}.{tag or 'nosalt'}"


def _cached_key(db: Path, spec) -> str:
    """上次验证通过的密钥。按 salt 存 —— 库换密钥 salt 就变，缓存自动失效。"""
    try:
        # 这里不能依赖 store（可能没传），所以直接读一个旁挂文件
        meta = _load_meta(CACHE_DIR / "keys.json")
        return str(meta.get(_key_cache_key(db, spec)) or "")
    except Exception:      # pragma: no cover
        return ""


def _remember_key(db: Path, spec, key_hex: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / "keys.json"
        meta = _load_meta(path)
        meta[_key_cache_key(db, spec)] = key_hex
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:      # pragma: no cover
        log.warning("记住密钥失败：%s", exc)


def _me_ids_for(account: str, req: CollectRequest) -> list[str]:
    """「我」的 id 候选。

    账号目录名是最可靠的一个：微信是 `wxid_xxx`，QQ 就是 QQ 号本身。
    再叠上用户在界面上补的（QQ 的库内部用的是 `u_xxx` uid，
    和目录名对不上，必须让用户补一次）。
    """
    out: list[str] = []
    for value in [account, *(req.me_ids or [])]:
        v = str(value or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _key_next_steps(spec, attempt: KeyAttempt) -> list[str]:
    steps = [
        "可以先用「半自动采集」：在客户端里选中消息 → 复制，"
        "程序会从剪贴板里接住内容和时间，完全不需要密钥。",
    ]
    if attempt.method in ("memory", "memory-hex", "memory-scan"):
        steps.append(
            f"{spec.display_name} 这个版本的密钥没有按常见方式放在内存里"
            "（自动尝试已经按预算跑完并留了结论）。"
            "如果你手上有密钥，粘进来即可，后面的解密和解析全都是真跑的。")
    return steps


# ================================================================ 预演

# 预演时给「搜内存找密钥」的时间上限。正式采集默认 60 秒，预演只给 12 秒。
# 理由：预演是用户点一下就想看到结果的入口，卡一分钟才出结论等于没做这个入口。
# 代价是预演可能报「取不到密钥」而正式采集能取到 —— 所以必须在报告里说清楚，
# 否则用户会以为这条路彻底走不通，直接放弃。
PREVIEW_MEMORY_BUDGET_S = 12.0


def preview(store, req: CollectRequest) -> CollectReport:
    """预演：走完整条链路，但不写库。用来回答「这套配置能不能采到东西」。"""
    req = CollectRequest(**{**req.__dict__, "dry_run": True})
    trimmed = req.memory_budget_s > PREVIEW_MEMORY_BUDGET_S
    if trimmed:
        req.memory_budget_s = PREVIEW_MEMORY_BUDGET_S
    rep = run(None, req)
    if trimmed:
        rep.notes.append(
            f"预演把「在进程内存里找密钥」的时间压到 {PREVIEW_MEMORY_BUDGET_S:.0f} 秒"
            f"（正式采集默认 {DEFAULT_MEMORY_BUDGET_S:.0f} 秒）。"
            "所以这里报「取不到密钥」不代表正式采集也取不到。")
    return rep
