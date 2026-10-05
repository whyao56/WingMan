"""离线真实模型路径测试：scripts/openai_stub.py + app.llm.openai_compat 走真实 HTTP。

要证明的东西只有一句：**「文档教的接模型配置」真的通，而且不是悄悄退回 Mock。**

做法：拉起本地 stub（不联网、不要真实密钥），把 ctx 配成
`llm_provider=openai_compat` + `llm_base_url=http://127.0.0.1:<port>/v1`，
然后跑一次完整的「导入 → 建索引 → 分析 → 策略 → 建议」。
模式判定只认 provider 的 `name`（或 /api/health 的 providers）——
**不用 warnings**：warnings 是检索/建议告警（engine/suggestor.py、engine/context.py），
跟「当前是不是 Mock」没有任何关系。

两种跑法都支持：

    cd backend
    python -m pytest tests/test_model_path.py -q
    python tests/test_model_path.py

红线：用临时数据目录，绝不碰 backend/data/wingman.db。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
STUB = PROJECT / "scripts" / "openai_stub.py"
SAMPLE = PROJECT / "samples" / "qq_sample_小鹿.txt"
PEER_LINE = "哈哈哈今天好累啊"

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


# ---------------------------------------------------------------- 基础设施


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    if extra:
        env.update(extra)
    return env


def _terminate_tree(proc: subprocess.Popen) -> None:
    """结束整个进程树，别只杀外层。

    backend\\.venv\\Scripts\\python.exe 是个「跳板」：它会再起一个真实解释器
    （E:\\Python311\\python.exe）跑同一个脚本。只 terminate 外层跳板的话，
    真正的 stub 会继续活着、继续占着端口 —— 这正是「测试跑完端口还没释放」的来源。
    """
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
    try:
        proc.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _wait_port_released(port: int, timeout: float = 8.0) -> bool:
    """等端口彻底没人应答。

    用 connect 探测（比 bind 可靠：Windows 上 SO_REUSEADDR 会骗人）。
    注意要「轮询到释放」而不是「探一次就判」：进程树被 kill 后，
    子进程还会活几十毫秒，立刻探测会误报「进程没结束」。
    """
    deadline = time.time() + timeout
    while True:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


class StubProcess:
    """拉起 openai_stub.py 子进程，读它的 stdout 当请求日志用。"""

    def __init__(self, port: int | None = None, extra_args: tuple[str, ...] = (),
                 env: dict[str, str] | None = None) -> None:
        self.port = port or _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(STUB), "--port", str(self.port), *extra_args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=_clean_env(env), cwd=str(PROJECT),
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    @property
    def completion_count(self) -> int:
        return sum(1 for line in self.lines if f"POST /v1/chat/completions 200" in line)

    @property
    def rule_engine_count(self) -> int:
        return sum(1 for line in self.lines if "规则引擎" in line)

    def wait_ready(self, timeout: float = 25.0) -> None:
        import httpx

        deadline = time.time() + timeout
        last_error = ""
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise AssertionError(f"stub 提前退出（exit={self.proc.returncode}）：\n{self.output}")
                try:
                    resp = client.get(f"{self.base_url}/models",
                                      headers={"Authorization": "Bearer ready-probe"})
                    if resp.status_code == 200:
                        return
                    last_error = f"HTTP {resp.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.2)
        raise AssertionError(f"stub 在 {timeout:.0f}s 内没有就绪（{last_error}）：\n{self.output}")

    def stop(self) -> None:
        _terminate_tree(self.proc)
        self._reader.join(timeout=5)

    def __enter__(self) -> "StubProcess":
        self.wait_ready()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _fresh_ctx(base_url: str, api_key: str = "sk-stub-test",
               model: str = "wingman-stub") -> tuple[object, Path]:
    """临时数据目录 + openai_compat 配置的 ctx（绝不碰真实数据库）。"""
    # 走本地回环时不能被环境里的 HTTP 代理截胡：httpx 默认 trust_env=True，
    # 设了 http_proxy 的机器上 127.0.0.1 的请求也会被发给代理（e2e_check.py 踩过同一个坑）。
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    from app import config

    tmpdir = Path(tempfile.mkdtemp(prefix="wingman_test_model_"))
    config.DATA_DIR = tmpdir
    config.get_settings.cache_clear()

    import app.context as context_module

    context_module.DATA_DIR = tmpdir
    context_module._CTX = None
    ctx = context_module.get_ctx()
    ctx.update_cfg({
        "llm_provider": "openai_compat",
        "llm_base_url": base_url,
        "llm_api_key": api_key,
        "llm_model": model,
        "embedder": "hash",
        "asr_engine": "mock",
    })
    return ctx, tmpdir


def _load_stub_module():
    """把 scripts/openai_stub.py 当模块加载（用于单元级断言，不影响子进程用例）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wingman_openai_stub", STUB)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass/模块内自引用需要它已在 sys.modules
    spec.loader.exec_module(module)
    return module


