"""剪贴板解析守卫 —— 半自动采集的**入口**，它错了后面全错。

## 为什么这块必须钉死

半自动采集不需要密钥、不依赖客户端版本，实测下来是当下唯一确定能跑通的通道。
但它有一个必须靠解析器兜住的环节：**复制出来的东西是一坨文本**。
客户端不会告诉你「这是谁说的、什么时候说的、哪几行属于同一条」。

解析器的两条底线：

1. **认得出就是认得出，认不出就说认不出。** 认不出说话人时不猜 ——
   猜错是静默的，「老王的话记成小鹿说的」之后再怎么分析都是错的。
2. **不许从正文里偷东西。** 正文里出现的日期、冒号、名字都不是元数据。
   `我 今天 2026-09-28 21:03:15 那会儿还在加班` 是一句话，
   不是「我」在 21:03:15 说的。

## 关于「块头只出现一次」

真实的多选复制里每条消息前面都有自己的「称呼 + 时间」头（见下面的用例 1）。
但如果只出现了一次头，其余行会**并进同一条消息** —— 那是「这段是小鹿说的」
这个判断唯一说得通的读法。这个行为是有意的，用例 `test_..._folds_...` 把它写明。

跑法：

    cd backend
    python -m pytest tests/test_collect_clipboard.py -q
    python tests/test_collect_clipboard.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.collect import clipboard as cb  # noqa: E402

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


ME = ["我"]
PEER = ["小鹿"]


def _parse(text: str, *, me=ME, peer=PEER) -> cb.ParseResult:
    return cb.parse_copied(text, me_names=list(me), peer_names=list(peer))


def _roles(res: cb.ParseResult) -> list[str]:
    return [it.role for it in res.items]


# ================================================================ QQ 主形态


def test_qq_multi_select_shape_gets_sender_and_time_per_message() -> None:
    """QQ NT 多选复制：每条消息前面一个「称呼 + 空格 + 时间」，正文在下一行。

    这是半自动采集最主要的输入形态。两个关键点：说话人要**逐条**判对
    （一次复制里通常既有对方说的也有我说的），时间要真的取到而不是猜。
    """
    res = _parse(
        "小鹿 2026-09-28 21:03:15\n今天有点累，不想加班了\n\n"
        "我 2026-09-28 21:04:02\n那就早点回去，明天再说\n\n"
        "小鹿 2026-09-28 21:05:40\n嗯，你呢？"
    )
    assert res.shape == "structured", res.shape
    assert len(res.items) == 3
    assert not res.needs_sender
    assert _roles(res) == ["peer", "me", "peer"], _roles(res)
    assert [i.ts_source for i in res.items] == ["clipboard"] * 3
    assert res.items[0].ts == "2026-09-28T21:03:15"
    assert res.items[2].ts == "2026-09-28T21:05:40"
    assert res.items[0].text == "今天有点累，不想加班了", repr(res.items[0].text)
    assert "21:03" not in res.items[0].text, "时间戳不能留在正文里"


def test_colon_shape_gets_the_sender_but_never_invents_a_time() -> None:
    """「称呼: 正文」能判出说话人；没带时间就空着，不编。"""
    res = _parse("小鹿: 在吗\n我: 在的\n小鹿: 帮我看看这个")
    assert res.shape == "structured"
    assert len(res.items) == 3
    assert _roles(res) == ["peer", "me", "peer"], _roles(res)
    assert all(not i.ts for i in res.items), [i.ts for i in res.items]
    assert all(not i.ts_source for i in res.items)


def test_sender_time_and_text_on_one_line_keeps_the_text() -> None:
    """「称呼 + 时间 + 同行正文」：三段都要各就各位。"""
    res = _parse("小鹿 2026-09-28 21:03:15 在吗\n我 2026-09-28 21:04:02 在的")
    assert res.shape == "structured"
    assert len(res.items) == 2
    assert res.items[0].text == "在吗", repr(res.items[0].text)
    assert res.items[0].ts == "2026-09-28T21:03:15"


# ================================================================ 相对时间（需求 3）
#
# 客户端复制出来的时间**大量是相对的**：「小鹿 21:03」「小鹿 昨天 21:03」
# 「小鹿 下午 3:30」。这些就是消息自己显示的时刻 —— 也就是要记的「回复时刻」。
# 只认绝对日期的话，它们全落进「剪贴板没带时间」，最后被顶替成「按下 Ctrl+C
# 的那一刻」，而那是采集时刻。下面的用例把每种相对写法钉住。


def _rel(text: str, now: str = "2026-10-06T23:45:00") -> object:
    """用固定的「现在」解析，否则用例会随运行日期漂移。

    「现在」选在当天 23:45 —— 比下面所有用例里的钟点都晚，
    这样它们默认落在「今天」；专门验「回退一天」的那条会自己改这个值。
    """
    from datetime import datetime as _dt

    return cb.parse_copied(text, me_names=list(ME), peer_names=list(PEER),
                           now=_dt.fromisoformat(now))


def test_bare_clock_after_the_name_is_the_message_time() -> None:
    """「小鹿 21:03」—— 钟点是消息自己的时刻，日期补成今天。"""
    res = _rel("小鹿 21:03\n在吗\n\n我 21:04\n在的")
    assert res.shape == "structured", res.shape
    assert [i.ts for i in res.items] == ["2026-10-06T21:03:00", "2026-10-06T21:04:00"]
    assert [i.ts_source for i in res.items] == ["relative"] * 2
    assert "21:03" not in res.items[0].text


def test_relative_day_words_resolve_to_the_right_date() -> None:
    """昨天 / 前天 / 周几 都要落到具体日期。"""
    assert _rel("小鹿 昨天 21:03\n在吗").items[0].ts == "2026-10-05T21:03:00"
    assert _rel("小鹿 前天 08:30\n在吗").items[0].ts == "2026-10-04T08:30:00"
    # 2026-10-06 是周二，「周三」= 上一个周三（09-30）
    assert _rel("小鹿 周三 20:15\n在吗").items[0].ts == "2026-09-30T20:15:00"


def test_afternoon_words_are_turned_into_24h() -> None:
    """「下午 3:30」是 15:30，不是 03:30 —— 差 12 小时的错最伤人。"""
    assert _rel("小鹿 下午 3:30\n在吗").items[0].ts == "2026-10-06T15:30:00"
    assert _rel("小鹿 上午 9:05\n在吗").items[0].ts == "2026-10-06T09:05:00"
    assert _rel("小鹿 晚上 11:40\n在吗").items[0].ts == "2026-10-06T23:40:00"


def test_a_clock_time_that_would_be_in_the_future_rolls_back_a_day() -> None:
    """复制到「还没发生的钟点」时，它属于昨天，不是今天。

    消息不可能来自未来。这条规则同时管住两件事：刚过午夜时复制昨天的
    晚间消息（下面这条），以及「今天/昨天」的边界不会因为跑得久了而漂。
    """
    assert _rel("小鹿 23:50\n在吗", now="2026-10-06T00:05:00").items[0].ts \
        == "2026-10-05T23:50:00"
    # 同一个钟点，在它**已经发生**之后复制 → 就是今天
    assert _rel("小鹿 23:50\n在吗", now="2026-10-07T01:00:00").items[0].ts \
        == "2026-10-06T23:50:00"


def test_text_containing_a_score_is_not_mistaken_for_a_time() -> None:
    """正文里的「3:1」不是时间。

    裸钟点是弱信号，所以只在「紧跟在称呼后面」的位置才认。
    正文里出现 `3:1` / `1:0` 这类比分时，绝不能被当成消息时间偷走。
    """
    res = _parse("小鹿: 我 3:1 赢了这场比赛")
    assert res.items[0].ts == "", f"把正文里的比分当成时间了：{res.items[0].ts}"
    assert res.items[0].text == "我 3:1 赢了这场比赛", repr(res.items[0].text)


def test_one_head_with_several_lines_folds_them_into_that_sender() -> None:
    """块头只出现一次 → 后面几行算同一条消息。

    这是**有意的**读法而不是碰巧：只出现一次「小鹿 21:03:15」时，
    「这段都是小鹿说的」是唯一有依据的解释。反过来把它拆成 3 条、
    再给后 2 条各编一个时间为空的消息，等于凭空造出 2 条不存在的记录。

    真实的多选复制不会走到这里（每条都有自己的头，见上面的用例）；
    这条用例是把「遇到这种输入会怎么处理」写清楚，而不是留给下次猜。
    """
    res = _parse("小鹿 2026-09-28 21:03:15\n第一句\n第二句")
    assert len(res.items) == 1, [i.text for i in res.items]
    assert res.items[0].role == "peer"
    assert res.items[0].ts == "2026-09-28T21:03:15"
    assert res.items[0].text == "第一句\n第二句", repr(res.items[0].text)


# ================================================================ 认不出就不猜


def test_a_lone_line_is_never_attributed_to_anyone() -> None:
    """只有正文（微信单条复制最常见的形态）→ 必须报「待指定」。"""
    res = _parse("最近怎么样啊，好久没聊了")
    assert res.shape == "plain", res.shape
    assert res.needs_sender
    assert res.items[0].text == "最近怎么样啊，好久没聊了"
    assert res.items[0].role == ""


def test_an_unknown_name_is_not_mistaken_for_the_peer() -> None:
    """「老王」不是小鹿 —— 名字对不上就等于认不出，不是「大概是小鹿」。"""
    res = _parse("老王: 今晚聚一下\n小李: 好")
    assert res.needs_sender
    assert all(i.role == "" for i in res.items), _roles(res)


def test_text_containing_a_colon_is_not_a_sender_line() -> None:
    """正文里带冒号不能被当成「谁说的」。"""
    res = _parse("我觉着吧：这事儿得再想想")
    assert res.shape == "plain", res.shape


def test_a_date_inside_the_sentence_is_not_stolen_as_the_message_time() -> None:
    """正文中间出现的日期不是这条消息的时间。

    偷走它有两个后果：时间轴上是假的，正文还被切了一道口子 ——
    两个都不报错，用户也不会发现。
    """
    raw = "我 今天 2026-09-28 21:03:15 那会儿还在加班"
    res = _parse(raw)
    assert res.shape == "plain", res.shape
    assert res.items[0].role == ""
    assert res.items[0].ts == "", repr(res.items[0].ts)
    assert res.items[0].text == raw, repr(res.items[0].text)


def test_a_trailing_time_on_its_own_line_is_recognised() -> None:
    """时间单独在最后一行 → 那是真的时间，要认。"""
    res = _parse("那会儿还在加班\n2026-09-28 21:03:15")
    assert res.items[0].ts == "2026-09-28T21:03:15", res.items[0].ts


def test_longer_names_win_over_their_prefixes() -> None:
    """「小鹿儿」不能被「小鹿」吃掉前缀 —— 那会把两个人的话混起来。

    这就是 `_known_pairs()` 必须按名字长度倒序排的原因。
    """
    res = _parse("小鹿儿 2026-09-28 21:00:00\n我这边没事",
                 peer=["小鹿", "小鹿儿"])
    assert res.items[0].sender == "小鹿儿", res.items[0].sender
    assert res.items[0].role == "peer"


# ================================================================ 监听器


def test_watcher_only_fires_when_the_content_really_changed() -> None:
    """只有内容真的变了才产出一条 —— 靠剪贴板序号判断，不比对文本。

    比对文本会把「用户复制了两遍同一句话」当成没变化，而那是两次真实操作；
    序号在内容真的变化时才 +1，这是系统的口径。
    """
    from app.collect import winapi

    if not winapi.IS_WINDOWS:
        _skip("剪贴板 API 是 Win32 实现，非 Windows 环境跳过")

    saved = cb.read_text()
    try:
        w = cb.ClipboardWatcher(interval=0.1)
        w.reset()
        assert cb.write_text("第一条")
        first = w.poll(me_names=ME, peer_names=PEER)
        again = w.poll(me_names=ME, peer_names=PEER)
        assert cb.write_text("第二条")
        third = w.poll(me_names=ME, peer_names=PEER)
        assert first is not None and first.items[0].text == "第一条"
        assert again is None, "内容没变却又产出了一条 —— 界面会不停重复出现同一条"
        assert third is not None and third.items[0].text == "第二条"
    finally:
        if saved:
            cb.write_text(saved)


def main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    skipped = failed = 0
    print("=" * 64)
    print(f"剪贴板解析守卫：{len(tests)} 项")
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
