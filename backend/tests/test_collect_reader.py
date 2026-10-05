"""读库与消息解析的守卫。

## 为什么用「合成库」测

真库是加过密的，本机拿不到密钥（`keys.py` 里有实测结论）。所以「解析器对不对」
这件事不能靠真库来证 —— 只能反过来做：**按两家的表结构造一个假库，
把消息写进去，再读出来，逐字段比对**。

这跟密码学模块的自检是同一个套路：既然拿不到真数据，就造一份
「已知答案」的数据，让解析器在可验证的输入上跑。造不出往返一致的证据，
就只能靠「我觉得这段代码对」——那不算验证。

## 两份假库的取材

- **微信 4.x 形状**：消息表的说话人存的是**整数**（指向 `Name2Id` 的 rowid），
  正文是文本，时间字段是英文名。这条链路上最容易错的就是「忘了过 Name2Id
  这一跳」，于是所有消息的说话人都变成一个数字。
- **QQ NT 形状**：列名是**数字**（`40033` 这种），正文是一段 JSON 元素，
  时间在整数列里。这里最容易错的是「时间列认成了消息 ID」——
  认错的表现是时间全部变成 1970 年或者 2001 年的某一天，而且还挺像真的。

## 还要钉住的「不许发生」

- 认不出时间列的表**不能**被当成消息表（宁可少采，不可乱采）；
- 认不出说话人时**默认跳过并计数**，不能默认算成对方说的；
- 从二进制里「捞」正文默认**关闭**（捞出来的必然是片段）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:  # pytest 可选：没有 pytest 时用本文件的 main() 直跑
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

from app.collect import reader  # noqa: E402


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


ME_WXID = "wxid_me000000000000"
PEER_WXID = "wxid_xiaolu00000000"
ME_UID = "u_meUidAAA"
PEER_UID = "u_peerUidBBB"


def _ts(y, mo, d, h=12, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------- 假库：微信 4.x 形状


def _make_wechat_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    # Name2Id：单列 + rowid 当 id。微信就是这么存的。
    conn.execute("CREATE TABLE Name2Id (user_name TEXT UNIQUE)")
    conn.executemany("INSERT INTO Name2Id (user_name) VALUES (?)",
                     [(ME_WXID,), (PEER_WXID,)])
    # Contact：备注名才是用户认得的名字
    conn.execute("CREATE TABLE Contact (username TEXT, local_type INTEGER,"
                 " alias TEXT, remark TEXT, nick_name TEXT)")
    conn.executemany("INSERT INTO Contact VALUES (?,?,?,?,?)", [
        (ME_WXID, 1, "me_alias", "", "我自己"),
        (PEER_WXID, 1, "", "小鹿", "鹿鹿"),
    ])
    # 消息表：说话人是整数（Name2Id 的 rowid），正文是文本
    conn.execute("""
        CREATE TABLE Msg_aabbcc (
            local_id INTEGER PRIMARY KEY,
            server_id INTEGER,
            local_type INTEGER,
            create_time INTEGER,
            real_sender_id INTEGER,
            message_content TEXT,
            compress_content TEXT
        )
    """)
    rows = [
        (1, 900001, 1, _ts(2026, 9, 28, 21, 3, 15), 2, "今天有点累，不想加班了", None),
        (2, 900002, 1, _ts(2026, 9, 28, 21, 4, 2), 1, "那就早点回去，明天再说", None),
        (3, 900003, 1, _ts(2026, 9, 28, 21, 5, 40), 2, "嗯，你呢？", None),
        (4, 900004, 3, _ts(2026, 9, 28, 21, 6, 0), 2, "", None),          # 图片，没正文
    ]
    conn.executemany("INSERT INTO Msg_aabbcc VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 假库：QQ NT 形状


def _qq_text(*parts: str) -> str:
    """QQ 的消息元素：一段 JSON 数组，正文在 `textElement.content` 里。"""
    return json.dumps([{"elementType": 1, "textElement": {"content": p}} for p in parts],
                      ensure_ascii=False)


def _make_qq_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE c2c_msg_table (
            "40001" INTEGER PRIMARY KEY,
            "40011" TEXT,
            "40027" TEXT,
            "40033" INTEGER,
            "40050" TEXT,
            "40090" TEXT
        )
    """)
    rows = [
        (7001, PEER_UID, PEER_UID, _ts(2026, 9, 28, 21, 3, 15), _qq_text("今天有点累，不想加班了"), ""),
        (7002, ME_UID, PEER_UID, _ts(2026, 9, 28, 21, 4, 2), _qq_text("那就早点回去，明天再说"), ""),
        (7003, PEER_UID, PEER_UID, _ts(2026, 9, 28, 21, 5, 40), _qq_text("嗯，", "你呢？"), ""),
    ]
    conn.executemany('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?)', rows)
    # 一张「有时间没正文」的表：不许被当成消息表
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT,"
                 " updated_at INTEGER)")
    conn.execute("INSERT INTO settings VALUES (1, 'theme', 'dark', ?)",
                 (_ts(2026, 9, 1),))
    conn.commit()
    conn.close()


