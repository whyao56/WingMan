"""半自动采集（剪贴板）的守卫。

这层代码做的事情听起来很朴素：**你复制，我接住**。但它有两个地方一旦写错
就是「静默错」，而静默错比崩溃难查得多：

1. **归属不能猜。** 认不出「这句话是谁说的」时，程序必须停下等人说一句，
   不能默认成对方。默认成对方的话，界面上看起来一切正常，
   而之后所有画像、检索、建议都建立在「把老王的话记成小鹿说的」之上。
   所以下面有一半用例在盯「该拒绝的时候有没有拒绝」。
2. **时间不能假装。** 剪贴板经常不带时间。用抓取时刻顶替是可以的，
   但必须**标成 `assumed`**，否则真时间轴上混进假时间，
   「他平时几点找我说话」这种结论就是错的。

另外还有一条业务约束：**一个人只能有一个「人」实体**。
半自动采集如果每次都按称呼新建人，用户采一次就会看到两个小鹿，
而这两个小鹿的消息永远检索不到一起 —— 用例 `...creates_a_twin...` 钉这条。

跑法：

    cd backend
    python -m pytest tests/test_collect_semi.py -q
    python tests/test_collect_semi.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:  # pytest 可选：没有 pytest 时用本文件的 main() 直跑
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="wingman_semi_"))


def _modules(tmp: Path):
    """拿到一套指向临时目录的 (store, clipboard, semi) 模块。

    `semi.STATE_FILE` 是模块级常量、在 import 时就算好了，所以必须
    **在实例化之前**把它改到临时目录 —— 否则测试会读到用户真实的那份
    待确认队列，用例的结果就取决于「用户上次复制过什么」。
    """
    from app import config

    config.DATA_DIR = tmp
    config.get_settings.cache_clear()

    from app import store as store_mod
    from app.collect import clipboard as cb
    from app.collect import semi as semi_mod

    semi_mod.STATE_FILE = tmp / "collect_cache" / "semi_state.json"
    semi_mod._SEMI = None

    db = store_mod.Store(tmp / "wingman.db")
    db.init()
    return db, cb, semi_mod


def _cap(cb, items, *, shape="structured", note="", at=None):
    return cb.Capture(at=at or time.time(), items=items, shape=shape,
                      note=note, needs_sender=any(i.needs_sender for i in items))


def _item(cb, sender="", text="", ts="", role="", ts_source="") -> object:
    return cb.Captured(sender=sender, text=text, ts=ts, role=role,
                       ts_source=ts_source, raw_line=text)


class _FakeWatcher:
    """把「剪贴板变了」这件事变成可控的输入。

    用它而不是真的去写系统剪贴板：真实的剪贴板是全局共享资源，
    测试去写它会覆盖用户当前复制的东西，而且并发跑测试时会互相抢。
    """

    def __init__(self, captures):
        self.queue = list(captures)
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def poll(self, **_kwargs):
        return self.queue.pop(0) if self.queue else None


def _feed(semi, watcher, *captures):
    """喂进几条剪贴板内容，返回这次新增的 Capture 列表。

    走真实的 `_tick()` 而不是直接调 `_ingest()`：`_tick` 里有计数、
    有队列裁剪、有 `_save()`（持久化）。绕过它测出来的「通过」不算数 ——
    真正会丢数据的路径恰恰是「重启后待确认的还在不在」。
    """
    before = len(semi.captures)
    watcher.queue.extend(captures)
    semi._watcher = watcher
    for _ in range(len(captures)):
        semi._tick()
    return semi.captures[before:]


def _started(semi, store, *, client="qq", peer_name="小鹿", person_id="",
             me_names=("我",), missing_time="inferred", peer_names=None):
    """把采集器摆成「已经开始监听」的状态，但不真的起后台线程。

    `start()` 会起线程 + 建真实剪贴板观察器；这里只复用它对状态的初始化
    逻辑（名字解析、会话归属），让用例保持确定。
    """
    sc = semi.SemiCollector()
    sc._store = store
    sc._me_names = list(me_names)
    sc._peer_names = list(peer_names if peer_names is not None else [peer_name])
    sc._missing_time = missing_time
    sc.state = semi.SemiState(active=True, client=client, peer_name=peer_name,
                              person_id=person_id, me_name=sc._me_names[0])
    sc.state.chat_id = f"{client}:semi:{peer_name}"
    return sc


def _texts(store, chat_id: str) -> list[str]:
    return [r["text"] for r in store.list_messages(chat_id, limit=200)]


# ================================================================ 归属：有依据就写


def test_capture_with_known_sender_goes_straight_into_the_db() -> None:
    """认得出说话人 → 直接落库。

    这是用户要的「点一下就进库里」：他不想每采一条都回答一遍「这是谁说的」。
    复制出来的块头里明明写着「小鹿」，那就是依据。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        got = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="我今天加班到十点", ts="2026-09-28T21:03:15",
                  role="peer", ts_source="clipboard"),
        ]))
        assert len(got) == 1
        cap = got[0]
        assert cap.status == "committed", cap.problems
        assert not cap.needs_user
        assert cap.written == 1

        rows = store.list_messages(sc.state.chat_id, limit=10)
        assert len(rows) == 1
        assert rows[0]["sender"] == "小鹿"
        assert rows[0]["role"] == "peer"
        assert rows[0]["text"] == "我今天加班到十点"
        assert rows[0]["ts_source"] == "clipboard"
        assert rows[0]["ts"].startswith("2026-09-28T21:03:15")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_my_own_line_is_written_as_me_not_as_the_peer() -> None:
    """「我」说的那句要写成 role=me / sender=我，不能混成对方。

    一次复制里通常既有自己说的话也有对方说的话。如果只按「有块头就是对方」
    处理，用户自己说的话会被记到对方头上 —— 之后分析出来的「Ta 的性格」
    其实是在分析用户本人。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store, peer_names=["小鹿", "我"])
        got = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="我", text="那你早点休息", ts="2026-09-28T21:04:00",
                  role="me", ts_source="clipboard"),
            _item(cb, sender="小鹿", text="嗯，你呢？", ts="2026-09-28T21:04:30",
                  role="peer", ts_source="clipboard"),
        ]))
        assert got[0].status == "committed", got[0].problems
        rows = store.list_messages(sc.state.chat_id, limit=10)
        assert [(r["sender"], r["role"]) for r in rows] == [("我", "me"), ("小鹿", "peer")]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 归属：没依据就停


def test_unknown_sender_is_never_guessed_and_lands_nowhere() -> None:
    """认不出说话人 → 挂起等确认，库里**一条都不能有**。

    这是本模块最重要的一条：宁可少采几条，也不能把话记到错的人头上。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        got = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, text="老王说周末一起吃饭", ts="2026-09-28T21:05:00"),
        ]))
        cap = got[0]
        assert cap.status == "pending"
        assert cap.needs_user
        assert cap.items[0].needs_role
        assert cap.written == 0
        assert store.list_messages(sc.state.chat_id, limit=10) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_block_with_only_the_first_line_attributed_holds_the_rest() -> None:
    """一次复制多条、只有第一条带称呼 → 整块挂起，等人说一句。

    不把「第一条是小鹿」推广到后面几条：QQ 多选复制里后面的行也完全是
    对方说的、还是混了别的话，程序无从判断。猜错的代价是静默的。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        got = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="在吗", ts="2026-09-28T21:06:00",
                  role="peer", ts_source="clipboard"),
            _item(cb, text="帮我看个东西", ts="2026-09-28T21:06:10"),
            _item(cb, text="有点急", ts="2026-09-28T21:06:20"),
        ]))
        cap = got[0]
        assert cap.status == "pending"
        assert [i.needs_role for i in cap.items] == [False, True, True]
        assert store.list_messages(sc.state.chat_id, limit=10) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_to_rest_writes_the_whole_block_without_touching_others() -> None:
    """用户点「剩下的都是对方说的」→ 整块落库，且每条都归到对方。

    `apply_to_rest` 是必要的批量动作（一次复制里往往只有第一条带称呼），
    但必须由人点。这条用例证明「点了之后确实按人说的执行」。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="在吗", ts="2026-09-28T21:06:00",
                  role="peer", ts_source="clipboard"),
            _item(cb, text="帮我看个东西", ts="2026-09-28T21:06:10"),
            _item(cb, text="有点急", ts="2026-09-28T21:06:20"),
        ]))[0]
        assert cap.status == "pending"

        out = sc.commit(cap.id, {"apply_to_rest": "peer"})
        assert out is not None
        assert out.status == "committed", out.problems
        rows = store.list_messages(sc.state.chat_id, limit=10)
        assert [r["text"] for r in rows] == ["在吗", "帮我看个东西", "有点急"]
        assert {r["sender"] for r in rows} == {"小鹿"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_commit_still_refuses_when_the_user_left_something_undecided() -> None:
    """用户点确认但还有条目没说清 → 拒绝落库，并说明原因。

    「确认」不等于「你替我决定」。半截确认就直接写库，等于把没决定的那几条
    按默认值处理 —— 而默认值在这里是不存在的。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, text="这句没说谁说的", ts="2026-09-28T21:07:00"),
            _item(cb, text="这句也没说", ts="2026-09-28T21:07:10"),
        ]))[0]
        out = sc.commit(cap.id, {"items": {cap.items[0].id: {"role": "peer"}}})
        assert out is not None
        assert out.status == "pending"
        assert any("没说清是谁说的" in p for p in out.problems), out.problems
        assert store.list_messages(sc.state.chat_id, limit=10) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_commit_can_also_fix_the_text_and_the_time() -> None:
    """用户可以把文本和时间一起改掉 —— 剪贴板抽错行是常事。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, text="小鹿 21:08:00", ts="2026-09-28T21:08:00"),
        ]))[0]
        out = sc.commit(cap.id, {"items": {cap.items[0].id: {
            "role": "peer", "text": "那明天见", "ts": "2026-09-28T21:09:00",
        }}})
        assert out is not None and out.status == "committed", out.problems
        row = store.list_messages(sc.state.chat_id, limit=10)[0]
        assert row["text"] == "那明天见"
        assert row["ts"].startswith("2026-09-28T21:09:00")
        assert row["ts_source"] == "manual", "用户填的时间要标成 manual，不是 assumed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 时间：诚实


