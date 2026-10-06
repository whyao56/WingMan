"""对象中心迭代的守卫（S2/S3）：多渠道上下文、桌面壳关闭协商、检查更新。
这一层里最值得钉住的是**多渠道合并**，因为它错起来是静默的：

- 事实只取了主渠道 → 微信上说过的「她讨厌被叫宝贝」，在 QQ 那个渠道分析时就消失，
  模型照样给建议，用户看不出少喂了东西；
- 近期消息不按时间归并 → 「她昨晚说什么、今天又说什么」的先后关系被渠道割断，
  模型会把两条不同渠道的话当成两段无关的事；
- 主渠道不固定 → 用户贴的那句话这次记到 A、下次记到 B，同一条消息在库里出现两遍。

另外两条是「怎么退出」和「有没有新版本」：都属于**失败要说清楚**的类别 ——
「关不掉」和「查不到却说是最新」都让用户没法自己做主。

两种跑法都支持：

    cd backend
    python -m pytest tests/test_objects_center.py -q
    python tests/test_objects_center.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
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
    return Path(tempfile.mkdtemp(prefix="wingman_center_"))


def _fresh_store(tmp: Path):
    from app.store import Store

    store = Store(tmp / "wingman.db")
    store.init()
    return store


def _msg(chat_id: str, sender: str, role: str, ts: str, text: str, platform: str = "qq"):
    from app.schemas import Msg

    return Msg(chat_id=chat_id, platform=platform, sender=sender, role=role,
               ts=datetime.fromisoformat(ts), text=text)


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


def _seed_two_channels(store) -> tuple[str, str, str]:
    """建一个对象 + 两个渠道（微信 / QQ），各放一条事实与一条消息。

    返回 (person_id, wechat_chat_id, qq_chat_id)。
    """
    from app.schemas import Fact

    pid = store.create_person("小鹿")
    store.upsert_chat("wechat:xiaolu", "wechat", "小鹿", peer_name="小鹿",
                      channel="wechat", person_id=pid)
    store.upsert_chat("qq:xiaolu", "qq", "小鹿", peer_name="小鹿",
                      channel="qq", person_id=pid)
    base = datetime(2026, 9, 28, 21, 0, 0)
    store.insert_messages([
        _msg("wechat:xiaolu", "小鹿", "peer", (base).isoformat(), "微信上说：最近在准备考试", "wechat"),
        _msg("qq:xiaolu", "小鹿", "peer", (base + timedelta(minutes=5)).isoformat(), "QQ 上说：考完想去看展"),
    ])
    store.upsert_fact("wechat:xiaolu", Fact(subject="peer", key="讨厌", value="被叫宝贝"))
    store.upsert_fact("qq:xiaolu", Fact(subject="peer", key="喜欢", value="猫"))
    return pid, "wechat:xiaolu", "qq:xiaolu"


# ================================================================ 多渠道上下文


def test_context_merges_channels_and_keeps_primary_first() -> None:
    """多条渠道 → 事实合并、消息按时间归并；第一条是主渠道。

    这三件事缺一件，多渠道就只是个说法：少一条事实会让模型漏掉雷区，
    不归并消息会让它把两个渠道看成两段无关的事，主渠道不固定则会让
    「你贴的那句话记哪儿」变得随机。
    """
    import asyncio

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid, wx, qq = _seed_two_channels(store)

        from app.engine.context import build_context
        from app.schemas import Persona

        class _Ctx:
            def __init__(self, s):
                self.store = s
                self.embedder = None
                self.llm = None

        pack = asyncio.run(build_context(
            _Ctx(store), [wx, qq], "在吗", use_retrieval=False))

        assert pack.chat_ids == [wx, qq], pack.chat_ids
        assert pack.chat.id == wx, "主渠道不是列表里的第一个"

        keys = {(f.subject, f.key, f.value) for f in pack.facts}
        assert ("peer", "讨厌", "被叫宝贝") in keys, "主渠道的事实丢了"
        assert ("peer", "喜欢", "猫") in keys, "另一条渠道的事实没合并进来"

        texts = [str(r.get("text") or "") for r in pack.recent]
        assert any("微信上说" in t for t in texts)
        assert any("QQ 上说" in t for t in texts)
        # 归并后必须保持真实先后：微信那条在前
        assert texts.index(next(t for t in texts if "微信上说" in t)) < \
            texts.index(next(t for t in texts if "QQ 上说" in t))
        assert any("合并了 2 个渠道" in w for w in pack.warnings), pack.warnings
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_context_accepts_a_single_chat_id_string() -> None:
    """单个渠道的老调用方式不能被破坏（所有既有调用点都是这么传的）。"""
    import asyncio

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        _pid, wx, _qq = _seed_two_channels(store)

        from app.engine.context import build_context

        class _Ctx:
            def __init__(self, s):
                self.store = s
                self.embedder = None
                self.llm = None

        pack = asyncio.run(build_context(
            _Ctx(store), wx, "在吗", use_retrieval=False))
        assert pack.chat_ids == [wx]
        assert pack.chat.id == wx
        assert not any("合并了" in w for w in pack.warnings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_context_rejects_an_empty_channel_list() -> None:
    """一条渠道都没勾 → 明确报错，而不是拿一个随机渠道凑合。"""
    import asyncio

    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        _pid, _wx, _qq = _seed_two_channels(store)

        from app.engine.context import build_context

        class _Ctx:
            def __init__(self, s):
                self.store = s
                self.embedder = None
                self.llm = None

        try:
            asyncio.run(build_context(_Ctx(store), [], "在吗", use_retrieval=False))
        except KeyError as exc:
            assert "没有选定任何会话" in str(exc)
        else:
            raise AssertionError("空渠道列表竟然通过了")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 对象级分析接口


def test_person_suggest_uses_the_selected_channels_and_records_them() -> None:
    """`POST /persons/{id}/suggest`：勾哪几个就用哪几个，且全记进历史。

    留存里少记一个渠道，回看时就复现不出「当时到底看着什么给的结论」——
    那样的历史只是一堆孤立的句子。
    """
    client, tmp = _client_with_temp_data_dir()
    try:
        store = client.app.state.ctx.store if hasattr(client.app.state, "ctx") else None
        from app.context import get_ctx

        store = get_ctx().store
        pid, wx, qq = _seed_two_channels(store)

        r = client.post(f"/api/persons/{pid}/suggest",
                        json={"chat_ids": [wx, qq], "peer_message": "在吗", "persist": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["trace"]["chat_ids"] == [wx, qq], body["trace"]
        assert (body["trace"].get("run_id") or 0) > 0, "没有写进历史"

        run = store.get_run(body["trace"]["run_id"])
        assert run is not None
        assert run.chat_ids == [wx, qq], run.chat_ids
        assert run.person_id == pid
        # 你贴的那句话只记到主渠道名下，不会两条都塞
        assert store.last_peer_message(wx)["text"] == "在吗"
        assert (store.last_peer_message(qq) or {}).get("text") != "在吗"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_suggest_defaults_to_all_of_the_person_channels() -> None:
    """不传 chat_ids → 用这个对象名下的全部渠道（「看整个对象」是默认语义）。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        from app.context import get_ctx

        store = get_ctx().store
        pid, wx, qq = _seed_two_channels(store)

        r = client.post(f"/api/persons/{pid}/suggest",
                        json={"peer_message": "在吗", "persist": False})
        assert r.status_code == 200, r.text
        assert set(r.json()["trace"]["chat_ids"]) == {wx, qq}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_suggest_explains_an_object_without_channels() -> None:
    """没有渠道的对象 → 400 且说清「先去采集」，不是 500 或者一个空结果。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        from app.context import get_ctx

        store = get_ctx().store
        pid = store.create_person("空对象")

        r = client.post(f"/api/persons/{pid}/suggest", json={"peer_message": "在吗"})
        assert r.status_code == 400, r.text
        assert "渠道" in r.json()["detail"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_suggest_404s_on_an_unknown_object() -> None:
    client, tmp = _client_with_temp_data_dir()
    try:
        r = client.post("/api/persons/no-such-person/suggest", json={"peer_message": "在吗"})
        assert r.status_code == 404, r.text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_suggest_404s_on_an_unknown_channel() -> None:
    """勾了一个不存在的渠道 → 404，而不是悄悄降级成「只用一个」。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        from app.context import get_ctx

        store = get_ctx().store
        pid, wx, _qq = _seed_two_channels(store)

        r = client.post(f"/api/persons/{pid}/suggest",
                        json={"chat_ids": [wx, "qq:不存在"], "peer_message": "在吗"})
        assert r.status_code == 404, r.text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 桌面壳（需求 9）


