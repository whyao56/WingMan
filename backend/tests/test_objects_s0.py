"""对象中心迭代 S0 的守卫：迁移、消息二次编辑、输出留存、对象级事实与设定。

这一层是「后端打底」：**纯加能力，不动界面**。它最怕的不是崩溃，而是：
1. **迁移不幂等** —— 每次启动重跑一遍加列/重建，第二次就报错，程序起不来；
2. **对象级事实没有唯一性保护** —— SQLite 里 NULL 互不相等，chat_id 为空的行
   靠旧的唯一键等于没约束，同一条被抽两次就存两条，而且是静默的；
3. **二次编辑撞唯一键当成 500** —— 对用户这不是服务出错，是「你改的这条和已有的
   重复了」，必须能区分并给出人话提示；
4. **改平台时把 chat_id 也改了** —— chat_id 是幂等键与采集游标的依据，改了会
   静默破坏去重与增量。

两种跑法都支持：

    cd backend
    python -m pytest tests/test_objects_s0.py -q
    python tests/test_objects_s0.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime
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
    return Path(tempfile.mkdtemp(prefix="wingman_objects_"))


def _msg(chat_id: str, sender: str, role: str, ts: str, text: str, platform: str = "qq"):
    from app.schemas import Msg

    return Msg(chat_id=chat_id, platform=platform, sender=sender, role=role,
               ts=ts, text=text)


def _fresh_store(tmp: Path):
    from app.store import Store

    store = Store(tmp / "wingman.db")
    store.init()
    return store


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


# ---------------------------------------------------------------- 迁移与建表


def test_migrations_are_idempotent_and_add_all_new_structures() -> None:
    """反复 init 不报错，且新列 / 新表 / 部分唯一索引都到位。

    迁移写得不幂等，表现是「第二次启动起不来」—— 而用户只会看到程序打不开。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.init()
        store.init()
        with store.conn() as c:
            mcols = {r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
            fcols = {r[1] for r in c.execute("PRAGMA table_info(facts)").fetchall()}
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            idx = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "captured_at" in mcols
        assert "person_id" in fcols
        for t in ("person_personas", "engine_runs", "sim_runs", "activity_log"):
            assert t in tables, f"缺表：{t}"
        assert "ux_facts_person" in idx
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_facts_person_id_is_backfilled_from_chats() -> None:
    """老库事实的 person_id 要按 chats 回填 —— 不回填，对象级视图就漏数据。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq:a", "qq", "小鹿", peer_name="小鹿")
        from app.schemas import Fact

        store.upsert_fact("qq:a", Fact(subject="peer", key="喜欢", value="猫"))
        # 模拟老库：列是空的
        with store.conn() as c:
            c.execute("UPDATE facts SET person_id = NULL")
        store.init()
        fact = store.list_facts("qq:a")[0]
        assert fact.person_id == store.get_chat("qq:a").person_id
        assert fact.person_id, "回填不能是空"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_scope_index_dedupes_before_creating() -> None:
    """建部分唯一索引前必须先清重复 —— 否则启动直接失败。

    这里手工制造两个「没有唯一键保护」时的重复行（先删索引），再 init，
    验证迁移把它们收敛成一条、且索引确实建立。
    """
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿")
        with store.conn() as c:
            c.execute("DROP INDEX IF EXISTS ux_facts_person")
            c.execute(
                "INSERT INTO facts (chat_id, person_id, subject, key, value) "
                "VALUES (NULL, ?, 'peer', '怕黑', '是')", (pid,))
            c.execute(
                "INSERT INTO facts (chat_id, person_id, subject, key, value) "
                "VALUES (NULL, ?, 'peer', '怕黑', '是')", (pid,))
        store.init()          # 应该去重并重建索引，不抛异常
        with store.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM facts WHERE person_id = ? "
                "AND (chat_id IS NULL OR chat_id = '')", (pid,)).fetchone()[0]
            idx = c.execute(
                "SELECT name FROM sqlite_master WHERE name = 'ux_facts_person'").fetchone()
        assert n == 1, f"重复行没有被收敛：{n}"
        assert idx is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 消息二次编辑（数据层）


def test_update_message_returns_structured_errors_not_exceptions() -> None:
    """撞唯一键返回结构化错误，不是抛异常让接口 500。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq:a", "qq", "小鹿", peer_name="小鹿")
        store.insert_messages([
            _msg("qq:a", "小鹿", "peer", "2026-01-01T10:00:00", "A"),
            _msg("qq:a", "小鹿", "peer", "2026-01-01T10:00:05", "B"),
        ])
        ids = [r["id"] for r in store.list_messages("qq:a")]

        ok = store.update_message(ids[0], {"text": "A（改）"})
        assert ok["ok"] and ok["message"]["text"] == "A（改）"

        dup = store.update_message(ids[1], {"text": "A（改）", "ts": "2026-01-01T10:00:00"})
        assert dup["ok"] is False and dup["error"] == "duplicate"
        assert "完全一样" in dup["detail"], "要给人话提示，而不是干巴巴的错误码"

        missing = store.update_message(99999, {"text": "x"})
        assert missing["error"] == "not_found"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_set_chat_platform_recomputes_channel_and_keeps_chat_id() -> None:
    """改平台时 channel 按映射重算，且**绝不改 chat_id**。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        store.upsert_chat("qq:a", "qq", "小鹿", peer_name="小鹿")
        got = store.set_chat_platform("qq:a", "offline")
        assert got is not None
        assert got.id == "qq:a", "chat_id 是幂等键，绝不能改"
        assert got.platform == "offline" and got.channel == "offline"
        # 显式传 channel 时听显式的
        got2 = store.set_chat_platform("qq:a", "offline", channel="generic")
        assert got2.channel == "generic"
        assert store.set_chat_platform("nope", "qq") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 输出留存 / 痕迹（数据层）


def test_runs_and_activity_roundtrip() -> None:
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿")
        rid = store.save_run(person_id=pid, chat_ids=["qq:a"], peer_message="在吗",
                             analysis={"emotion": "平静"}, strategy={"tone": "轻松"},
                             options=[{"id": "o1", "text": "hi"}], trace={"total_ms": 12})
        assert rid > 0
        store.save_sim_run(run_id=rid, option_id="o1", option_text="hi",
                           branches=[{"label": "升温"}], advice="稳一点")
        run = store.get_run(rid)
        assert run is not None and run.analysis["emotion"] == "平静"
        assert len(run.sim_runs) == 1
        assert len(store.list_runs(pid)) == 1
        assert store.get_run(99999) is None

        store.log_activity("edit", person_id=pid, chat_id="qq:a", summary="批量改 2 条")
        acts = store.list_activity(person_id=pid)
        assert [(a.kind, a.summary) for a in acts] == [("edit", "批量改 2 条")]

        assert store.delete_run(rid) == 1
        assert store.get_run(rid) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 对象级事实 / 设定（数据层）


def test_person_facts_merge_two_scopes_without_losing_either() -> None:
    """对象级与渠道级事实都要看得到，且各自标着来源。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿")
        store.upsert_chat("qq:a", "qq", "小鹿", peer_name="小鹿", person_id=pid)
        from app.schemas import Fact

        store.upsert_fact("qq:a", Fact(subject="peer", key="喜欢", value="猫"))
        store.upsert_person_fact(pid, Fact(subject="peer", key="怕黑", value="是"))
        # 重复写对象级同一条 → 只能一条
        store.upsert_person_fact(pid, Fact(subject="peer", key="怕黑", value="是"))

        merged = store.list_facts_for_person(pid)
        scopes = {(f.scope, f.key) for f in merged}
        assert ("person", "怕黑") in scopes
        assert ("chat", "喜欢") in scopes
        assert sum(1 for f in merged if f.scope == "person") == 1

        # 直接绕过 store 方法塞一条重复：必须被部分唯一索引拦住。
        # 这就是「SQLite 里 NULL 互不相等」那个坑的守卫 —— 没有这条索引，
        # 对象级事实会静默存成两条。
        import sqlite3

        raised = False
        try:
            with store.conn() as c:
                c.execute(
                    "INSERT INTO facts (chat_id, person_id, subject, key, value) "
                    "VALUES (NULL, ?, 'peer', '怕黑', '是')", (pid,))
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "对象级事实必须被 ux_facts_person 拦住"

        # 不存在的人不能写
        assert store.upsert_person_fact("p_nope", Fact(key="k", value="v")) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_persona_is_partial_and_keeps_other_fields() -> None:
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿")
        assert store.get_person_persona(pid).goal == ""
        store.save_person_persona(pid, {"goal": "想约看展", "taboos": "宝贝"})
        p = store.save_person_persona(pid, {"stage": "熟悉期"})
        assert p is not None
        assert p.goal == "想约看展" and p.taboos == "宝贝" and p.stage == "熟悉期"
        assert store.save_person_persona("p_nope", {"goal": "x"}) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_deleting_a_person_drops_its_object_persona() -> None:
    """删人时对象级设定一起走（它挂在人身上）；聊天记录照旧不删。"""
    tmp = _tmp_dir()
    try:
        store = _fresh_store(tmp)
        pid = store.create_person("小鹿")
        store.upsert_chat("qq:a", "qq", "小鹿", peer_name="小鹿", person_id=pid)
        store.save_person_persona(pid, {"goal": "看展"})
        store.delete_person(pid)
        with store.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM person_personas WHERE person_id = ?", (pid,)).fetchone()[0]
        assert n == 0
        assert store.get_chat("qq:a") is not None, "删人不该动聊天记录"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- HTTP：消息


def _seed_chat(client, name: str = "小鹿", chat_id: str = "qq:api"):
    """建一个对象 + 一条会话 + 两条消息，返回 (person_id, [msg_id...])。"""
    import app.context as context_module
    from app.schemas import Msg

    pid = client.post("/api/persons", json={"name": name}).json()["id"]
    store = context_module.get_ctx().store
    store.upsert_chat(chat_id, "qq", name, peer_name=name, me_name="我", person_id=pid)
    store.insert_messages([
        Msg(chat_id=chat_id, platform="qq", sender=name, role="peer",
            ts=datetime(2026, 1, 1, 10, 0, 0), text="在吗"),
        Msg(chat_id=chat_id, platform="qq", sender=name, role="peer",
            ts=datetime(2026, 1, 1, 10, 0, 5), text="忙不忙"),
    ])
    ids = [r["id"] for r in store.list_messages(chat_id)]
    return pid, ids


def test_message_edit_endpoints_normal_and_boundaries() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            _pid, ids = _seed_chat(client)

            # 正常：改文本
            ok = client.patch(f"/api/messages/{ids[0]}", json={"text": "在吗？"})
            assert ok.status_code == 200, ok.text
            assert ok.json()["message"]["text"] == "在吗？"

            # 边界：改成和另一条完全一样 → 409 结构化错误
            dup = client.patch(f"/api/messages/{ids[1]}",
                               json={"text": "在吗？", "ts": "2026-01-01T10:00:00"})
            assert dup.status_code == 409, dup.text
            assert dup.json()["error"] == "duplicate"

            # 边界：不存在 / 没字段 / 非法 role
            assert client.patch("/api/messages/99999", json={"text": "x"}).status_code == 404
            assert client.patch(f"/api/messages/{ids[0]}", json={}).status_code == 422
            assert client.patch(f"/api/messages/{ids[0]}",
                                json={"role": "boss"}).status_code == 422

            # 手动加一条：正常 + 重复
            add = client.post("/api/chats/qq:api/messages",
                              json={"sender": "我", "role": "me", "text": "在的",
                                    "ts": "2026-01-01T10:01:00"})
            assert add.status_code == 200, add.text
            new_id = add.json()["id"]
            again = client.post("/api/chats/qq:api/messages",
                                json={"sender": "我", "role": "me", "text": "在的",
                                      "ts": "2026-01-01T10:01:00"})
            assert again.status_code == 409
            assert client.post("/api/chats/nope/messages",
                               json={"sender": "我", "role": "me", "text": "x"}).status_code == 404
            assert client.post("/api/chats/qq:api/messages",
                               json={"sender": "", "role": "me", "text": "x"}).status_code == 422

            # 删一条：正常 + 再删 404
            assert client.delete(f"/api/messages/{new_id}").status_code == 200
            assert client.delete(f"/api/messages/{new_id}").status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bulk_endpoint_normal_and_boundaries() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            _pid, ids = _seed_chat(client)

            # 边界：空 ids 不报错
            empty = client.post("/api/messages/bulk", json={"ids": [], "action": "delete"})
            assert empty.json() == {"changed": 0, "skipped": 0, "errors": []}

            # 边界：非法 action
            assert client.post("/api/messages/bulk",
                               json={"ids": ids, "action": "nuke"}).status_code == 422

            # 正常：批量改角色
            role = client.post("/api/messages/bulk",
                               json={"ids": ids, "action": "set_role", "value": "me"})
            assert role.json()["changed"] == 2, role.text

            # 正常：批量改时间会标 manual
            ts = client.post("/api/messages/bulk",
                             json={"ids": [ids[0]], "action": "set_ts",
                                   "value": "2026-03-03T08:00:00"})
            assert ts.json()["changed"] == 1
            import app.context as context_module

            row = context_module.get_ctx().store.messages_by_ids([ids[0]])[0]
            assert row["ts_source"] == "manual", "人工改的时间不该继续冒充原始时间"

            # 边界：不存在的 id → skipped（不是错误）
            gone = client.post("/api/messages/bulk",
                               json={"ids": [424242], "action": "delete"})
            assert gone.json() == {"changed": 0, "skipped": 1, "errors": []}

            # 正常：批量删除
            dele = client.post("/api/messages/bulk",
                               json={"ids": ids, "action": "delete"})
            assert dele.json()["changed"] == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bulk_reports_duplicate_as_error_not_500() -> None:
    """批量把一个 id 改成与另一条完全重复 → 进 errors，接口仍是 200。"""
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            _pid, ids = _seed_chat(client)
            # 先把第二条文本改成和第一条一致（时间仍不同，不撞键）
            assert client.patch(f"/api/messages/{ids[1]}",
                                json={"text": "在吗"}).status_code == 200
            # 再把它的时间也改成和第一条一样 → 撞唯一键 → 计入 errors
            out = client.post("/api/messages/bulk", json={
                "ids": [ids[1]], "action": "set_ts", "value": "2026-01-01T10:00:00"})
            assert out.status_code == 200, out.text
            body = out.json()
            assert body["changed"] == 0
            assert len(body["errors"]) == 1
            assert body["errors"][0]["error"] == "duplicate"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- HTTP：对象


def test_person_overview_and_history_endpoints() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            pid, _ids = _seed_chat(client)
            ov = client.get(f"/api/persons/{pid}/overview")
            assert ov.status_code == 200, ov.text
            body = ov.json()
            assert body["person"]["id"] == pid
            assert body["counts"]["channels"] == 1
            assert body["counts"]["messages"] == 2
            assert body["person"]["message_count"] == 2
            assert isinstance(body["todos"], list)
            assert len(body["recent_messages"]) == 2

            assert client.get("/api/persons/p_nope/overview").status_code == 404

            hist = client.get(f"/api/persons/{pid}/history")
            assert hist.status_code == 200
            assert set(hist.json()) == {"person_id", "runs", "activity"}
            assert client.get("/api/persons/p_nope/history").status_code == 404
            assert client.get(f"/api/persons/{pid}/history", params={"kind": "edit"}).status_code == 200
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_facts_and_persona_endpoints() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            pid, _ids = _seed_chat(client)

            # 对象级事实：新增 / 列出 / 删除
            added = client.post(f"/api/persons/{pid}/facts",
                                json={"key": "怕黑", "value": "是", "subject": "peer"})
            assert added.status_code == 200, added.text
            fid = added.json()["id"]
            assert added.json()["scope"] == "person"
            listed = client.get(f"/api/persons/{pid}/facts").json()
            assert any(f["scope"] == "person" and f["key"] == "怕黑" for f in listed)

            # 边界：空 key/value 422；不存在的人 404
            assert client.post(f"/api/persons/{pid}/facts",
                               json={"key": "", "value": ""}).status_code == 422
            assert client.post("/api/persons/p_nope/facts",
                               json={"key": "k", "value": "v"}).status_code == 404
            assert client.get("/api/persons/p_nope/facts").status_code == 404

            # 边界：删不属于这个人的事实 → 404
            assert client.delete(f"/api/persons/{pid}/facts/99999").status_code == 404
            # 正常：删除
            assert client.delete(f"/api/persons/{pid}/facts/{fid}").status_code == 200

            # 对象级设定：默认空 + PUT + 局部更新
            assert client.get(f"/api/persons/{pid}/persona").json()["goal"] == ""
            put = client.put(f"/api/persons/{pid}/persona",
                             json={"goal": "想约看展", "taboos": "宝贝"})
            assert put.status_code == 200
            assert put.json()["goal"] == "想约看展"
            put2 = client.put(f"/api/persons/{pid}/persona", json={"stage": "熟悉期"})
            assert put2.json()["goal"] == "想约看展" and put2.json()["stage"] == "熟悉期"
            assert client.get("/api/persons/p_nope/persona").status_code == 404
            assert client.put("/api/persons/p_nope/persona",
                              json={"goal": "x"}).status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_patch_chat_platform_endpoint() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            _seed_chat(client)
            resp = client.patch("/api/chats/qq:api", json={"platform": "wechat"})
            assert resp.status_code == 200, resp.text
            chat = resp.json()["chat"]
            assert chat["platform"] == "wechat"
            assert chat["channel"] == "wechat", "channel 要按 platform 重算"
            assert chat["id"] == "qq:api", "chat_id 绝不能变"
            # 刷新后仍然是微信
            assert client.get("/api/chats/qq:api").json()["platform"] == "wechat"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- HTTP：输出留存


def test_suggest_and_simulate_are_persisted() -> None:
    """跑一次指挥台 → 历史里能找到这次输出（含推演），并且能删掉。"""
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            import app.context as context_module

            ctx = context_module.get_ctx()
            ctx.update_cfg({"llm_provider": "mock", "embedder": "hash"})
            pid, _ids = _seed_chat(client)

            bundle = client.post("/api/chats/qq:api/suggest",
                                 json={"peer_message": "哈哈哈今天好累啊", "persist": False})
            assert bundle.status_code == 200, bundle.text
            run_id = bundle.json()["trace"].get("run_id")
            assert run_id, "建议返回里要能拿到 run_id，前端才接得上历史"

            got = client.get(f"/api/history/runs/{run_id}")
            assert got.status_code == 200, got.text
            assert got.json()["person_id"] == pid
            assert got.json()["options"], "留存的候选不能是空的"

            option = bundle.json()["options"][0]
            sim = client.post("/api/chats/qq:api/simulate", json={
                "option_text": option["text"], "option_id": option["id"],
                "run_id": run_id, "peer_message": "哈哈哈今天好累啊"})
            assert sim.status_code == 200, sim.text
            after = client.get(f"/api/history/runs/{run_id}").json()
            assert len(after["sim_runs"]) == 1, "推演要挂到这次运行上"

            # 对象历史能看到这次输出
            hist = client.get(f"/api/persons/{pid}/history").json()
            assert any(r["id"] == run_id for r in hist["runs"])

            # 边界：不存在的 run
            assert client.get("/api/history/runs/999999").status_code == 404
            assert client.delete("/api/history/runs/999999").status_code == 404
            assert client.delete(f"/api/history/runs/{run_id}").status_code == 200
            assert client.get(f"/api/history/runs/{run_id}").status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_import_writes_an_activity_trace() -> None:
    """导入成功要留一条 activity_log（kind=import）。"""
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            sample = BACKEND.parent / "samples" / "qq_sample_小鹿.txt"
            if not sample.exists():
                _skip("缺少示例文件，跳过导入痕迹用例")
                return
            with sample.open("rb") as fh:
                result = client.post(
                    "/api/import",
                    files={"file": (sample.name, fh, "text/plain")},
                    data={"chat_name": "小鹿"},
                )
            assert result.status_code == 200, result.text
            assert result.json()["inserted"] > 0, result.text
            chat_id = result.json()["chat_id"]
            import app.context as context_module

            store = context_module.get_ctx().store
            info = store.get_chat(chat_id)
            assert info is not None and info.person_id
            acts = store.list_activity(person_id=info.person_id, kind="import")
            assert acts and "导入" in acts[0].summary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [(name, obj) for name, obj in list(globals().items())
            if name.startswith("test_") and callable(obj)]


def main() -> int:
    print("=" * 68, flush=True)
    print("对象中心 S0：迁移 / 消息编辑 / 留存 / 对象级记忆（临时目录）", flush=True)
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
