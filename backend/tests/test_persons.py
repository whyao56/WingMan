"""「以人为中心」的数据层与接口守卫。

这一层做的是**结构性改造**：把记忆的主键从「会话」换成「人」。
结构改造最容易出的事不是崩溃，而是**静默丢数据**，所以下面的用例
重点盯三件事：

1. **老库回填**：升级后已有的会话必须自动归到某个人名下，
   否则用户打开界面看到「人物列表是空的」＝ 以为数据没了。
2. **删除语义**：删掉一个人**不能**连带删掉聊天记录。
   消息是不可再生资产，删一个人不该顺手毁掉几万条记录 ——
   这条如果写反，用户点一下「删除人物」就永久损失数据，且没有任何提示。
3. **局部更新**：关系 / 期望关系 / 阶段目标是用户手写的核心内容，
   一次「只改名字」的保存绝不能把它们清空。
4. **归属不能迟到**：写入会话的**那一刻**就要有归属的人，不能等下一次
   启动回填 —— 用户导入完看不到人，会以为导入失败。

两种跑法都支持：

    cd backend
    python -m pytest tests/test_persons.py -q
    python tests/test_persons.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
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
    return Path(tempfile.mkdtemp(prefix="wingman_persons_"))


def _msg(chat_id: str, sender: str, role: str, ts: str, text: str, platform: str = "qq"):
    from app.schemas import Msg

    return Msg(chat_id=chat_id, platform=platform, sender=sender, role=role,
               ts=ts, text=text)


def _fresh_store(tmp: Path):
    from app.store import Store

    store = Store(tmp / "wingman.db")
    store.init()
    return store


def _person_named(store, name: str) -> str:
    """按名字取人的 id。

    刻意不走 `list_persons()[0]`：那个列表按 `updated_at DESC, name` 排，
    同一秒建的人谁在前取决于名字的字节序 —— 测试会因此时绿时红。
    """
    got = store.find_person_by_alias(name)
    assert got is not None, f"没有找到「{name}」：{[p.name for p in store.list_persons()]}"
    return got.id


def _merge_all(store) -> str:
    """把库里的所有「人」合并成一个，返回活下来的那个 id。

    「一个会话一个人」是回填和导入的规则，所以「把两个渠道归成同一个人」
    是用户的日常操作而不是异常路径 —— 这里把它显式写出来当测试的前置步骤。
    """
    persons = store.list_persons()
    assert len(persons) >= 2, f"这个场景需要至少两个人才能合并：{[p.name for p in persons]}"
    keep = persons[0].id
    store.merge_persons(keep, [p.id for p in persons[1:]])
    return keep


# ---------------------------------------------------------------- 迁移与回填


def test_init_backfills_a_person_for_every_existing_chat() -> None:
    """老库升级后，每个已有会话都要自动挂到一个「人」名下。

    不回填的话界面上「人物」是空的，用户的第一反应是「我的数据哪去了」——
    实际数据都在，只是没人把它们归到人下面。这类问题不报错，最难发现。
    """
    tmp = _tmp_dir()
    try:
        # 先造一个「老库」：只有 chats / messages，没有 persons
        store = _fresh_store(tmp)
        store.upsert_chat("qq-小鹿", "qq", "小鹿", peer_name="小鹿")
        store.upsert_chat("wx-阿哲", "wechat", "阿哲", peer_name="阿哲")
        store.insert_messages([
            _msg("qq-小鹿", "小鹿", "peer", "2026-01-01T10:00:00", "在吗"),
            _msg("wx-阿哲", "阿哲", "peer", "2026-01-02T10:00:00", "hello", "wechat"),
        ])
        # 模拟老库：把归属清掉，再跑一次 init
        with store.conn() as c:
            c.execute("UPDATE chats SET person_id = NULL")
            c.execute("DELETE FROM persons")

        store.init()

        persons = store.list_persons()
        assert len(persons) == 2, f"应为两个会话各回填一个人：{[(p.name, p.message_count) for p in persons]}"
        by_name = {p.name: p for p in persons}
        assert set(by_name) == {"小鹿", "阿哲"}
        assert by_name["小鹿"].message_count == 1
        assert by_name["阿哲"].message_count == 1
        # 每个会话都必须真的挂上，而不是建了人却忘了绑
        for info in store.list_chats():
            assert info.person_id, f"{info.id} 没有归属"
            assert info.person_id in {p.id for p in persons}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_init_is_idempotent_and_does_not_duplicate_persons() -> None:
    """反复 init 不能每次都造一批新人 —— 启动一次多一个人是灾难。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq-a", "qq", "A", peer_name="A")
        store.init()
        first = len(store.list_persons())
        store.init()
        store.init()
        assert len(store.list_persons()) == first == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_upsert_chat_gives_a_person_immediately_without_restart() -> None:
    """导入一份记录后，**不重启**就该在人物列表里看到它。

    这里踩过一次：建人的逻辑只挂在 `init()` 上，于是运行中导入的会话
    `person_id` 是空的 —— 数据都在，界面上却什么都没有，要重启才冒出来。
    用户看到的是「导入失败了」。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        assert store.list_persons() == []

        store.upsert_chat("wx-鹿", "wechat", "鹿鹿", peer_name="鹿鹿")

        persons = store.list_persons()
        assert len(persons) == 1, f"导入后应立刻有一个人：{[(p.name, p.channel_count) for p in persons]}"
        assert persons[0].name == "鹿鹿"
        assert store.get_chat("wx-鹿").person_id == persons[0].id

        # 再导入一次同一个会话（用户重复点导入）：不能变成两个人
        store.upsert_chat("wx-鹿", "wechat", "鹿鹿", peer_name="鹿鹿")
        assert len(store.list_persons()) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_upsert_chat_can_bind_to_an_existing_person_explicitly() -> None:
    """采集器先把对方认出来，再把会话挂过去 —— 不能每次都多一个同名的人。

    这条是给采集器留的入口：采集时先按昵称/备注找到已有的人（
    `find_person_by_alias`），把 id 传进来，归属就确定了；
    不传才会按「一个会话一个人」新建。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿", ["鹿鹿", "xiaolu"])

        # 采到微信侧的新会话，对方昵称是「鹿鹿」= 已知别名
        hit = store.find_person_by_alias("鹿鹿")
        assert hit is not None and hit.id == pid
        store.upsert_chat("wx-鹿", "wechat", "鹿鹿", peer_name="鹿鹿",
                          source="collect", person_id=pid)

        assert len(store.list_persons()) == 1, "显式传了归属就不该再新建人"
        info = store.get_chat("wx-鹿")
        assert info.person_id == pid
        assert info.source == "collect", "来路要能区分开，否则排查时分不清数据谁写的"

        # 显式归属也能用来纠错：把挂错的会话改挂到正确的人名下
        other = store.create_person("阿哲")
        store.upsert_chat("wx-鹿", "wechat", "鹿鹿", person_id=other)
        assert store.get_chat("wx-鹿").person_id == other
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chat_channel_is_filled_so_readers_do_not_see_null() -> None:
    """`chats.channel` 必须真的有值。

    这里踩过一次：读接口有一层「channel 为空就按 platform 推」的兜底，
    于是列表页看着一切正常，而直接读列的代码（按人取消息）拿到的是 None。
    同一个字段两处口径不同 = 下一颗雷，所以迁移里一次补齐。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq-a", "qq", "A")
        store.upsert_chat("wx-b", "wechat", "B")
        store.upsert_chat("misc-c", "generic", "C")
        # 模拟老库：列是空的
        with store.conn() as c:
            c.execute("UPDATE chats SET channel = NULL")
        store.init()
        channels = {c.id: c.channel for c in store.list_chats()}
        assert channels == {"qq-a": "qq", "wx-b": "wechat", "misc-c": "generic"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 跨渠道聚合


def test_person_stats_aggregate_across_channels() -> None:
    """同一个人在两个平台上的记录，条数与时间跨度要合并成一个口径。

    这正是「以人为中心」的意义：用户问的是「我和她聊了多少」，
    不是「这个会话有多少条」。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq-a", "qq", "小鹿", peer_name="小鹿")
        store.upsert_chat("wx-a", "wechat", "鹿鹿", peer_name="鹿鹿")
        store.insert_messages([
            _msg("qq-a", "小鹿", "peer", "2025-03-01T10:00:00", "QQ 上的一句"),
            _msg("qq-a", "我", "me", "2025-03-01T10:01:00", "QQ 回一句"),
            _msg("wx-a", "鹿鹿", "peer", "2026-08-01T10:00:00", "微信上的一句", "wechat"),
        ])
        # 合并成一个人
        pid = _merge_all(store)

        person = store.get_person(pid)
        assert person is not None
        assert person.channel_count == 2, person.channel_count
        assert person.message_count == 3
        assert person.peer_count == 2 and person.me_count == 1
        # 时间跨度必须跨两个渠道
        assert person.first_ts.startswith("2025-03-01")
        assert person.last_ts.startswith("2026-08-01")

        detail = store.person_detail(pid)
        assert detail is not None and len(detail.channels) == 2
        assert {c.channel for c in detail.channels} == {"qq", "wechat"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_messages_merges_channels_in_time_order() -> None:
    """按人取消息要按时间合并，并且每行带得出渠道。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq-a", "qq", "小鹿", peer_name="小鹿")
        store.upsert_chat("wx-a", "wechat", "鹿鹿", peer_name="鹿鹿")
        store.insert_messages([
            _msg("qq-a", "小鹿", "peer", "2026-01-01T10:00:00", "早"),
            _msg("wx-a", "鹿鹿", "peer", "2026-01-01T10:00:05", "在的", "wechat"),
            _msg("qq-a", "小鹿", "peer", "2026-01-01T10:00:09", "看这个"),
        ])
        pid = _merge_all(store)

        rows = store.person_messages(pid, limit=10)
        assert [r["text"] for r in rows] == ["早", "在的", "看这个"]
        assert [r["channel"] for r in rows] == ["qq", "wechat", "qq"]
        assert all(r["chat_name"] for r in rows), "每行都要能说出它来自哪段记录"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_channel_count_agrees_with_the_channel_list() -> None:
    """`channel_count` 必须等于 `person_detail().channels` 的长度。

    踩过一次：渠道数是「有消息的会话数」，而详情页列的是「挂着的会话数」——
    挂了一个还没导入记录的会话时，卡片写 1、点进去有 2 段入口。
    同一个概念两个数字，界面上就是自相矛盾。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿", ["鹿鹿"])
        store.upsert_chat("qq-a", "qq", "小鹿", peer_name="小鹿", person_id=pid)
        store.upsert_chat("wx-a", "wechat", "鹿鹿", peer_name="鹿鹿", person_id=pid)
        store.upsert_chat("call-a", "generic", "通话", person_id=pid, channel="call")
        store.insert_messages([
            _msg("qq-a", "小鹿", "peer", "2026-01-01T10:00:00", "只有这段有记录"),
        ])

        person = store.get_person(pid)
        detail = store.person_detail(pid)
        assert person.channel_count == 3, "刚挂上、还没记录的一段也要算进去"
        assert person.channel_count == len(detail.channels)
        assert person.message_count == 1, "条数只数消息，不受空会话影响"
        assert {c.channel for c in detail.channels} == {"qq", "wechat", "call"}
        # 通话这种没有导出文件的渠道，得能只靠手工挂上去
        assert store.get_chat("call-a").channel == "call"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 危险操作语义


def test_delete_person_releases_chats_and_keeps_all_messages() -> None:
    """删掉一个人，**绝不能**连带删掉聊天记录。

    这是本文件最重要的一条。删人物是个「管理动作」，用户的心理预期是
    「这个人从我的列表里消失」；如果实现成级联删除，一次点击就永久
    损失几万条消息 —— 而且界面不会告诉他。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq-a", "qq", "小鹿", peer_name="小鹿")
        store.insert_messages([
            _msg("qq-a", "小鹿", "peer", "2026-01-01T10:00:00", "别删我"),
            _msg("qq-a", "我", "me", "2026-01-01T10:01:00", "不会的"),
        ])
        pid = _person_named(store, "小鹿")
        assert store.get_person(pid).message_count == 2

        store.delete_person(pid)

        assert store.get_person(pid) is None
        chats = store.list_chats()
        assert len(chats) == 1, "会话不该被删"
        assert chats[0].person_id == "", "会话应回到未归属状态，而不是消失"
        assert chats[0].message_count == 2, "消息必须一条不少地留着"
        assert store.get_chat("qq-a").message_count == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_person_is_partial_and_never_clears_unrelated_fields() -> None:
    """只改名字不能把关系 / 期望关系 / 阶段目标清空。

    这三项是用户一个字一个字敲进去的，丢了没人能帮他恢复 ——
    所以更新实现必须是逐字段合并，而不是整行覆盖。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿", ["鹿鹿"])
        store.update_person(pid, {
            "relation": "大学同学，毕业后偶尔联系",
            "desired_relation": "希望能稳定地做朋友，而不是有事才想起对方",
            "stage_goal": "这周先自然地把展约上",
        })

        store.update_person(pid, {"name": "小鹿（新备注）"})

        p = store.get_person(pid)
        assert p.name == "小鹿（新备注）"
        assert p.relation == "大学同学，毕业后偶尔联系"
        assert p.desired_relation == "希望能稳定地做朋友，而不是有事才想起对方"
        assert p.stage_goal == "这周先自然地把展约上"
        assert p.aliases == ["鹿鹿"], "别名不该被顺手清掉"

        # 空字符串是有意义的「清空」意图，但只能是显式传空
        store.update_person(pid, {"stage_goal": ""})
        assert store.get_person(pid).stage_goal == ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_merge_persons_is_conservative_about_handwritten_fields() -> None:
    """合并时手写字段以保留的那个为准，不猜、不覆盖。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        keep = store.create_person("小鹿")
        drop = store.create_person("鹿鹿")
        store.update_person(keep, {"relation": "同事"})
        store.update_person(drop, {"relation": "网友", "stage_goal": "先约出来喝咖啡"})

        merged = store.merge_persons(keep, [drop])

        assert merged.relation == "同事", "保留方的值优先"
        assert merged.stage_goal == "先约出来喝咖啡", "保留方为空时用另一方的补齐"
        assert "鹿鹿" in merged.aliases, "被合并方的名字要进别名，否则以后认不出"
        assert store.get_person(drop) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_find_person_by_alias_matches_exactly_only() -> None:
    """按别名找人只做精确匹配。

    模糊匹配会把「小明」和「小明明」合成一个人 —— 而这种错误是静默的，
    用户不会发现两个人的记忆被混在了一起。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿", ["鹿鹿", "xiaolu"])
        assert store.find_person_by_alias("小鹿").id == pid
        assert store.find_person_by_alias("鹿鹿").id == pid
        assert store.find_person_by_alias("XIAOLU").id == pid, "别名匹配应忽略大小写"
        assert store.find_person_by_alias("  小鹿  ").id == pid, "首尾空白应被忽略"
        assert store.find_person_by_alias("小鹿鹿") is None, "不该模糊命中"
        assert store.find_person_by_alias("") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 采集游标


def test_cursor_roundtrip_then_reset_forces_full_recollect() -> None:
    """游标能存能读；重置后必须「查不到」= 下次全量重采。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        assert store.get_cursor("wechat", "wxid_me", "peer1") is None

        store.save_cursor(
            "wechat", "wxid_me", "peer1",
            chat_id="wx-1", last_ts="2026-05-01T09:00:00", last_ext_id="m-9001",
            fingerprint="fp-abc", merged_count=120, collected_from="2026-01-01",
            status="ok",
        )
        cur = store.get_cursor("wechat", "wxid_me", "peer1")
        assert cur is not None
        assert cur.last_ts == "2026-05-01T09:00:00"
        assert cur.last_ext_id == "m-9001"
        assert cur.merged_count == 120
        assert cur.collected_from == "2026-01-01"
        assert cur.status == "ok"
        assert cur.last_run_at, "必须记下这次跑的时间，否则不知道进度是不是陈的"

        # 再存一次只带状态：不能把已有进度冲掉
        store.save_cursor("wechat", "wxid_me", "peer1", status="skipped", message="无新消息")
        cur2 = store.get_cursor("wechat", "wxid_me", "peer1")
        assert cur2.status == "skipped"
        assert cur2.last_ts == "2026-05-01T09:00:00", "只报状态时不能丢掉采集进度"
        assert cur2.merged_count == 120

        store.reset_cursor("wechat", "wxid_me", "peer1")
        assert store.get_cursor("wechat", "wxid_me", "peer1") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fingerprint_changes_only_when_content_changes() -> None:
    """内容指纹：没变就不该变，变了必须变。

    它是「无变动就不重采」的唯一依据 —— 指纹算错了，要么每次白跑一遍全量，
    要么新消息被当成旧的跳过。两种都不会报错。
    """
    from app.store import _fingerprint_of

    base = [
        {"ext_id": "1", "ts": "2026-01-01T10:00:00", "text": "一"},
        {"ext_id": "2", "ts": "2026-01-01T10:00:05", "text": "二"},
    ]
    same = [dict(base[0]), dict(base[1])]
    assert _fingerprint_of(base) == _fingerprint_of(same)

    # 追加一条 → 必须变
    grown = base + [{"ext_id": "3", "ts": "2026-01-01T10:00:09", "text": "三"}]
    assert _fingerprint_of(grown) != _fingerprint_of(base)

    # 条数不变但最后一条被编辑了 → 也要变
    edited = [dict(base[0]), {**base[1], "text": "二（改过）"}]
    assert _fingerprint_of(edited) != _fingerprint_of(base)

    # 空列表要有确定的指纹，不能抛异常
    assert _fingerprint_of([]) == "empty"


def test_cursors_are_deleted_with_their_person() -> None:
    """删人时游标一起清 —— 游标指向不存在的人就是悬空引用。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿")
        store.save_cursor("qq", "", "peer9", person_id=pid, last_ts="2026-01-01T00:00:00")
        assert len(store.list_cursors(person_id=pid)) == 1
        store.delete_person(pid)
        assert store.list_cursors() == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- HTTP 接口


def _client_with_temp_data_dir():
    """把 DATA_DIR 指到临时目录再建 TestClient，绝不碰真实数据库。"""
    from app import config

    tmp = _tmp_dir()
    config.DATA_DIR = tmp
    config.get_settings.cache_clear()

    import app.context as context_module

    context_module.DATA_DIR = tmp
    context_module._CTX = None

    from fastapi.testclient import TestClient

    from app.api import routes_health
    from app.main import app

    routes_health.DATA_DIR = tmp
    return TestClient(app), tmp


def test_persons_api_crud_and_channel_binding() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            assert client.get("/api/persons").json() == []

            created = client.post("/api/persons", json={
                "name": "小鹿",
                "relation": "大学同学",
                "desired_relation": "做长期的朋友",
                "stage_goal": "把展约上",
            })
            assert created.status_code == 200, created.text
            person = created.json()
            pid = person["id"]
            assert person["relation"] == "大学同学"

            # 重名要拦，避免两个「小鹿」并存导致合并时靠猜
            dup = client.post("/api/persons", json={"name": "小鹿"})
            assert dup.status_code == 409
            forced = client.post("/api/persons", json={"name": "小鹿", "force": True})
            assert forced.status_code == 200

            # 空名字要拒绝
            assert client.post("/api/persons", json={"name": "  "}).status_code == 422

            # 局部更新不能清掉手写字段
            patched = client.patch(f"/api/persons/{pid}", json={"name": "小鹿（备注）"})
            assert patched.status_code == 200
            body = patched.json()
            assert body["name"] == "小鹿（备注）"
            assert body["stage_goal"] == "把展约上"

            # 造一个会话并挂到这个人的名下
            import app.context as context_module

            store = context_module.get_ctx().store
            store.upsert_chat("qq-x", "qq", "文档示例", peer_name="小鹿")
            bound = client.post(f"/api/persons/{pid}/channels", json={"chat_id": "qq-x"})
            assert bound.status_code == 200, bound.text

            detail = client.get(f"/api/persons/{pid}").json()
            assert [c["chat_id"] for c in detail["channels"]] == ["qq-x"]
            assert detail["channels"][0]["channel"] == "qq"

            # 时间线接口可用
            msgs = client.get(f"/api/persons/{pid}/messages").json()
            assert msgs["person_id"] == pid and msgs["messages"] == []

            # 解除归属：会话留着
            assert client.delete(f"/api/persons/{pid}/channels/qq-x").status_code == 200
            assert client.get(f"/api/persons/{pid}").json()["channels"] == []
            assert store.get_chat("qq-x") is not None

            # 删人：会话不删
            client.post(f"/api/persons/{pid}/channels", json={"chat_id": "qq-x"})
            removed = client.delete(f"/api/persons/{pid}")
            assert removed.status_code == 200
            assert removed.json()["released_chats"] == 1
            assert client.get(f"/api/persons/{pid}").status_code == 404
            assert store.get_chat("qq-x") is not None, "删人之后会话必须还在"

            # 不存在的对象返回 404 而不是 500
            assert client.get("/api/persons/p_nope").status_code == 404
            assert client.patch("/api/persons/p_nope", json={"name": "x"}).status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_merge_api_requires_targets_and_rejects_self_merge() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            a = client.post("/api/persons", json={"name": "甲"}).json()["id"]
            b = client.post("/api/persons", json={"name": "乙"}).json()["id"]
            assert client.post(f"/api/persons/{a}/merge", json={}).status_code == 422
            assert client.post(f"/api/persons/{a}/merge", json={"merge_ids": [a]}).status_code == 422
            ok = client.post(f"/api/persons/{a}/merge", json={"merge_ids": [b]})
            assert ok.status_code == 200, ok.text
            assert "乙" in ok.json()["aliases"]
            assert client.get(f"/api/persons/{b}").status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reset_cursor_api_requires_identity() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            # httpx 的 TestClient.delete() 不接受 json= 关键字，只能走 request()。
            # 这里不能用 client.request("DELETE", url, json={...}) 之外的花招，
            # 因为「用不用请求体传身份」本身就是要验的行为。
            missing = client.request("DELETE", "/api/collect/cursors", json={"platform": "qq"})
            assert missing.status_code == 422, missing.text
            ok = client.request(
                "DELETE", "/api/collect/cursors",
                json={"platform": "qq", "peer_key": "p1"},
            )
            assert ok.status_code == 200, ok.text
            assert client.get("/api/collect/cursors").json() == {"cursors": []}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [(name, obj) for name, obj in list(globals().items())
            if name.startswith("test_") and callable(obj)]


def main() -> int:
    print("=" * 68, flush=True)
    print("以人为中心：数据层与接口回归（临时目录，不碰真实数据库）", flush=True)
    print("=" * 68, flush=True)
    failures: list[tuple[str, str]] = []
    skipped = 0
    tests = _collect_tests()
    for index, (name, function) in enumerate(tests, start=1):
        try:
            function()
        except _SkipTest as exc:
            skipped += 1
            print(f"[{index:>2}/{len(tests)}] SKIP {name} —— {exc}", flush=True)
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"[{index:>2}/{len(tests)}] FAIL {name} —— {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"[{index:>2}/{len(tests)}] OK   {name}", flush=True)
    print("-" * 68, flush=True)
    print(f"通过 {len(tests) - len(failures) - skipped} / 跳过 {skipped} / 失败 {len(failures)}", flush=True)
    if failures:
        for name, error in failures:
            print(f"  FAILED {name}: {error}", flush=True)
        print("结果：失败", flush=True)
        return 1
    print("结果：全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