def _parse(response) -> dict:
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"响应不是 JSON（HTTP {response.status_code}, "
            f"Content-Type={response.headers.get('content-type')}）：{response.text[:200]!r}"
        ) from exc


# ---------------------------------------------------------------- stub 协议面


def test_stub_protocol_surface_and_strictness() -> None:
    import httpx

    with StubProcess() as stub:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            auth = {"Authorization": "Bearer sk-any"}

            # GET /v1/models 需要 Bearer
            ok = client.get(f"{stub.base_url}/models", headers=auth)
            assert ok.status_code == 200
            assert ok.headers["content-type"] == "application/json; charset=utf-8"
            models = _parse(ok)
            assert models["object"] == "list"
            assert [m["id"] for m in models["data"]] == ["wingman-stub"]

            missing = client.get(f"{stub.base_url}/models")
            assert missing.status_code == 401, "没带 Bearer 竟然成功了 —— 这种 stub 证明不了任何配置"
            assert "error" in _parse(missing)

            # 路径不对必须 404，而不是兜底 200
            unknown = client.get(f"{stub.base_url}/embeddings", headers=auth)
            assert unknown.status_code == 404

            # 一个完整会话里连续跑：401 → 400 → 200，确认长连接没有错位
            body = {
                "model": "wingman-stub",
                "messages": [{"role": "system", "content": "[TASK:ANALYZE]\n分析"},
                             {"role": "user", "content": "【对方最新消息】\n哈哈哈今天好累啊"}],
            }
            no_auth = client.post(f"{stub.base_url}/chat/completions", json=body)
            assert no_auth.status_code == 401
            broken = client.post(f"{stub.base_url}/chat/completions", content=b"{not json",
                                 headers={**auth, "Content-Type": "application/json"})
            assert broken.status_code == 400, "非法 JSON 竟然被接受了"
            after = client.post(f"{stub.base_url}/chat/completions", json=body, headers=auth)
            assert after.status_code == 200, "401/400 之后的同一个长连接被协议错位污染了"
            assert _parse(after)["choices"][0]["message"]["content"]

    # 出了 with 才是「stub 已停止」；端口必须真的没人应答。
    # venv 的 python.exe 是跳板进程，只杀外层会让真正的 stub 继续占着端口（实测漏过 3 个）。
    assert _wait_port_released(stub.port), "stub 已停止但端口仍在应答：进程没被真正结束"


def test_chat_completions_shape_and_content_comes_from_rule_engine() -> None:
    import httpx

    from app.llm.mock import MockProvider

    system = "[TASK:SUGGEST]\n你是 WingMan 的回复建议器。"
    user = "【对方最新消息】\n哈哈哈今天好累啊"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    expected = asyncio.run(MockProvider().chat_raw(messages))

    with StubProcess() as stub:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.post(f"{stub.base_url}/chat/completions", headers={
                "Authorization": "Bearer sk-any", "Content-Type": "application/json",
            }, json={
                "model": "wingman-stub",
                "messages": messages,
                "temperature": 0.9,
                "max_tokens": 512,
                "stream": False,
                # 故意带上 response_format：stub 忽略它也必须正常返回
                "response_format": {"type": "json_object"},
            })
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/json; charset=utf-8"
            data = _parse(resp)

            assert data["object"] == "chat.completion"
            assert data["model"] == "wingman-stub"
            choice = data["choices"][0]
            assert choice["finish_reason"] == "stop"
            assert choice["message"]["role"] == "assistant"
            # 内容必须逐字等于规则引擎的输出：stub 不许自己发明回复
            assert choice["message"]["content"] == expected
            usage = data["usage"]
            assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
            assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
            payload = json.loads(choice["message"]["content"])
            assert len(payload["options"]) >= 3


