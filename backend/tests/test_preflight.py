"""scripts/preflight.py 与结构化健康路由的回归测试。

两种跑法都支持：

    cd backend
    python -m pytest tests/test_preflight.py -q
    python tests/test_preflight.py

红线：**绝不碰真实的 backend/data/wingman.db**。
所有自检都跑在临时目录里造出来的假仓库上；健康路由测试把 config.DATA_DIR
指到临时目录之后再建 TestClient。真实数据库只做「前后哈希不变」的旁观校验。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
SCRIPT = PROJECT / "scripts" / "preflight.py"
REAL_DB = BACKEND / "data" / "wingman.db"

sys.path.insert(0, str(BACKEND))  # 直跑时也能 import app.*

try:  # pytest 可选：没有 pytest 时用本文件的 main() 直跑
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    # 只有真的在 pytest 里才用 pytest.skip —— 直跑模式下 pytest 可能装着但没跑，
    # 那时 raise Skipped 会直接掀翻整个测试进程。
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


# ---------------------------------------------------------------- 基础设施


def _run_preflight(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """用当前解释器跑 preflight，原始字节进出（编码相关断言不能被文本层糊掉）。"""
    run_env = dict(os.environ)
    run_env.pop("PYTHONIOENCODING", None)
    run_env.pop("PYTHONUTF8", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=run_env,
        cwd=str(PROJECT),
    )


def _decode_gbk(data: bytes) -> str:
    return data.decode("gbk", errors="replace")


def _decode_text(data: bytes) -> str:
    """默认 auto 模式下，输出接管道/重定向时用 UTF-8（PowerShell 7.4+ 的默认解码）。"""
    return data.decode("utf-8", errors="replace")


def make_fake_project(
    root: Path,
    *,
    with_frontend: bool = True,
    with_samples: bool = True,
    with_data_dir: bool = True,
) -> Path:
    """造一个「结构上像 WingMan 仓库」的临时目录，检查项可控。"""
    (root / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "app" / "main.py").write_text("# fake entry\n", encoding="utf-8")
    (root / "backend" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    if with_data_dir:
        (root / "backend" / "data").mkdir(parents=True, exist_ok=True)
        (root / "backend" / "data" / ".gitkeep").write_text("", encoding="utf-8")
    if with_frontend:
        (root / "frontend").mkdir(parents=True, exist_ok=True)
        (root / "frontend" / "index.html").write_text("<title>WingMan</title>", encoding="utf-8")
    if with_samples:
        (root / "samples").mkdir(parents=True, exist_ok=True)
        (root / "samples" / "demo.txt").write_text("hi\n", encoding="utf-8")
    return root


def snapshot_tree(root: Path) -> dict[str, list]:
    """文件/目录集合 + 大小 + 内容哈希 + mtime：同时抓「有没有变」和「怎么变的」。"""
    rows: dict[str, list] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            rows[rel] = ["dir"]
            continue
        try:
            data = path.read_bytes()
            stat = path.stat()
        except OSError:
            rows[rel] = ["unreadable"]
            continue
        rows[rel] = [len(data), hashlib.sha256(data).hexdigest(), stat.st_mtime_ns]
    return rows


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return "<missing>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_dir_readonly(path: Path) -> bool:
    """把目录变成「真的写不进去」。返回是否确认生效。

    Windows 上 os.access(dir, W_OK) 对 ACL 拒绝写的目录依然返回 True（实测），
    所以这里必须真写一次验证才算生效。
    """
    if os.name == "nt":
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant", f"{user}:(OI)(CI)(RX)",
             "/deny", f"{user}:(W,D,AD,WDAC,WO)"],
            capture_output=True,
        )
    else:
        os.chmod(path, 0o555)
    try:
        probe = path / ".write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return False
    except OSError:
        return True


def _restore_dir(path: Path) -> None:
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/reset", "/T"], capture_output=True)
    else:
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


def _rmtree(path: Path) -> None:
    _restore_dir(path)
    shutil.rmtree(path, ignore_errors=True)


def _load_preflight_module():
    """把 scripts/preflight.py 当模块加载（用于单元级断言），不影响子进程用例。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wingman_preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 需要模块已在 sys.modules 里
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- 自检核心