def _with_db(builder) -> tuple[sqlite3.Connection, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="wingman_reader_"))
    path = tmp / "msg.db"
    builder(path)
    return sqlite3.connect(str(path)), path


# ================================================================ 用例


def test_wechat_shape_resolves_integer_sender_through_name2id():
    """微信：整数说话人要过 Name2Id 那一跳，不能直接当人名用。"""
    conn, _ = _with_db(_make_wechat_db)
    try:
        maps, skipped = reader.pick_message_tables(conn, me_ids=[ME_WXID])
        assert maps, f"应认出消息表，跳过的是 {skipped}"
        m = maps[0]
        assert m.table == "Msg_aabbcc"

        names = reader.build_name_index(conn, [ME_WXID])
        res = reader.read_messages(conn, m, name_index=names, me_ids=[ME_WXID])

        assert [x.role for x in res.messages] == ["peer", "me", "peer"], \
            f"role 不对：{[x.role for x in res.messages]}"
        # 说话人不该是 "2" 这种 rowid，而该是 wxid / 备注名
        assert res.messages[0].sender_id == PEER_WXID, res.messages[0].sender_id
        assert res.messages[0].sender_name == "小鹿", res.messages[0].sender_name
        assert res.messages[1].sender_name == "我", res.messages[1].sender_name
        assert res.messages[0].text == "今天有点累，不想加班了"
        # 图片那条没有正文，应该被跳过并计数，而不是留一条空的
        assert res.skipped_empty == 1, res.skipped_empty
        assert len(res.messages) == 3
        assert res.messages[0].ts == datetime(2026, 9, 28, 21, 3, 15, tzinfo=timezone.utc)
    finally:
        conn.close()


def test_wechat_never_treats_message_id_as_time():
    """时间列不能认成消息 ID：认错会让整条时间轴变成 2001 年。"""
    conn, _ = _with_db(_make_wechat_db)
    try:
        maps, _ = reader.pick_message_tables(conn, me_ids=[ME_WXID])
        m = maps[0]
        assert m.ts == "create_time", m.ts
        assert m.ts_unit == "s"
        assert m.text == "message_content", m.text
        assert m.ext_id in ("local_id", "server_id"), m.ext_id
    finally:
        conn.close()


def test_qq_shape_reads_json_elements_and_numeric_columns():
    """QQ：列名是数字，正文是 JSON 元素，时间在整数列里。"""
    conn, _ = _with_db(_make_qq_db)
    try:
        maps, skipped = reader.pick_message_tables(conn, me_ids=[ME_UID])
        assert maps, f"应认出 c2c_msg_table，跳过的是 {skipped}"
        m = maps[0]
        assert m.table == "c2c_msg_table"
        assert m.ts == "40033", m.ts
        assert m.text == "40050", m.text
        assert m.text_kind == "json", m.text_kind

        res = reader.read_messages(conn, m, me_ids=[ME_UID])
        assert len(res.messages) == 3, res.summary()
        assert [x.role for x in res.messages] == ["peer", "me", "peer"]
        assert res.messages[0].text == "今天有点累，不想加班了"
        # 同一行里有多个文本元素时要按顺序拼起来
        assert res.messages[2].text == "嗯，\n你呢？", repr(res.messages[2].text)
        assert res.messages[0].ext_id == "7001"
    finally:
        conn.close()


def test_qq_without_me_id_refuses_to_guess_the_speaker():
    """不知道「我」的 id 时：**不猜**，如实报「认不出说话人」。

    为什么这里不做「剩下的都算对方」这种聪明的兜底：QQ 的库里只有 `u_xxx`
    这样的 uid，没有任何一处写着「哪个 uid 是我」。没有这个锚点，
    「这句是谁说的」就没有依据 —— 硬猜的后果是所有消息的归属都可能反过来，
    而用户看不出来。所以正确行为是拒绝并把话说清楚。
    """
    conn, _ = _with_db(_make_qq_db)
    try:
        maps, _ = reader.pick_message_tables(conn, me_ids=[])   # 故意不给登录账号
        m = maps[0]
        assert m.needs_sender, "没有依据时必须标成「说话人未认出」"
        assert m.problems, "拒绝要有理由，不能默默什么都不做"
        assert any("说话人" in p for p in m.problems), m.problems

        res = reader.read_messages(conn, m, me_ids=[])
        assert res.messages == [], res.summary()
        assert res.skipped_unknown_role == 3
    finally:
        conn.close()


def test_qq_with_a_known_me_id_attributes_both_sides():
    """一旦知道「我」的 uid，两侧的归属就都能定下来。"""
    conn, _ = _with_db(_make_qq_db)
    try:
        maps, _ = reader.pick_message_tables(conn, me_ids=[ME_UID])
        m = maps[0]
        assert m.sender == "40011", m.sender
        assert m.peer == "40027", m.peer      # 会话对象列，值域被说话人列包住
        res = reader.read_messages(conn, m, me_ids=[ME_UID])
        assert [x.role for x in res.messages] == ["peer", "me", "peer"]
    finally:
        conn.close()


