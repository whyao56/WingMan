"""采集接口的契约守卫。

这里测的不是「算法对不对」（那在 `test_collect_reader.py` /
`test_collect_semi.py` 里），而是**接口告诉用户什么**。采集这个功能的特点是：
失败的原因有很多种（版本不对、没登录、密钥取不到、库还没建），
而用户只看得懂一句话。所以接口层最重要的性质是：

1. **该拒的必须拒，而且要拒得明白。** `semi/start` 不指定采谁就该 422，
   而不是「先跑起来，反正什么都没采到」。
2. **不能把「我不知道」说成「支持」。** `/matrix` 要如实区分实测过的版本
   和没实测过的版本。
3. **校验不通过时不能返回垃圾。** 密钥校验失败要返回 `verified=false`
   加一句人话，不能是 500、更不能是「大概可以」。

另外还有一条工程性质的：**响应体的类型注解要写对**。
FastAPI 会按注解校验返回值，注解写成 `dict` 而实际返回 `list` 时
接口是 500 —— 这类错在界面上的表现是「点了没反应」，
只有真的发一次请求才发现得到。`test_matrix_is_a_list_not_a_dict` 就钉这个。

跑法：

    cd backend
    python -m pytest tests/test_collect_api.py -q
    python tests/test_collect_api.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

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


def _client(tmp: Path):
    """一个把数据目录指向临时目录的 TestClient。"""
    from app import config

    config.DATA_DIR = tmp
    config.get_settings.cache_clear()

    import app.context as context_module
    from app.collect import semi as semi_mod

    consumer = tmp / "collect_cache"
    consumer.mkdir(parents=True, exist_ok=True)
    semi_mod.STATE_FILE = consumer / "semi_state.json"
    semi_mod._SEMI = None
    context_module._CTX = None

    from app.main import app
    from fastapi.testclient import TestClient

    ctx = context_module.get_ctx()
    ctx.update_cfg({"llm_provider": "mock", "embedder": "hash"})
    return TestClient(app), ctx


# ================================================================ 版本指引


def test_clients_endpoint_says_what_to_do_not_just_what_is_wrong() -> None:
    """`/clients` 必须给出「下一步做什么」，而不只是一个状态码。

    用户点进采集页时心里只有一句「我这能不能用」。所以每个客户端都要带
    `verdict`（支不支持）和 `actions`（照做的事）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.get("/api/collect/clients?deep=false")
        assert r.status_code == 200, r.text
        body = r.json()
        keys = {cl["client"] for cl in body["clients"]}
        assert {"qq", "wechat"} <= keys, keys
        for cl in body["clients"]:
            assert cl["verdict"] in {
                "supported", "untested", "unsupported", "not_installed"}, cl
            assert cl["headline"], "每个客户端都要有一句给人看的结论"
            assert cl["actions"], f"{cl['client']} 没说下一步做什么"
        assert body["summary"]["next_steps"], "总览里要有可以照做的下一步"
        assert isinstance(body["matrix"], list)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_matrix_is_a_list_not_a_dict() -> None:
    """响应体的注解必须和实际返回一致 —— 写错的话接口是 500。

    FastAPI 会按类型注解校验返回值。这个接口本来注解写成了 `dict`，
    实际返回是列表，表现是「界面点了没反应、日志里一个 500」。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.get("/api/collect/matrix")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert isinstance(rows, list) and rows
        by_key = {row["client"]: row for row in rows}
        assert "wechat" in by_key and "qq" in by_key
        for row in rows:
            for field in ("name", "range", "tested", "guide", "layout"):
                assert field in row, f"{row.get('client')} 少了字段 {field}"
            assert row["tested"], "「实测过哪些版本」必须写出来，不能留空"
        # 实测过的版本要如实体现在 tested 里，而不是含糊地说「支持」
        assert "4.1.13.12" in by_key["wechat"]["tested"]
        assert "9.9.20.37051" in by_key["qq"]["tested"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unknown_client_is_a_404_with_the_name_in_it() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.get("/api/collect/clients/nothing")
        assert r.status_code == 404
        assert "nothing" in r.json()["detail"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 密钥


def test_key_check_explains_a_bad_key_instead_of_crashing() -> None:
    """粘进来的东西不是密钥 → 说清「它应该长什么样」，不是 500。"""
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.post("/api/collect/key/check",
                   json={"client": "qq", "key": "这不是密钥"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "64" in body["detail"] and "十六进制" in body["detail"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_key_check_never_claims_success_when_it_could_not_verify() -> None:
    """格式对但没库可验时，必须是 `verified=false`，不能说「可以开始采集」。

    这是本模块 fail-closed 的同一个原则落到接口上：不确定就不说确定的话。
    32 字节的随机密钥几乎不可能通过解密校验，所以这里断言的是
    「不会假装成功」，而不是「一定成功」。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.post("/api/collect/key/check", json={
            "client": "qq", "key": "ab" * 32, "db": str(tmp / "no-such.db"),
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert not (body.get("ok") and body.get("verified") and body.get("profile")), body
        assert body.get("detail")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 半自动：守卫


def test_semi_start_refuses_when_nobody_was_specified() -> None:
    """不指定「采谁」就该 422，而不是先跑起来什么都不归属。

    抓到的消息没有归属，用户在界面上会看到一堆「这是谁说的？」——
    这比直接告诉他「先选个人」更糟。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.post("/api/collect/semi/start", json={"client": "qq"})
        assert r.status_code == 422
        assert "至少要指定" in r.json()["detail"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_semi_start_rejects_an_unknown_person_id() -> None:
    """指名道姓要采一个不存在的人 → 404，不静默降级成「新建一个」。"""
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.post("/api/collect/semi/start",
                   json={"client": "qq", "person_id": "p-does-not-exist"})
        assert r.status_code == 404
        assert "p-does-not-exist" in r.json()["detail"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_semi_lifecycle_start_status_stop() -> None:
    """start → status 显示在跑 → stop。提示语要能照着做。"""
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, ctx = _client(tmp)
        pid = ctx.store.create_person("小鹿")
        r = c.post("/api/collect/semi/start",
                   json={"client": "qq", "peer_name": "小鹿", "person_id": pid})
        assert r.status_code == 200, r.text
        state = r.json()["state"]
        assert state["active"] is True
        assert state["person_id"] == pid
        assert "小鹿" in state["poll_hint"], state["poll_hint"]

        got = c.get("/api/collect/semi/status")
        assert got.status_code == 200
        assert got.json()["state"]["active"] is True

        stop = c.post("/api/collect/semi/stop")
        assert stop.status_code == 200
        assert stop.json()["state"]["active"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_semi_commit_needs_a_real_capture_id() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        assert c.post("/api/collect/semi/commit", json={}).status_code == 422
        r = c.post("/api/collect/semi/commit", json={"capture_id": "nope"})
        assert r.status_code == 404
        r2 = c.post("/api/collect/semi/discard", json={"capture_id": "nope"})
        assert r2.status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_semi_poll_is_safe_before_anything_was_started() -> None:
    """没开启时轮询也要能返回，而不是报错 —— 界面是定时轮询的。"""
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.get("/api/collect/semi/poll")
        assert r.status_code == 200
        body = r.json()
        assert body["captures"] == []
        assert body["state"]["active"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 其它接口


def test_inspect_on_a_missing_file_is_a_404() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        r = c.post("/api/collect/inspect", json={"db": str(tmp / "nope.db")})
        assert r.status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cursors_round_trip_and_reset_reports_what_it_removed() -> None:
    """重置游标必须**不用账号也能重置**，并且如实回报删了几条。

    游标的主键是 `(平台, 账号, 对方)`，但界面上用户只看到「小鹿 · QQ」。
    如果接口要求账号必须精确匹配，用户点「重置」会删掉 0 条、接口照旧返回
    `ok`——下次采集还是从老位置继续，而用户以为已经重置了。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, ctx = _client(tmp)
        ctx.store.save_cursor("qq", "2213914174", "小鹿", chat_id="qq:xiaolu",
                              last_ts="2026-09-28T21:00:00")
        rows = c.get("/api/collect/cursors").json()["cursors"]
        assert any(x["peer_key"] == "小鹿" for x in rows), rows

        bad = c.request("DELETE", "/api/collect/cursors", json={"platform": "qq"})
        assert bad.status_code == 422

        # 不传 account：按「小鹿在 QQ 上的全部账号」重置
        ok = c.request("DELETE", "/api/collect/cursors",
                       json={"platform": "qq", "peer_key": "小鹿"})
        assert ok.status_code == 200
        assert ok.json()["removed"] == 1, ok.text
        rows = c.get("/api/collect/cursors").json()["cursors"]
        assert not any(x["peer_key"] == "小鹿" for x in rows)

        # 什么都没有的时候要如实说 0 条，不能让界面显示「已重置」
        again = c.request("DELETE", "/api/collect/cursors",
                          json={"platform": "qq", "peer_key": "小鹿"})
        assert again.json()["removed"] == 0
        assert "没有找到" in again.json()["detail"], again.text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preview_never_writes_to_the_db() -> None:
    """`/preview` 走完整条链路但不落库 —— 采集不可逆，必须先有「先看看」。"""
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, ctx = _client(tmp)
        before = ctx.store.counts()["messages"]
        # allow_memory_scan=false：这条测的是「预演不写库」，
        # 不是「能不能搜到密钥」，别把时间花在几十秒的内存扫描上。
        r = c.post("/api/collect/preview", json={
            "client": "qq", "max_messages": 5, "allow_memory_scan": False})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert "describe" in rep, f"报告要有一句总结：{list(rep)}"
        assert rep["dry_run"] is True
        assert rep["inserted"] == 0
        assert ctx.store.counts()["messages"] == before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preview_says_out_loud_that_it_shortened_the_key_search() -> None:
    """预演压缩了搜密钥的预算，就必须把这件事写在报告里。

    不然用户看到「预演：取不到密钥」会以为这条路彻底走不通，
    直接放弃 —— 而正式采集给的时间预算长得多。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wingman_capi_"))
    try:
        c, _ = _client(tmp)
        rep = c.post("/api/collect/preview",
                     json={"client": "qq", "allow_memory_scan": False}).json()
        assert rep["notes"], "压缩了预算却不说，等于给了个会被误读的结论"
        assert any("预演" in n for n in rep["notes"]), rep["notes"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ 真剪贴板端到端


def test_end_to_end_with_the_real_clipboard() -> None:
    """真的写一次系统剪贴板，走完整条 HTTP 链路，看消息有没有落对人。

    这条默认**跳过**（要显式设 `WINGMAN_E2E_CLIPBOARD=1`）：它会动用户
    当前复制的内容。虽然结束时会把原内容还回去，但测试不应该有副作用。
    需要验证真实链路时：

        WINGMAN_E2E_CLIPBOARD=1 python -m pytest tests/test_collect_api.py -q
    """
    from app.collect import clipboard as cb
    from app.collect import winapi

    if os.environ.get("WINGMAN_E2E_CLIPBOARD") != "1":
        _skip("需要显式开启（会临时改写系统剪贴板）：WINGMAN_E2E_CLIPBOARD=1")
    if not winapi.IS_WINDOWS:
        _skip("剪贴板 API 是 Win32 实现，非 Windows 环境跳过")

    tmp = Path(tempfile.mkdtemp(prefix="wingman_e2e_"))
    saved = cb.read_text()
    try:
        c, ctx = _client(tmp)
        pid = ctx.store.create_person("小鹿")
        r = c.post("/api/collect/semi/start",
                   json={"client": "qq", "peer_name": "小鹿", "person_id": pid})
        assert r.status_code == 200, r.text
        chat_id = r.json()["state"]["chat_id"]
        assert chat_id, "要有一个确定的会话 id，否则采到的东西不知道往哪写"

        # QQ NT 多选复制的真实形态：每条消息前面一个「称呼 + 空格 + 时间」
        cb.write_text(
            "小鹿 2026-09-28 21:03:15\n我今天加班到十点\n"
            "小鹿 2026-09-28 21:03:40\n嗯，你呢？\n"
            "我 2026-09-28 21:04:05\n那你早点睡"
        )

        deadline = time.time() + 10
        cap = None
        while time.time() < deadline:
            caps = c.get("/api/collect/semi/poll").json()["captures"]
            if caps:
                cap = caps[0]
                break
            time.sleep(0.3)
        assert cap is not None, "10 秒内没有抓到剪贴板内容"

        # 三条都带称呼 → 归属全有依据 → **直接落库**，不用人工确认
        assert cap["status"] == "committed", cap
        assert not cap["needs_user"], cap
        assert cap["written"] == 3, cap

        rows = ctx.store.list_messages(chat_id, limit=20)
        assert [x["text"] for x in rows] == ["我今天加班到十点", "嗯，你呢？", "那你早点睡"], rows
        assert [(x["sender"], x["role"]) for x in rows] == [
            ("小鹿", "peer"), ("小鹿", "peer"), ("我", "me")], rows
        assert all(x["ts_source"] == "clipboard" for x in rows), rows
        assert rows[0]["ts"].startswith("2026-09-28T21:03:15"), rows[0]["ts"]
        assert [p.name for p in ctx.store.list_persons()] == ["小鹿"]
    finally:
        if saved:
            cb.write_text(saved)
        c.post("/api/collect/semi/stop")
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    skipped = failed = 0
    print("=" * 64)
    print(f"采集接口契约：{len(tests)} 项")
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