def test_json_contract_is_machine_readable_and_matches_exit_code() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_ok_"))
    try:
        make_fake_project(root)
        result = _run_preflight("--json", "--project-dir", str(root), "--port", "0")
        assert result.returncode == 0, _decode_text(result.stdout)[-800:]
        # 纯 ASCII：任何代码页下都能直接解析
        assert all(byte < 128 for byte in result.stdout), "JSON 必须是纯 ASCII 输出"
        payload = json.loads(result.stdout.decode("ascii"))

        assert payload["schema_version"] == 1
        assert payload["ok"] is True
        assert payload["blocked"] is False
        assert payload["status"] == "pass"
        assert payload["exit_code"] == 0
        assert payload["conclusion"] == "可以启动"
        assert payload["blocking_ids"] == []
        ids = [check["id"] for check in payload["checks"]]
        for expected in (
            "python_version", "backend_deps", "data_dir", "database", "port",
            "frontend", "samples", "optional_soundcard", "optional_faster_whisper", "config",
        ):
            assert expected in ids, f"缺少检查项 {expected}：{ids}"
        for check in payload["checks"]:
            assert check["status"] in ("pass", "warn", "fail")
        # 非阻断项永远不能是 fail —— 否则退出码语义会被顶坏
        for check in payload["checks"]:
            if check["status"] == "fail":
                assert check["blocking"] is True
    finally:
        _rmtree(root)


def test_human_output_says_ready_without_touching_database() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_human_"))
    try:
        make_fake_project(root)
        before = _file_hash(REAL_DB)
        result = _run_preflight("--project-dir", str(root), "--port", "0")
        assert result.returncode == 0
        text = _decode_text(result.stdout)
        assert "结论：可以启动" in text, text
        assert "Traceback" not in text
        # 假仓库里绝不能被自检建出数据库
        assert not (root / "backend" / "data" / "wingman.db").exists()
        assert sorted(p.name for p in (root / "backend" / "data").iterdir()) == [".gitkeep"]
        assert _file_hash(REAL_DB) == before, "测试污染了真实的 backend/data/wingman.db"
    finally:
        _rmtree(root)


def test_frontend_missing_is_blocking_with_actionable_hint() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_frontend_"))
    try:
        make_fake_project(root, with_frontend=False)
        result = _run_preflight("--json", "--project-dir", str(root), "--port", "0")
        assert result.returncode == 2, _decode_text(result.stdout)[-800:]
        payload = json.loads(result.stdout.decode("ascii"))
        assert payload["ok"] is False
        assert payload["blocked"] is True
        assert payload["status"] == "blocked"
        assert payload["blocking_ids"] == ["frontend"]
        assert payload["conclusion"] == "有 1 项必须先修"
        frontend = next(c for c in payload["checks"] if c["id"] == "frontend")
        assert frontend["status"] == "fail" and frontend["blocking"] is True
        assert "index.html" in frontend["detail"]
        assert ("git clone" in frontend["hint"]) or ("解压" in frontend["hint"])

        human = _decode_text(_run_preflight("--project-dir", str(root), "--port", "0").stdout)
        assert "结论：有 1 项必须先修" in human
        assert "修复建议" in human
        assert "Traceback" not in human
    finally:
        _rmtree(root)


def test_readonly_data_dir_is_blocking_with_actionable_hint() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_ro_"))
    try:
        data_dir = make_fake_project(root).joinpath("backend", "data")
        if not _make_dir_readonly(data_dir):
            _skip("当前环境无法把目录变成真正不可写（ACL 未生效），跳过只读目录用例")
        try:
            result = _run_preflight("--json", "--project-dir", str(root), "--port", "0")
            assert result.returncode == 2, _decode_text(result.stdout)[-800:]
            payload = json.loads(result.stdout.decode("ascii"))
            assert "data_dir" in payload["blocking_ids"], payload["blocking_ids"]
            check = next(c for c in payload["checks"] if c["id"] == "data_dir")
            assert check["status"] == "fail"
            assert check["data"]["writable"] is False
            assert "icacls" in check["hint"]
            human = _decode_text(_run_preflight("--project-dir", str(root), "--port", "0").stdout)
            assert "不可写" in human
            assert "Traceback" not in human
        finally:
            _restore_dir(data_dir)
    finally:
        _rmtree(root)