def test_untagged_request_gets_plain_reply_not_a_crash() -> None:
    """控制台「测试连通」发的是没有任务标记的普通 prompt —— 不能让它变成红叉。"""
    import httpx

    with StubProcess() as stub:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            resp = client.post(f"{stub.base_url}/chat/completions", headers={
                "Authorization": "Bearer sk-any",
            }, json={"model": "wingman-stub", "messages": [
                {"role": "system", "content": "你是一个测试助手。"},
                {"role": "user", "content": "只回复两个字：正常"},
            ]})
            assert resp.status_code == 200
            content = _parse(resp)["choices"][0]["message"]["content"]
            assert content.strip()


# ---------------------------------------------------------------- 关键：真实链路


def test_full_analysis_over_real_http_uses_openai_compat() -> None:
    import httpx  # noqa: F401 - 确保依赖在，失败信息更直白

    from app.adapters import registry
    from app.engine import pipeline
    from app.memory import profiler

    with StubProcess() as stub:
        ctx, tmpdir = _fresh_ctx(stub.base_url)
        try:
            # (a) 配置成 openai_compat 就必须是 openai_compat —— 不许静默回退 mock
            assert ctx.llm.name == "openai_compat", (
                f"provider 变成了 {ctx.llm.name}：配置没被采纳，或发生了静默回退"
            )
            assert ctx.llm.base_url == stub.base_url
            assert ctx.llm.available is True

            assert SAMPLE.exists(), f"示例文件缺失：{SAMPLE}"
            imported = registry.import_file(ctx.store, SAMPLE, chat_name="小鹿")
            assert imported.inserted > 40, f"只导入了 {imported.inserted} 条"
            chat_id = imported.chat_id
            assert asyncio.run(profiler.build_index(ctx, chat_id)) > 0

            # 完整「分析 + 策略 + 建议」：三次真实 HTTP 调用
            bundle = asyncio.run(pipeline.run_analysis(ctx, chat_id, PEER_LINE))

            # 模式判定只从 name / trace 读，不碰 warnings
            assert ctx.llm.name == "openai_compat"
            assert bundle.trace.get("llm") == "openai_compat", bundle.trace

            assert bundle.analysis.emotion, "分析结果为空"
            assert bundle.analysis.signals, "没有观测信号"
            assert bundle.strategy.goal_this_turn, "策略为空"

            # (b) >=3 条候选，且每条都有打分依据
            assert len(bundle.options) >= 3, f"只生成了 {len(bundle.options)} 条建议"
            for option in bundle.options:
                assert option.text.strip(), f"候选 {option.id} 文本为空"
                assert option.score_notes, f"候选 {option.id} 缺少打分依据"
                assert option.total > 0, f"候选 {option.id} 总分为 0"

            # (c) 真实 HTTP：stub 至少收到 3 次 chat/completions（ANALYZE/STRATEGY/SUGGEST）
            assert stub.completion_count >= 3, (
                f"stub 只收到 {stub.completion_count} 次请求，链路可能没走 HTTP：\n{stub.output}"
            )
            assert stub.rule_engine_count >= 3, "回复不是规则引擎产生的"
            assert "没有产出可用的回复建议" not in " ".join(bundle.warnings)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def test_health_endpoint_reports_openai_compat_provider() -> None:
    """服务运行时 /api/health 的 providers 必须如实显示 openai_compat（t5 的验收点）。"""
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except ImportError as exc:
        _skip(f"缺少后端依赖，跳过健康检查用例：{exc}")
        return

    with StubProcess() as stub:
        ctx, tmpdir = _fresh_ctx(stub.base_url)
        try:
            with TestClient(app) as client:
                health = client.get("/api/health")
                assert health.status_code == 200
                providers = {p["kind"]: p for p in health.json()["providers"]}
                llm = providers["llm"]
                assert llm["name"] == "openai_compat", f"providers 里 llm={llm}"
                assert llm["available"] is True
                assert stub.base_url in llm["note"]

                details = client.get("/api/health/details").json()
                assert details["config"]["llm_provider"]["value"] == "openai_compat"
                assert details["config"]["llm_provider"]["source"] == "runtime"
                assert details["config"]["llm_base_url"]["value"] == stub.base_url
                # 密钥只允许掩码出现
                assert "sk-stub-test" not in json.dumps(details, ensure_ascii=False)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def test_provider_ping_and_model_listing_through_stub() -> None:
    with StubProcess() as stub:
        ctx, tmpdir = _fresh_ctx(stub.base_url)
        try:
            ok, message = asyncio.run(ctx.llm.ping())
            assert ok is True, message
            assert message.strip()
            assert asyncio.run(ctx.llm.list_models()) == ["wingman-stub"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def test_unreachable_endpoint_fails_loudly_instead_of_mock_fallback() -> None:
    """反向证明：端点不通时必须报错，而不是悄悄用 Mock 把流程跑完。"""
    from app.llm.base import LLMError

    dead_port = _free_port()  # 没有任何进程监听
    ctx, tmpdir = _fresh_ctx(f"http://127.0.0.1:{dead_port}/v1")
    try:
        assert ctx.llm.name == "openai_compat", "配置成 openai_compat 却换了 provider"
        try:
            asyncio.run(ctx.llm.complete_json("[TASK:ANALYZE]\n", "分析这段对话"))
        except LLMError as exc:
            text = str(exc)
            assert ("网络错误" in text) or ("超时" in text), f"错误信息不可操作：{text}"
        else:
            raise AssertionError("端点不可达时没有报错 —— 说明发生了静默回退")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 离线与编码


def test_ctrl_c_stops_with_exit_code_zero() -> None:
    """Ctrl+C 语义：Ctrl+C 必须干净退出（0），而不是抛栈或留下监听端口。"""
    module = _load_stub_module()
    original = module.StubServer.serve_forever

    def fake_serve_forever(self) -> None:  # noqa: ANN001
        raise KeyboardInterrupt

    module.StubServer.serve_forever = fake_serve_forever
    try:
        code = module.main(["--port", str(_free_port()), "--quiet"])
    finally:
        module.StubServer.serve_forever = original
    assert code == 0, f"Ctrl+C 退出码应为 0，实际 {code}"


def test_stub_is_offline_only_by_construction() -> None:
    """静态自证：stub 里没有任何 HTTP 客户端代码，不可能发起外网请求。"""
    source = STUB.read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import requests", "urllib.request", "socket.create_connection"):
        assert forbidden not in source, f"stub 里出现了对外请求能力：{forbidden}"
    assert "http.server" in source, "stub 应该只用标准库 http.server 提供本地服务"
    # 默认只绑本机回环，且 client 侧用的就是 127.0.0.1 字面量（不解析域名）
    assert 'DEFAULT_HOST = "127.0.0.1"' in source


def test_stub_output_survives_gbk_console() -> None:
    """中文 Windows（GBK）下启动提示不能抛 UnicodeEncodeError。"""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(STUB), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_clean_env({"PYTHONIOENCODING": "gbk:strict"}), cwd=str(PROJECT),
    )
    try:
        import httpx

        deadline = time.time() + 25
        ready = False
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            while time.time() < deadline:
                try:
                    if client.get(f"http://127.0.0.1:{port}/v1/models",
                                  headers={"Authorization": "Bearer x"}).status_code == 200:
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
        assert ready, "stub 在 GBK 环境下没能起来"
    finally:
        _terminate_tree(proc)
        try:
            out, _ = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            out = b""
    assert _wait_port_released(port), "GBK 用例结束后端口仍在应答：进程没被真正结束"

    assert b"UnicodeEncodeError" not in out, out[-400:]
    text = out.decode("gbk")  # 严格解码：能解出来说明字节就是合法 GBK，没有乱码
    assert "WingMan 离线 OpenAI 兼容 stub 已就绪" in text
    assert f"--port {port}" in text or str(port) in text


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [
        (name, obj)
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]


def main() -> int:
    print("=" * 68, flush=True)
    print("WingMan 离线模型路径测试（stub + openai_compat，真实 HTTP，临时库）", flush=True)
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
