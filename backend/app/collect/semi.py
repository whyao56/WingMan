"""半自动采集：你复制，我接住。

## 它在整条链路里的位置

自动采集（`pipeline.py`）需要密钥才能读库；密钥这条路在本机这两个版本上
走不通（`keys.py` 里有完整实测）。**半自动采集不需要密钥**：
用户在客户端里选中消息 → 复制 → 本模块从剪贴板里接住内容和时间。
它原本的设计定位是「风控/安全场景的兜底」，但实测下来它其实是
**当下唯一能真正跑通的通道**（两个客户端的界面都读不到消息文本，
见 `clipboard.py` 的实测表格）。

## 「自动写库」和「等人确认」的分界

分界线只有一条：**这条消息的归属有没有依据**。

- 说话人能对上已知的名字（`小鹿:`、`小鹿 2026-…`）→ 归属有依据 → 直接写库。
  这是用户要的「点一下就到库里」。
- 说话人对不上 → **挂起，等人说一句**。不猜。
  「把老王的话记成小鹿说的」这种错误是静默的，之后所有分析都建立在错的人身上。

时间也走同一条逻辑：剪贴板里带时间就用它；没带就用抓取时刻顶替，
但**记为 `assumed`**，界面上会标出来。真时间轴上混进假时间，
比少几条消息坏得多。

## 为什么消息本身不自动判重就够了

`messages` 表有 `UNIQUE (chat_id, sender, ts, text)`，同一条消息复制两次会被忽略。
但当时间是 `assumed` 的时候，两次抓取的时间不同 → 唯一键不同 → 会进两条。
所以额外加一道「最近 50 条里文本一模一样且同一归属就跳过」的护栏 ——
它只在时间不可信的时候启用，避免把「用户真的重复说了一句一样的话」误杀。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..schemas import Msg
from . import clipboard as cb

log = logging.getLogger("wingman.collect.semi")

STATE_FILE = DATA_DIR / "collect_cache" / "semi_state.json"
POLL_INTERVAL = 0.4
DEDUPE_WINDOW = 50

# 「我的称呼」候选。库里记的是 me_name，用户也可能把消息头写成「我」。
DEFAULT_ME_NAMES = ("我", "自己", "本机")


@dataclass
class PendingItem:
    """一条待落库的消息。"""

    id: str
    text: str
    ts: str = ""                 # ISO 时间；空表示剪贴板里没带
    role: str = ""               # me | peer | ""（要用户说）
    sender: str = ""
    ts_source: str = ""          # clipboard | assumed | manual
    msg_type: str = "text"
    # 剪贴板里没带时间，而且这次采集的策略是「问我」而不是「用抓取时刻顶替」。
    # 单独一个标记而不是复用 role：时间不对和归属不对是两回事，界面上问的话也不一样。
    needs_time: bool = False

    @property
    def needs_role(self) -> bool:
        return self.role not in ("me", "peer")

    @property
    def blocked(self) -> bool:
        return self.needs_role or self.needs_time


@dataclass
class Capture:
    """一次剪贴板变化抓到的内容（可能有多条）。"""

    id: str
    at: float
    shape: str = "plain"
    note: str = ""
    items: list[PendingItem] = field(default_factory=list)
    status: str = "pending"      # pending | committed | discarded
    written: int = 0
    duplicates: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def needs_user(self) -> bool:
        return self.status == "pending" and any(i.blocked for i in self.items)


@dataclass
class SemiState:
    active: bool = False
    client: str = ""
    peer_name: str = ""
    person_id: str = ""
    chat_id: str = ""
    me_name: str = "我"
    started_at: float = 0.0
    captures: int = 0
    committed: int = 0
    skipped_duplicate: int = 0
    last_change_at: float = 0.0
    last_error: str = ""
    poll_hint: str = ""


class SemiCollector:
    """剪贴板观察器 + 待确认队列。

    单例（`get_semi()`）：剪贴板是全局资源，开两个观察器会互相抢。
    """

    def __init__(self) -> None:
        self.state = SemiState()
        self.captures: list[Capture] = []
        self._store = None
        self._watcher: cb.ClipboardWatcher | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._me_names: list[str] = list(DEFAULT_ME_NAMES)
        self._peer_names: list[str] = []
        # 剪贴板里没带时间时的策略：assumed（用抓取时刻，界面标出来）| ask（挂起等人填）
        self._missing_time = "assumed"
        self._load()

    # ------------------------------------------------------------ 生命周期

    def start(self, store, *, client: str, peer_name: str = "",
              person_id: str = "", missing_time: str = "assumed") -> SemiState:
        """开始监听。

        `peer_name` / `person_id` 决定「抓到的消息归到谁名下」。
        给了 person_id 就用那个人已有的会话（同一个渠道复用同一个会话 id），
        这样半自动采的内容和自动采的内容会落在同一段记录里，而不是分成两段。
        """
        with self._lock:
            self._store = store
            person_id = self._resolve_person_id(store, person_id, peer_name)
            self._me_names, self._peer_names = self._resolve_names(
                store, person_id, peer_name)
            self.state = SemiState(
                active=True, client=client, peer_name=peer_name,
                person_id=person_id, started_at=time.time(),
                me_name=self._me_names[0] if self._me_names else "我",
            )
            self.state.chat_id = self._resolve_chat_id(store, client, person_id, peer_name)
            self._missing_time = ("ask" if str(missing_time) == "ask" else "assumed")
            self._watcher = cb.ClipboardWatcher(interval=POLL_INTERVAL)
            # 记住「开始监听这一刻的剪贴板」：不然一按开始就会把上一次
            # 复制的东西当成新内容收进来。
            self._watcher.reset()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="semi-collect",
                                            daemon=True)
            self._thread.start()
            self.state.poll_hint = self._poll_hint()
            self._save()
        log.info("半自动采集已开启：%s → %s", client, self.state.peer_name or person_id)
        return self.state

    def stop(self) -> SemiState:
        with self._lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=3)
                self._thread = None
            self.state.active = False
            self._save()
        log.info("半自动采集已关闭")
        return self.state

    def status(self) -> SemiState:
        with self._lock:
            return self.state

    # ------------------------------------------------------------ 观察循环

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:            # pragma: no cover - 兜底不崩线程
                log.exception("半自动采集循环出错")
                with self._lock:
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        capture = watcher.poll(me_names=self._me_names, peer_names=self._peer_names)
        if capture is None:
            return
        with self._lock:
            self.state.last_change_at = capture.at
            self.state.captures += 1
            item = self._ingest(capture)
            self.captures.append(item)
            # 队列只留最近 200 条，界面上翻不过来的部分没有意义，
            # 但也不能无限涨 —— 挂着不动的时候它会一直堆。
            if len(self.captures) > 200:
                self.captures = self.captures[-200:]
            self.state.poll_hint = self._poll_hint()
            self._save()

    def _ingest(self, capture: cb.Capture) -> Capture:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        items = [
            PendingItem(
                id=uuid.uuid4().hex[:12],
                text=c.text,
                ts=c.ts,
                role=c.role,
                sender=c.sender,
                ts_source=c.ts_source or "",
            )
            for c in capture.items
        ]
        for it in items:
            if not it.ts:
                if self._missing_time == "ask":
                    it.ts_source = ""
                    it.needs_time = True
                else:
                    it.ts = now
                    it.ts_source = "assumed"
        cap = Capture(id=uuid.uuid4().hex[:12], at=capture.at, shape=capture.shape,
                      note=capture.note, items=items)
        # 归属和时间都有依据 → 直接写库（这是用户要的「点一下就到库里」）
        if items and all(not i.blocked for i in items):
            self._commit_capture(cap)
        elif not items:
            cap.status = "discarded"
            cap.problems.append("剪贴板里没有可用的文本。")
        return cap

    # ------------------------------------------------------------ 落库

    def _commit_capture(self, cap: Capture) -> Capture:
        store = self._store
        if store is None:
            cap.problems.append("没有存储层，无法写库。")
            return cap
        chat_id = self.state.chat_id
        if not chat_id:
            cap.problems.append("没有确定要写入哪个会话，请先指定采集对象。")
            return cap

        peer = self.state.peer_name or "对方"
        history = store.list_messages(chat_id, limit=DEDUPE_WINDOW)
        rows: list[Msg] = []
        for it in cap.items:
            if it.blocked:
                continue          # 归属或时间还没依据的不写
            sender = "我" if it.role == "me" else (it.sender or peer)
            # 时间不可信时额外查重：同一条消息复制两次会拿到两个不同的抓取时刻，
            # 唯一键因此不同，会被当成两条。只在 assumed 时启用 ——
            # 时间可信时「同一句话在同一秒说两次」几乎不可能，不需要误杀。
            if it.ts_source == "assumed" and _seen_recently(history, sender, it.text):
                cap.duplicates += 1
                continue
            rows.append(Msg(
                chat_id=chat_id, platform=self.state.client, sender=sender,
                role=it.role, ts=_parse_iso(it.ts), text=it.text,
                msg_type=it.msg_type, ext_id=None, ts_source=it.ts_source or "exact",
                captured_at=_capture_iso(cap.at),
            ))

        if not rows:
            # 计数必须在提前返回**之前**更新：整块都是重复的时候，
            # 恰恰是用户最需要看到「刚才那次复制没进库」的时刻。
            self.state.skipped_duplicate += cap.duplicates
            cap.status = "committed" if cap.duplicates else "pending"
            if cap.duplicates:
                cap.problems.append(f"{cap.duplicates} 条和刚采过的内容重复，已跳过。")
            return cap

        store.upsert_chat(chat_id, self.state.client,
                          name=self.state.peer_name or "剪贴板采集",
                          peer_name=self.state.peer_name, me_name=self.state.me_name,
                          channel=self.state.client, source="collect",
                          person_id=self.state.person_id)
        inserted, _skipped = store.insert_messages(rows)
        cap.written = inserted
        cap.status = "committed"
        self.state.committed += inserted
        self.state.skipped_duplicate += cap.duplicates
        if inserted < len(rows):
            cap.problems.append(
                f"{len(rows) - inserted} 条库里已经有了一模一样的内容，已忽略。")
        return cap

    def commit(self, capture_id: str, decisions: dict[str, Any] | None = None) -> Capture | None:
        """用户对挂起的条目做出决定后落库。

        `decisions` 形如：
            {"items": {"<item_id>": {"role": "peer", "ts": "...", "text": "..."}},
             "apply_to_rest": "peer"}
        `apply_to_rest` 是「剩下的都是对方说的」这种批量动作 ——
        它的存在是必要的（一次复制里往往只有第一条带称呼），
        但必须由人点，程序不替人做这个决定。
        """
        with self._lock:
            cap = next((c for c in self.captures if c.id == capture_id), None)
            if cap is None or cap.status != "pending":
                return cap
            decisions = decisions or {}
            per_item = decisions.get("items") or {}
            fallback = str(decisions.get("apply_to_rest") or "")
            if fallback not in ("me", "peer"):
                fallback = ""
            touched = 0
            for it in cap.items:
                patch = per_item.get(it.id) or {}
                role = str(patch.get("role") or "")
                if role in ("me", "peer"):
                    it.role = role
                    touched += 1
                ts = str(patch.get("ts") or "").strip()
                if ts:
                    it.ts = ts
                    it.ts_source = str(patch.get("ts_source") or "manual")
                    it.needs_time = False
                if patch.get("use_capture_time"):
                    # 用户明确说「就用抓取时刻」——那也是个决定，不是默认
                    it.ts_source = "assumed"
                    it.needs_time = False
                    touched += 1
                text = str(patch.get("text") or "")
                if text.strip():
                    it.text = text
            if fallback:
                for it in cap.items:
                    if it.needs_role:
                        it.role = fallback
                        touched += 1
            if any(i.blocked for i in cap.items):
                cap.problems.append(
                    "还有条目没说清是谁说的（或时间没定）—— "
                    "归属和时间没有依据时程序不会替你做决定。")
                return cap
            self._commit_capture(cap)
            self.state.poll_hint = self._poll_hint()
            self._save()
            return cap

    def discard(self, capture_id: str, reason: str = "") -> Capture | None:
        with self._lock:
            cap = next((c for c in self.captures if c.id == capture_id), None)
            if cap is None:
                return None
            cap.status = "discarded"
            if reason:
                cap.problems.append(f"已丢弃：{reason}")
            self.state.poll_hint = self._poll_hint()
            self._save()
            return cap

    def clear(self) -> int:
        """清掉已处理的记录，只留待确认的。"""
        with self._lock:
            before = len(self.captures)
            self.captures = [c for c in self.captures if c.status == "pending"]
            self._save()
            return before - len(self.captures)

    # ------------------------------------------------------------ 查询

    def poll(self, since: str = "") -> dict[str, Any]:
        """给前端的轮询接口：返回新抓到的内容 + 待确认数量。

        `since` 传上次拿到的最后一个 capture id；只返回它之后的新条目。
        用 id 而不是时间戳：同一毫秒内可能来两条，用时间比会漏。
        """
        with self._lock:
            caps = list(self.captures)
            st = self.state
        if since:
            idx = next((i for i, c in enumerate(caps) if c.id == since), None)
            if idx is not None:
                caps = caps[idx + 1:]
        return {
            "state": _state_dict(st),
            "captures": [_capture_dict(c) for c in caps],
            "pending": sum(1 for c in caps if c.needs_user),
            "hint": st.poll_hint,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": _state_dict(self.state),
                "captures": [_capture_dict(c) for c in self.captures],
                "pending": sum(1 for c in self.captures if c.needs_user),
            }

    # ------------------------------------------------------------ 内部

    def _resolve_person_id(self, store, person_id: str, peer_name: str) -> str:
        """把「我要采谁」落成一个真实存在的人。

        用户可能只填了个称呼（「小鹿」）。如果库里已经有叫「小鹿」的人，
        **必须复用** —— 不复用的话，`upsert_chat` 会按「对方称呼」再建一个
        同名的小鹿，采集采一次就多一个人。用户看到的是人物列表里两个小鹿，
        而这两个小鹿的消息永远不会互相检索到。

        匹配走 `find_person_by_alias`（**精确**匹配名与别名，忽略大小写）。
        不做模糊匹配：「小明」和「小明明」合并是静默错误，用户不会发现。
        找不到就返回空字符串 —— 交给 `ensure_person_for_chat` 按称呼新建，
        这是「第一次见这个人」的正常路径，不是异常。
        """
        if person_id or store is None or not peer_name:
            return person_id
        found = store.find_person_by_alias(peer_name)
        if found is not None:
            log.info("「%s」库里已存在（%s），这次的记录归到它名下，不新建人",
                     peer_name, found.id)
            return found.id
        return ""

    def _poll_hint(self) -> str:
        if not self.state.active:
            return "半自动采集没在运行。"
        who = self.state.peer_name or "选定的对象"
        return (f"现在去 {self.state.client} 里选中「{who}」的聊天内容，"
                "按 Ctrl+C 复制 —— 内容和时间会立刻出现在这里。")

    def _resolve_names(self, store, person_id: str,
                       peer_name: str) -> tuple[list[str], list[str]]:
        """已知的说话人名字：越全越好，因为「认得出是谁」是归属的唯一依据。"""
        me = list(DEFAULT_ME_NAMES)
        peers: list[str] = []
        if peer_name:
            peers.append(peer_name)
        if store is not None and person_id:
            person = store.get_person(person_id)
            if person is not None:
                peers.extend([person.name, *(person.aliases or [])])
                detail = store.person_detail(person_id)
                for ch in (detail.channels if detail else []):
                    for name in (ch.name, ch.peer_name):
                        if name:
                            peers.append(name)
                    if ch.me_name and ch.me_name not in me:
                        me.append(ch.me_name)
        return _dedupe(me), _dedupe(peers)

    def _resolve_chat_id(self, store, client: str, person_id: str,
                         peer_name: str) -> str:
        """决定写进哪个会话。

        优先复用这个人**在同一个渠道上已有的会话** —— 用户心里
        「我和小鹿的微信聊天」是一件事，不该因为来源不同（自动采/剪贴板）
        就变成两段互不相干的记录。
        """
        if store is not None and person_id:
            detail = store.person_detail(person_id)
            for ch in (detail.channels if detail else []):
                if ch.channel == client or ch.platform == client:
                    return ch.chat_id
            return f"{client}:{person_id}"
        return f"{client}:semi:{peer_name or 'unknown'}"

    # ------------------------------------------------------------ 持久化

    def _save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "state": _state_dict(self.state),
                "captures": [_capture_dict(c) for c in self.captures],
                "me_names": self._me_names,
                "peer_names": self._peer_names,
                "missing_time": self._missing_time,
            }
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError as exc:      # pragma: no cover
            log.warning("写半自动采集状态失败：%s", exc)

    def _load(self) -> None:
        """把上次没处理完的待确认条目捞回来。

        目的是「不丢」：这些条目是用户主动复制进来的，
        因为关了一次窗口就消失，等于让人白干一遍。
        """
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._me_names = list(raw.get("me_names") or DEFAULT_ME_NAMES)
        self._peer_names = list(raw.get("peer_names") or [])
        self._missing_time = str(raw.get("missing_time") or "assumed")
        fields = set(PendingItem.__dataclass_fields__)
        for c in raw.get("captures") or []:
            items: list[PendingItem] = []
            for i in c.get("items") or []:
                if not isinstance(i, dict):
                    continue
                items.append(PendingItem(**{k: v for k, v in i.items()
                                            if k in fields}))
            if not items:
                continue
            self.captures.append(Capture(
                id=c.get("id") or uuid.uuid4().hex[:12],
                at=float(c.get("at") or 0), shape=c.get("shape") or "plain",
                note=c.get("note") or "", items=items,
                status=c.get("status") or "pending",
                written=int(c.get("written") or 0),
                duplicates=int(c.get("duplicates") or 0),
                problems=list(c.get("problems") or []),
            ))
        # 只有待确认的才有保留价值；已经写进库的留着只在界面上占地方
        self.captures = [c for c in self.captures if c.status == "pending"]
        if self.captures:
            log.info("捞回 %d 条没来得及确认的剪贴板内容", len(self.captures))


def _seen_recently(history: list[dict[str, Any]], sender: str, text: str) -> bool:
    """最近的历史里有没有同一归属、同一文本的消息。"""
    body = text.strip()
    return any((h.get("text") or "").strip() == body and h.get("sender") == sender
               for h in history)


def _parse_iso(value: str) -> datetime:
    """解析 ISO 时间。带不带时区都接受，坏值退到当前时刻并标出来。"""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _capture_iso(at: float) -> str:
    """把 `Capture.at`（epoch 秒）转成 ISO 的**采集时刻**。

    与消息自身的 `ts` 分开存：这个值是「我何时抓的」，不是「消息何时发生的」。
    剪贴板没带时间时，界面要能同时展示「采集于」和「消息时间」，
    所以两者都得留。
    """
    if not at:
        return ""
    try:
        return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 极端坏值
        return ""


def _dedupe(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        n = str(n or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def _item_dict(it: PendingItem) -> dict[str, Any]:
    d = asdict(it)
    d["needs_role"] = it.needs_role
    return d


def _capture_dict(c: Capture) -> dict[str, Any]:
    return {
        "id": c.id,
        "at": c.at,
        "at_text": (datetime.fromtimestamp(c.at).strftime("%H:%M:%S") if c.at else ""),
        "shape": c.shape,
        "note": c.note,
        "status": c.status,
        "written": c.written,
        "duplicates": c.duplicates,
        "problems": c.problems,
        "needs_user": c.needs_user,
        "items": [_item_dict(i) for i in c.items],
    }


def _state_dict(s: SemiState) -> dict[str, Any]:
    return asdict(s)


_SEMI: SemiCollector | None = None


def get_semi() -> SemiCollector:
    global _SEMI
    if _SEMI is None:
        _SEMI = SemiCollector()
    return _SEMI