def test_missing_time_is_inferred_from_the_timeline_not_stamped_now() -> None:
    """剪贴板没带时间 → **按会话时间线推定**，不是直接盖一个「抓取时刻」。

    需求 3 的核心：记录的是消息自己的时刻。旧行为把 `datetime.now()`
    写进 `ts`，等于把「你什么时候复制的」当成「她什么时候说的」——
    随手一按差几秒，回头补录几天前的记录就差几天，而「他平时几点找我说话」
    这类结论直接建立在这个字段上。

    这里断言两件事，缺一不可：
    1. 推定出来的时间**排在库中最后一条消息之后**（时间线是单调的）；
    2. 它**不晚于采集时刻**（消息不可能来自未来），且来源标成 `inferred`。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        # 先往会话里放一条「已知的上一句」，作为时间线锚点
        store.upsert_chat(sc.state.chat_id, "qq", name="小鹿", peer_name="小鹿")
        store.insert_messages([semi.Msg(
            chat_id=sc.state.chat_id, platform="qq", sender="小鹿", role="peer",
            ts=semi._parse_iso("2026-09-28T21:00:00"), text="上一句")])

        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="我先睡了", role="peer"),
        ]))[0]
        assert cap.status == "committed", cap.problems
        row = store.list_messages(sc.state.chat_id, limit=10)[-1]
        assert row["text"] == "我先睡了"
        assert row["ts_source"] == "inferred", row["ts_source"]
        assert semi._parse_iso(row["ts"]) > semi._parse_iso("2026-09-28T21:00:00"), \
            f"推定时间没有排在锚点之后：{row['ts']}"
        # 采集时刻是另一个字段，单独留着 —— 「她何时说的」与「我何时抓的」不该互相顶替
        assert row["captured_at"], "采集时刻也要留下，两者是两件事"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_inferred_time_never_runs_into_the_future() -> None:
    """锚点已经很新（就是刚刚）时，推定时间不能越过采集时刻。

    批量复制十几条消息时最容易踩：`锚点 + 条数` 很容易冲到将来，
    于是聊天记录里出现「明天的消息」。这条断言就是钉住这个上限。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        store.upsert_chat(sc.state.chat_id, "qq", name="小鹿", peer_name="小鹿")
        now = datetime.now(timezone.utc)
        store.insert_messages([semi.Msg(
            chat_id=sc.state.chat_id, platform="qq", sender="小鹿", role="peer",
            ts=now, text="刚说的")])

        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text=f"连发第 {i} 条", role="peer") for i in range(5)
        ]))[0]
        rows = store.list_messages(sc.state.chat_id, limit=20)
        newest = max(semi._parse_iso(r["ts"]) for r in rows)
        assert newest <= now, f"推定时间跑到将来了：{newest} > {now}"
        assert all(r["ts_source"] == "inferred" for r in rows if r["text"].startswith("连发"))
        assert cap.written == 5, cap.problems
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ask_mode_holds_the_message_until_the_user_fills_the_time() -> None:
    """策略是「问我」时，没时间就挂起；用户填了才落库并标 `manual`。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store, missing_time="ask")
        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="到家了", role="peer"),
        ]))[0]
        assert cap.status == "pending"
        item = cap.items[0]
        assert item.needs_time and not item.needs_role
        assert not item.ts
        assert store.list_messages(sc.state.chat_id, limit=10) == []

        out = sc.commit(cap.id, {"items": {item.id: {"ts": "2026-09-28T22:10:00"}}})
        assert out is not None and out.status == "committed", out.problems
        row = store.list_messages(sc.state.chat_id, limit=10)[0]
        assert row["ts"].startswith("2026-09-28T22:10:00")
        assert row["ts_source"] == "manual"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_user_can_explicitly_say_use_the_capture_time() -> None:
    """「就用抓取时刻」也是一个决定：界面明确点了，才写成 assumed。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store, missing_time="ask")
        cap = _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="嗯", role="peer"),
        ]))[0]
        out = sc.commit(cap.id, {"items": {cap.items[0].id: {"use_capture_time": True}}})
        assert out is not None and out.status == "committed", out.problems
        row = store.list_messages(sc.state.chat_id, limit=10)[0]
        assert row["ts_source"] == "assumed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 防重