def test_occupied_port_is_blocking_with_actionable_hint() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_port_"))
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 故意学 uvicorn 设 SO_REUSEADDR：探测端不设，看是否仍能识别占用
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(8)
    port = int(holder.getsockname()[1])
    try:
        make_fake_project(root)
        result = _run_preflight("--json", "--project-dir", str(root), "--port", str(port))
        assert result.returncode == 2, _decode_text(result.stdout)[-800:]
        payload = json.loads(result.stdout.decode("ascii"))
        assert "port" in payload["blocking_ids"], payload["blocking_ids"]
        check = next(c for c in payload["checks"] if c["id"] == "port")
        assert check["status"] == "fail"
        assert check["data"]["occupied"] is True
        assert "netstat" in check["hint"]
        human = _decode_text(_run_preflight("--project-dir", str(root), "--port", str(port)).stdout)
        assert f"127.0.0.1:{port}" in human
        assert "修复建议" in human
        assert "Traceback" not in human
    finally:
        holder.close()
        _rmtree(root)


def test_warnings_alone_do_not_change_exit_code() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_warn_"))
    try:
        make_fake_project(root, with_samples=False)
        result = _run_preflight("--json", "--project-dir", str(root), "--port", "0")
        assert result.returncode == 0, _decode_text(result.stdout)[-800:]
        payload = json.loads(result.stdout.decode("ascii"))
        assert payload["ok"] is True and payload["blocked"] is False
        assert "samples" in payload["warning_ids"]
        assert payload["summary"]["failed"] == 0
        samples = next(c for c in payload["checks"] if c["id"] == "samples")
        assert samples["status"] == "warn"
        assert samples["blocking"] is False and samples["optional"] is True
    finally:
        _rmtree(root)


def test_two_runs_change_nothing_and_are_byte_identical() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_noside_"))
    try:
        make_fake_project(root)
        real_before = _file_hash(REAL_DB)
        before = snapshot_tree(root)
        first = _run_preflight("--json", "--project-dir", str(root), "--port", "0")
        second = _run_preflight("--json", "--project-dir", str(root), "--port", "0")
        after = snapshot_tree(root)

        assert first.returncode == 0 and second.returncode == 0
        assert before == after, f"自检产生了文件变化：{set(before) ^ set(after)}"
        assert first.stdout == second.stdout, "同一环境下两次自检输出必须逐字节相同"
        assert _file_hash(REAL_DB) == real_before
    finally:
        _rmtree(root)


def test_gbk_console_never_raises_unicode_error() -> None:
    ok_root = Path(tempfile.mkdtemp(prefix="wingman_preflight_gbk_ok_"))
    bad_root = Path(tempfile.mkdtemp(prefix="wingman_preflight_gbk_bad_"))
    try:
        make_fake_project(ok_root)
        make_fake_project(bad_root, with_frontend=False)
        # 1) 调用方强制 GBK（中文 Windows 控制台最常见的情形）：脚本必须自己扛住。
        #    --encoding console = 沿用流自身编码，正是「重定向到文件后是 GBK」的场景。
        gbk = {"PYTHONIOENCODING": "gbk:strict"}
        good = _run_preflight("--project-dir", str(ok_root), "--port", "0",
                              "--encoding", "console", env=gbk)
        assert good.returncode == 0
        assert b"UnicodeEncodeError" not in good.stderr
        text = _decode_gbk(good.stdout)
        assert "结论：可以启动" in text
        assert "\ufffd" not in text

        bad = _run_preflight("--project-dir", str(bad_root), "--port", "0",
                             "--encoding", "console", env=gbk)
        assert bad.returncode == 2
        assert b"UnicodeEncodeError" not in bad.stderr
        assert "修复建议" in _decode_gbk(bad.stdout)

        # 2) 默认 auto：管道输出走 UTF-8（PowerShell 7.4+ / CI 按 UTF-8 解码原生输出），
        #    调用方显式设了编码时则尊重调用方。
        auto = _run_preflight("--project-dir", str(ok_root), "--port", "0")
        assert auto.returncode == 0
        assert "结论：可以启动" in auto.stdout.decode("utf-8")  # 严格解码
        caller = _run_preflight("--project-dir", str(ok_root), "--port", "0", env=gbk)
        assert caller.returncode == 0
        assert "结论：可以启动" in caller.stdout.decode("gbk")  # 尊重 PYTHONIOENCODING

        # 3) 连中文都渲染不了的环境（cp1252）要降级成纯 ASCII，而不是吐乱码或崩溃
        c1252 = _run_preflight("--project-dir", str(ok_root), "--port", "0",
                               env={"PYTHONIOENCODING": "cp1252:strict"})
        assert c1252.returncode == 0
        assert b"UnicodeEncodeError" not in c1252.stderr
        assert all(byte < 128 for byte in c1252.stdout), "无法渲染中文时必须输出纯 ASCII"
        assert b"READY TO START" in c1252.stdout
    finally:
        _rmtree(ok_root)
        _rmtree(bad_root)