def test_assume_peer_switch_is_off_by_default_and_works_when_on():
    """「剩下的都算对方说的」是个开关，默认关着，用户可以自己打开。

    这个开关的实用场景：联系人在库里（所以说话人列认得出来），
    但用户还没告诉程序「哪个 wxid 是我」。这时每条消息都判不出归属，
    用户可以说一句「对，里面没有我，剩下都是对方」——
    但**必须由人来说**。默认打开就等于把我的话全算到对方头上。
    """
    conn, _ = _with_db(_make_wechat_db)
    try:
        maps, _ = reader.pick_message_tables(conn, me_ids=[])   # 没告诉程序我是谁
        m = maps[0]
        assert m.sender == "real_sender_id", m.sender
        assert not m.me_confirmed, "没给登录账号时不该自称确认了「我」"

        off = reader.read_messages(conn, m, me_ids=[])
        assert off.messages == [], off.summary()
        assert off.skipped_unknown_role == 3

        on = reader.read_messages(conn, m, me_ids=[],
                                  assume_peer_when_unknown=True)
        assert len(on.messages) == 3, on.summary()
        assert on.skipped_unknown_role == 0
        assert {x.role for x in on.messages} == {"peer"}
    finally:
        conn.close()


def test_table_without_a_time_column_is_not_a_message_table():
    """认不出时间列的表不是消息表 —— 宁可少采，不可乱采。"""
    conn, _ = _with_db(_make_qq_db)
    try:
        maps, skipped = reader.pick_message_tables(conn, me_ids=[ME_UID])
        assert all(m.table != "settings" for m in maps)
        assert any(t == "settings" for t, _ in skipped), skipped
    finally:
        conn.close()


def test_millisecond_timestamps_are_detected():
    """毫秒时间戳要认出来 —— 新版客户端有改用毫秒的。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE msg (id INTEGER PRIMARY KEY, t INTEGER, body TEXT, who TEXT)")
    for i in range(12):
        conn.execute("INSERT INTO msg VALUES (?,?,?,?)",
                     (i, _ts(2026, 5, 1, 10, i) * 1000, f"第 {i} 句话", "u_a" if i % 2 else "u_b"))
    m = reader.infer_mapping(conn, "msg", me_ids=["u_a"])
    assert m.ts == "t" and m.ts_unit == "ms", (m.ts, m.ts_unit)
    res = reader.read_messages(conn, m, me_ids=["u_a"])
    assert res.messages[0].ts.year == 2026
    assert {x.role for x in res.messages} == {"me", "peer"}


def test_carving_is_off_by_default():
    """二进制兜底默认关闭：捞出来的必然只有片段。

    这里的假数据做成 **protobuf 字符串字段**的样子（`0a 12` + UTF-8 正文），
    因为真实的二进制正文就长这样：protobuf 的 string 字段按 UTF-8 存。
    """
    blob = b"\x0a\x12" + "你好我是小鹿".encode("utf-8") + b"\x10\x01"
    got_off, kind_off = reader.extract_text(blob)
    got_on, kind_on = reader.extract_text(blob, allow_carve=True)
    assert got_off == "", f"默认不该从二进制里捞东西：{got_off!r}"
    assert kind_on == "carved", kind_on
    assert "小鹿" in got_on, repr(got_on)


def test_carving_does_not_invent_text_from_random_bytes():
    """随机字节里不该捞出「像人话」的东西 —— 捞垃圾比捞不到更糟。"""
    blob = bytes(range(256)) * 8
    got, _ = reader.extract_text(blob, allow_carve=True)
    assert len(got) < 20, repr(got[:120])


def test_time_column_must_be_in_the_right_era():
    """值域判据要真的在起作用：2026 年的时间不该被认成 1970 年。"""
    dt, unit = reader.ts_from_number(_ts(2026, 1, 1))
    assert unit == "s" and dt.year == 2026
    dt, unit = reader.ts_from_number(12345)
    assert dt is None and unit == "", "五位整数不可能是 2000 年后的时间戳"


def test_probe_reports_what_it_saw():
    """探测报告要能被人看懂：认出了什么、凭什么、认不出的候选有哪些。"""
    conn, path = _with_db(_make_qq_db)
    conn.close()
    rep = reader.probe(path, me_ids=[ME_UID])
    assert rep["ok"] is True
    assert rep["message_tables"], rep
    top = rep["message_tables"][0]
    assert top["table"] == "c2c_msg_table"
    assert top["evidence"].get("ts"), "结论必须带判据"
    assert any(c["column"] == "40033" for c in top["candidates"]["ts"])


def _main() -> int:
    import traceback

    fails: list[str] = []
    items = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in items:
        try:
            fn()
        except _SkipTest as exc:                       # pragma: no cover
            print(f"SKIP {name}: {exc}")
        except Exception:
            fails.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"OK   {name}")
    print(f"\n{len(items) - len(fails)}/{len(items)} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