def test_repeat_copy_with_assumed_time_does_not_create_a_twin_row() -> None:
    """时间不可信时的防重：同一条消息复制两次只进一条。

    这里必须用**不同的** assumed 时间：库里的唯一键是
    (chat_id, sender, ts, text)，两次抓取的时间不同 → 唯一键不同 →
    会进两条。这道护栏只在时间不可信时启用。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)

        first = semi.Capture(id="c1", at=time.time(), items=[
            semi.PendingItem(id="a", text="我先睡了", ts="2026-09-28T23:00:00",
                             role="peer", ts_source="assumed")])
        second = semi.Capture(id="c2", at=time.time(), items=[
            semi.PendingItem(id="b", text="我先睡了", ts="2026-09-28T23:00:07",
                             role="peer", ts_source="assumed")])
        sc._commit_capture(first)
        cap2 = sc._commit_capture(second)

        assert first.written == 1
        assert cap2.written == 0 and cap2.duplicates == 1, (cap2.written, cap2.duplicates)
        assert _texts(store, sc.state.chat_id) == ["我先睡了"]
        assert sc.state.skipped_duplicate == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_trustworthy_time_is_not_deduped_by_text() -> None:
    """时间可信时不做文本去重：同一句话在不同时刻说两次是真的两条。

    护栏如果无差别启用，用户真的重复说一句「嗯」就会丢消息 ——
    丢消息比多一条难发现得多。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        sc._commit_capture(semi.Capture(id="c1", at=time.time(), items=[
            semi.PendingItem(id="a", text="嗯", ts="2026-09-28T23:10:00",
                             role="peer", ts_source="clipboard")]))
        sc._commit_capture(semi.Capture(id="c2", at=time.time(), items=[
            semi.PendingItem(id="b", text="嗯", ts="2026-09-28T23:40:00",
                             role="peer", ts_source="clipboard")]))
        assert _texts(store, sc.state.chat_id) == ["嗯", "嗯"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 人只能有一个


def test_semi_reuses_the_existing_person_instead_of_creating_a_twin() -> None:
    """库里已有「小鹿」→ 采到的记录归到它名下，不新建人、不另开会话。

    不复用的话用户采一次就多一个小鹿，而这两个小鹿的消息永远检索不到一起。
    同一个渠道上的会话也要复用：用户心里「我和小鹿的 QQ 聊天」是一件事，
    不该因为来源不同（导入 / 剪贴板）就被拆成两段。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        pid = store.create_person("小鹿", aliases=["梦梦"])
        store.upsert_chat("qq:haha", "qq", "小鹿", peer_name="小鹿",
                          me_name="我", channel="qq", source="import", person_id=pid)

        sc = semi.SemiCollector()
        sc._store = store
        resolved = sc._resolve_person_id(store, "", "小鹿")
        assert resolved == pid, "应该按别名/名字精确命中已有的人"

        sc = _started(semi, store, person_id=resolved)
        sc.state.chat_id = sc._resolve_chat_id(store, "qq", resolved, "小鹿")
        assert sc.state.chat_id == "qq:haha", "应复用同渠道已有会话，而不是另开一段"

        _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="睡了", ts="2026-09-28T23:30:00",
                  role="peer", ts_source="clipboard"),
        ]))
        persons = store.list_persons()
        assert [p.name for p in persons] == ["小鹿"], [p.name for p in persons]
        assert persons[0].message_count == 1
        assert store.get_chat("qq:haha").person_id == pid
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_brand_new_peer_still_gets_one_person_created() -> None:
    """第一次见的人要建得出来 —— 复用逻辑不能变成「找不到就不采」。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store, peer_name="小鹿")
        _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="你好", ts="2026-09-28T20:00:00",
                  role="peer", ts_source="clipboard"),
        ]))
        persons = store.list_persons()
        assert [p.name for p in persons] == ["小鹿"]
        assert persons[0].message_count == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 不丢、能轮询