def test_encoding_flag_forces_utf8_for_piped_consumers() -> None:
    root = Path(tempfile.mkdtemp(prefix="wingman_preflight_utf8_"))
    try:
        make_fake_project(root)
        result = _run_preflight("--project-dir", str(root), "--port", "0", "--encoding", "utf-8")
        assert result.returncode == 0
        text = result.stdout.decode("utf-8")  # 严格解码：不是合法 UTF-8 就会抛
        assert "结论：可以启动" in text
        # ascii 模式：中文降级为其中的命令/路径，绝不吐乱码字节
        ascii_run = _run_preflight("--project-dir", str(root), "--port", "0", "--encoding", "ascii")
        assert ascii_run.returncode == 0
        assert all(byte < 128 for byte in ascii_run.stdout)
        assert b"RESULT: READY TO START" in ascii_run.stdout
        assert b"[OK]" in ascii_run.stdout and b"wingman.cmd" in ascii_run.stdout
    finally:
        _rmtree(root)


def test_missing_dependency_blocks_with_install_hint() -> None:
    """缺依赖时不能崩、不能给堆栈，要给「怎么装」的中文建议。"""
    module = _load_preflight_module()
    original = module._module_state

    def broken(module_name: str, dist_name: str, purpose: str) -> dict:
        row = original(module_name, dist_name, purpose)
        if module_name in ("fastapi", "pydantic_settings"):
            row.update({"ok": False, "error": "ModuleNotFoundError: No module named 'fastapi'", "version": ""})
        return row

    module._module_state = broken
    try:
        state = module.build_state(
            module.build_parser().parse_args(["--project-dir", str(PROJECT), "--port", "0"])
        )
        check = module.check_backend_deps(state)
    finally:
        module._module_state = original

    assert check.status == "fail"
    assert check.blocking is True
    assert "wingman.cmd --setup-only" in check.hint
    assert "backend" in check.hint
    assert "fastapi" in check.detail


def test_bad_arguments_exit_1_not_2() -> None:
    """参数错误属于「自检自身异常」= 1，绝不能和「有阻断项」= 2 抢语义。"""
    result = _run_preflight("--port", "not-a-number")
    assert result.returncode == 1
    assert b"Traceback" not in result.stdout
    assert b"Traceback" not in result.stderr
    help_result = _run_preflight("--help")
    assert help_result.returncode == 0
    assert b"--json" in help_result.stdout


# ---------------------------------------------------------------- 健康路由


def _client_with_temp_data_dir():
    """把 DATA_DIR 指到临时目录再建 TestClient，绝不碰真实数据库。"""
    from app import config

    tmp = Path(tempfile.mkdtemp(prefix="wingman_test_health_"))
    config.DATA_DIR = tmp
    config.get_settings.cache_clear()
    import app.context as context_module

    context_module.DATA_DIR = tmp
    context_module._CTX = None

    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import routes_health

    # routes_health 在 import 期就绑定了 DATA_DIR，这里同步成临时目录，
    # 保证用例断言的是「测试自己指定的数据目录」。
    routes_health.DATA_DIR = tmp
    return TestClient(app), tmp