def test_desktop_state_reports_no_native_window_under_tests() -> None:
    client, tmp = _client_with_temp_data_dir()
    try:
        r = client.get("/api/desktop/state")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["native_window"] is False
        assert "platform" in body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_close_rejects_an_unknown_action() -> None:
    """拼错的 action 必须 422 —— 否则用户点了「关闭」却什么也没发生。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        r = client.post("/api/desktop/close", json={"action": "shutdown"})
        assert r.status_code == 422, r.text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_close_cancel_keeps_running() -> None:
    client, tmp = _client_with_temp_data_dir()
    try:
        r = client.post("/api/desktop/close", json={"action": "cancel"})
        assert r.status_code == 200, r.text
        assert r.json()["action"] == "cancel"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_close_background_without_a_window_does_not_kill_the_server() -> None:
    """浏览器模式点「后台运行」不能真的退出 —— 那会把服务一起带走。

    没有原生窗口时它退化成「告诉你关掉标签页即可」，动作类型仍报 quit 是
    给界面用的语义（没有窗口可隐藏），但**绝不能**触发进程退出。
    """
    client, tmp = _client_with_temp_data_dir()
    try:
        from app import desktop

        killed: list[str] = []
        original = desktop._force_exit_soon
        desktop._force_exit_soon = lambda *a, **k: killed.append("exit")
        try:
            r = client.post("/api/desktop/close", json={"action": "background"})
            assert r.status_code == 200, r.text
            assert not killed, "浏览器模式下点「后台运行」把进程杀了"
            assert desktop._STOP.is_set() is False
        finally:
            desktop._force_exit_soon = original
            desktop._STOP.clear()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_close_quit_signals_shutdown_without_forkbombing() -> None:
    """「关闭程序」要真的发起退出（这里把强退替换掉，只验信号与返回值）。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        from app import desktop

        called: list[str] = []
        original = desktop._force_exit_soon
        desktop._force_exit_soon = lambda *a, **k: called.append("exit")
        try:
            r = client.post("/api/desktop/close", json={"action": "quit"})
            assert r.status_code == 200, r.text
            assert r.json()["action"] == "quit"
            assert desktop._STOP.is_set(), "没有发出退出信号"
            assert desktop._CLOSE["pending"] is False, "拦下的关闭没有复位"
        finally:
            desktop._force_exit_soon = original
            desktop._STOP.clear()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_show_without_a_window_says_so_instead_of_pretending() -> None:
    """浏览器模式调 /desktop/show：要如实说「没有窗口可显示」。

    如果这里谎报 `shown=True`，`desktop.main()` 的单实例分支就会以为
    「窗口已经调回来了」然后直接 return —— 用户双击一次什么都不会发生。
    """
    client, tmp = _client_with_temp_data_dir()
    try:
        r = client.post("/api/desktop/show", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["shown"] is False
        assert "8787" in body["hint"], "要告诉用户改去浏览器打开哪个地址"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_show_calls_hide_inverse_on_a_real_window() -> None:
    """有窗口时 /desktop/show 必须真的调 show()。

    「后台运行」藏了窗口之后，这是唯一的回头路 —— 之前再双击 `WingMan.exe`
    会走 `_open_window` 开出一个**没有服务的空壳**，关掉它并不会停掉真正在跑的
    那个进程，用户看到的是「关闭程序失灵」。
    """
    client, tmp = _client_with_temp_data_dir()
    try:
        from app import desktop

        calls: list[str] = []

        class _FakeWindow:
            def show(self) -> None:
                calls.append("show")

        original = desktop._WINDOW
        desktop._WINDOW = _FakeWindow()
        try:
            r = client.post("/api/desktop/show", json={})
            assert r.status_code == 200, r.text
            assert r.json()["shown"] is True
            assert calls == ["show"], "没有把窗口调出来"
            # 顺带确认 state 也跟着变 —— 界面靠它决定 × 该怎么说
            assert client.get("/api/desktop/state").json()["native_window"] is True
        finally:
            desktop._WINDOW = original
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_desktop_show_reports_failure_when_the_window_refuses() -> None:
    """show() 抛异常时要返回 shown=False 并带上原因，不能假装成功。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        from app import desktop

        class _BrokenWindow:
            def show(self) -> None:
                raise RuntimeError("窗口已经没了")

        original = desktop._WINDOW
        desktop._WINDOW = _BrokenWindow()
        try:
            body = client.post("/api/desktop/show", json={}).json()
            assert body["ok"] is False
            assert body["shown"] is False
            assert "窗口已经没了" in body["error"]
        finally:
            desktop._WINDOW = original
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 检查更新（需求 5）


def test_version_tuple_orders_numerically_not_lexically() -> None:
    """版本比较必须按数字，不能按字符串 —— `0.10.0` 比 `0.9.0` 新。"""
    from app.api.routes_admin import _ver_tuple

    assert _ver_tuple("0.10.0") > _ver_tuple("0.9.0")
    assert _ver_tuple("v0.4.0") == _ver_tuple("0.4.0")
    assert _ver_tuple("1.0") > _ver_tuple("0.9.9")
    assert _ver_tuple("") == (0,)


def test_update_check_says_unknown_instead_of_up_to_date(monkeypatch=None) -> None:
    """查不到网络时必须说「没查到」，**不能**说「已是最新」。

    把「不知道」说成「最新」，是在替用户做一个他没法复核的结论 ——
    他会以为自己这版就是最新的，从而不去更新。
    """
    client, tmp = _client_with_temp_data_dir()
    try:
        import urllib.request

        def _boom(*_a, **_k):
            raise OSError("network unreachable")

        original = urllib.request.urlopen
        urllib.request.urlopen = _boom
        try:
            r = client.get("/api/update/check")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is False
            assert "has_update" not in body, "失败时不能给出 has_update 这种结论"
            assert "不等于已是最新" in body["hint"], body["hint"]
            assert body["repo"].startswith("https://github.com/")
        finally:
            urllib.request.urlopen = original
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_check_reports_a_newer_release() -> None:
    """能查到时，比较结果要正确（这里把 HTTP 换成假响应，不打真网络）。"""
    client, tmp = _client_with_temp_data_dir()
    try:
        import json
        import urllib.request

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self, *_a):
                return json.dumps({
                    "tag_name": "v99.0.0", "name": "未来的版本",
                    "published_at": "2030-01-01T00:00:00Z",
                    "body": "很大的改动", "html_url": "https://github.com/whyao56/WingMan/releases/tag/v99.0.0",
                }).encode("utf-8")

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _Resp()
        try:
            r = client.get("/api/update/check")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            assert body["latest"] == "99.0.0", body
            assert body["has_update"] is True
            assert body["notes"] == "很大的改动"
        finally:
            urllib.request.urlopen = original
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_check_marks_the_current_version_as_up_to_date() -> None:
    client, tmp = _client_with_temp_data_dir()
    try:
        import json
        import urllib.request

        from app import __version__

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self, *_a):
                return json.dumps({"tag_name": f"v{__version__}", "body": ""}).encode("utf-8")

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _Resp()
        try:
            body = client.get("/api/update/check").json()
            assert body["ok"] is True
            assert body["has_update"] is False
        finally:
            urllib.request.urlopen = original
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 直跑入口


def _collect_tests():
    return [(name, obj) for name, obj in list(globals().items())
            if name.startswith("test_") and callable(obj)]


def main() -> int:
    print("=" * 68, flush=True)
    print("对象中心：多渠道上下文 / 桌面壳 / 检查更新（临时目录）", flush=True)
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
        return 1
    print("结果：全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