def test_pending_items_survive_a_restart() -> None:
    """重启后待确认的还在 —— 它们是用户主动复制进来的，不能白干一遍。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, text="这句话到底是谁说的", ts="2026-09-28T21:30:00"),
        ]))
        assert len(sc.captures) == 1

        again = semi.SemiCollector()          # 模拟「重新打开了程序」
        assert len(again.captures) == 1
        assert again.captures[0].items[0].text == "这句话到底是谁说的"
        assert again.captures[0].status == "pending"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_committed_items_are_not_restored() -> None:
    """已经落库的不要再捞回来 —— 界面上会显示成一条「又来了」的假待办。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="好的", ts="2026-09-28T21:31:00",
                  role="peer", ts_source="clipboard"),
        ]))
        again = semi.SemiCollector()
        assert again.captures == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_poll_returns_only_what_arrived_after_the_given_id() -> None:
    """轮询用 id 定位而不是时间戳：同一毫秒内来两条，比时间会漏。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        got = _feed(sc, _FakeWatcher([]),
                    _cap(cb, [_item(cb, sender="小鹿", text="一", ts="2026-09-28T21:40:00",
                                    role="peer", ts_source="clipboard")]),
                    _cap(cb, [_item(cb, sender="小鹿", text="二", ts="2026-09-28T21:40:01",
                                    role="peer", ts_source="clipboard")]))
        first_id = got[0].id
        out = sc.poll(since=first_id)
        assert [c["id"] for c in out["captures"]] == [got[1].id]
        assert out["captures"][0]["items"][0]["text"] == "二"
        assert out["hint"], "轮询结果要带上「接下来该做什么」"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discard_and_clear_never_touch_pending() -> None:
    """清理只清已处理的，待确认的一条都不动。"""
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store)
        got = _feed(sc, _FakeWatcher([]),
                    _cap(cb, [_item(cb, sender="小鹿", text="ok", ts="2026-09-28T21:50:00",
                                    role="peer", ts_source="clipboard")]),
                    _cap(cb, [_item(cb, text="不知道谁说的", ts="2026-09-28T21:50:10")]))
        done, waiting = got
        sc.discard(done.id, "复制错了")
        assert done.status == "discarded"
        assert sc.clear() == 1
        assert [c.id for c in sc.captures] == [waiting.id]
        assert sc.snapshot()["pending"] == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ start/stop 真跑


def test_start_and_stop_really_wire_the_watcher_and_leave_nothing_behind() -> None:
    """真调 `start()`：观察器要接上、提示要给人、`stop()` 之后不能有残留。

    这条只在 Windows 上有意义（剪贴板 API 是 Win32 的）；
    其它平台跳掉，而不是假装通过。
    """
    from app.collect import winapi

    if not winapi.IS_WINDOWS:
        _skip("剪贴板观察器是 Win32 实现，非 Windows 环境跳过")

    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        pid = store.create_person("小鹿")
        sc = semi.get_semi()
        state = sc.start(store, client="qq", peer_name="小鹿", person_id=pid)
        try:
            assert state.active
            assert state.person_id == pid
            assert "小鹿" in state.poll_hint
            assert sc._watcher is not None
            assert sc._thread is not None and sc._thread.is_alive()
        finally:
            stopped = sc.stop()
        assert not stopped.active
        assert sc._thread is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_works_when_the_person_already_has_a_channel() -> None:
    """回归：这个人**已经绑好渠道**时，再点一次「开始监听」不能崩。

    踩过的坑（`'PersonChannel' object has no attribute 'peer_name'`）：
    `_resolve_names` 会遍历 `person_detail().channels` 去补全「认得出是谁」的名字，
    却读了这个模型上根本不存在的 `peer_name` / `me_name`。
    于是出现一条很反直觉的规律 ——
    **第一次创建（person 还不存在，守卫短路）不炸，第二次选中（person 已绑好 chat）必炸**。
    原来的用例要么绕过 `_resolve_names`（`_started` 直接赋值），要么用了没有 chat 的
    person（`channels == []`，循环体一次都不进），所以一直没抓到。

    这条用例走**真 `start()`**，前置条件正是那条崩过的路径：
    建 person → 把 chat 绑到它名下 → 再 start。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        pid = store.create_person("小鹿")
        store.upsert_chat("qq:haha", "qq", "小鹿", peer_name="小鹿", me_name="我",
                          channel="qq", source="collect", person_id=pid)

        sc = semi.get_semi()
        try:
            state = sc.start(store, client="qq", peer_name="小鹿", person_id=pid)
            assert state.active
            assert "小鹿" in sc._peer_names, sc._peer_names
        finally:
            sc.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_committing_a_capture_leaves_an_activity_trace() -> None:
    """落库成功要留一条 collect_semi 痕迹（需求 11「有动作就有痕迹」）。

    痕迹是对象详情「历史」Tab 的数据源；漏写不会报错，只会让「这个人的数据
    是怎么来的」永远是空的 —— 所以单列一条用例钉住。
    """
    tmp = _tmp_dir()
    try:
        store, cb, semi = _modules(tmp)
        sc = _started(semi, store, person_id="")
        _feed(sc, _FakeWatcher([]), _cap(cb, [
            _item(cb, sender="小鹿", text="留个痕", ts="2026-09-28T21:03:15",
                  role="peer", ts_source="clipboard"),
        ]))
        acts = store.list_activity(kind="collect_semi")
        assert [a.kind for a in acts] == ["collect_semi"], acts
        assert "1" in acts[0].summary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    skipped = failed = 0
    print("=" * 64)
    print(f"半自动采集守卫：{len(tests)} 项")
    print("=" * 64)
    for fn in tests:
        try:
            fn()
        except _SkipTest as exc:
            skipped += 1
            print(f"  - 跳过 {fn.__name__}：{exc}")
        except Exception:                       # noqa: BLE001
            failed += 1
            print(f"  x 失败 {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"  + 通过 {fn.__name__}")
    print("-" * 64)
    print(f"通过 {len(tests) - failed - skipped} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