def test_health_route_is_backward_compatible_and_structured() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:  # 依赖没装齐的解释器里跳过（preflight 本身仍可跑）
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    try:
        with client:
            legacy = client.get("/api/health")
            assert legacy.status_code == 200
            body = legacy.json()
            # e2e_check.py 依赖的四个字段，逐字段确认不破坏
            for field in ("version", "db", "counts", "providers"):
                assert field in body, f"/api/health 丢了字段 {field}"
            for provider in body["providers"]:
                assert set(provider) >= {"kind", "name", "available", "note"}

            details = client.get("/api/health/details")
            assert details.status_code == 200
            payload = details.json()
            for field in ("version", "ok", "paths", "env_files", "env_files_hit",
                          "runtime_override_keys", "config", "providers", "db",
                          "optional_deps", "notes"):
                assert field in payload, f"details 缺字段 {field}"
            assert payload["providers"] == body["providers"]
            # 数据路径必须落在临时目录里 —— 证明测试没有动真实数据库
            assert str(tmp) in payload["paths"]["db"]
            assert str(tmp) in payload["db"]["path"]
            assert payload["db"]["exists"] is True
            assert set(payload["db"]["counts"]) >= {"messages", "facts", "chats"}
            for key in ("soundcard", "faster_whisper"):
                assert key in payload["optional_deps"]
                assert payload["optional_deps"][key]["optional"] is True
            assert payload["paths"]["data"] == str(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_health_details_masks_secrets_and_lists_override_keys_only() -> None:
    try:
        client, tmp = _client_with_temp_data_dir()
    except ImportError as exc:
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return
    secret = "sk-verify-secret-1234567890"
    try:
        with client:
            import app.context as context_module

            ctx = context_module.get_ctx()
            ctx.update_cfg({"llm_api_key": secret, "llm_model": "gpt-4o-mini"})

            response = client.get("/api/health/details")
            assert response.status_code == 200
            raw = response.text
            assert secret not in raw, "响应里出现了明文密钥"
            payload = response.json()
            assert sorted(payload["runtime_override_keys"]) == ["llm_api_key", "llm_model"]
            entry = payload["config"]["llm_api_key"]
            assert entry["secret"] is True and entry["set"] is True
            assert entry["value"] and secret not in entry["value"]
            # 掩码不能泄漏长度：短密钥固定 ***，长密钥固定 6 个星号
            assert entry["value"] == "sk-v******890"
            assert payload["config"]["llm_model"]["value"] == "gpt-4o-mini"
            assert payload["config"]["llm_model"]["source"] == "runtime"

            # /api/health 也不许出现明文密钥
            assert secret not in client.get("/api/health").text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_source_resolution_and_masking_helpers() -> None:
    """配置来源优先级与密钥掩码：不往仓库里写任何文件，只针对纯函数断言。"""
    try:
        from app.api import routes_health as rh
    except ImportError as exc:
        _skip(f"缺少后端依赖，跳过路由用例：{exc}")
        return

    tmp = Path(tempfile.mkdtemp(prefix="wingman_test_dotenv_"))
    saved = os.environ.pop("LLM_PROVIDER", None)
    try:
        env_file = tmp / ".env"
        env_file.write_text(
            "LLM_PROVIDER=ollama\nLLM_API_KEY=whatever\n# 注释行\nEMPTY=\n", encoding="utf-8"
        )
        parsed = [(env_file, rh._parse_env_file(env_file))]
        assert sorted(parsed[0][1]) == ["EMPTY", "LLM_API_KEY", "LLM_PROVIDER"]

        value, source, _ = rh._resolve_key("llm_provider", {}, parsed)
        assert (value, source) == ("ollama", "dotenv")
        value, source, _ = rh._resolve_key("llm_provider", {"llm_provider": "mock"}, parsed)
        assert (value, source) == ("mock", "runtime"), "运行时覆盖必须压过 .env"
        value, source, _ = rh._resolve_key("port", {}, parsed)
        assert (value, source) == (8787, "default")
        value, source, _ = rh._resolve_key("whisper_model", {}, parsed)
        assert (value, source) == ("small", "default")

        # 掩码不泄漏长度：短密钥固定 ***，长密钥固定 6 个星号且不暴露长度
        assert rh._mask_value("short") == "***"
        assert rh._mask_value("sk-abcdefghijklmnop") == "sk-a******nop"
        assert rh._mask_value("") == ""

        rows, parsed_rows = rh._env_file_rows()
        paths = [row["path"] for row in rows]
        assert str(PROJECT / ".env") in paths and str(BACKEND / ".env") in paths
        for row in rows:
            assert row["exists"] is Path(row["path"]).is_file()
        assert len(parsed_rows) == len(rows)
    finally:
        if saved is not None:
            os.environ["LLM_PROVIDER"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [
        (name, obj)
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]


def main() -> int:
    # flush=True：重定向到文件时也能实时看到进度，不会因为块缓冲看不到卡在哪一条
    print("=" * 68, flush=True)
    print("WingMan preflight 回归测试（临时目录，不碰真实数据库）", flush=True)
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
