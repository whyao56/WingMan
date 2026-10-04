#!/usr/bin/env python
"""WingMan 启动前自检（doctor / preflight）。

用法：

    python scripts/preflight.py                 # 中文体检报告（人读）
    python scripts/preflight.py --json          # 机器可读 JSON（纯 ASCII，供启动器 / CI 消费）
    python scripts/preflight.py --port 8788     # 检查指定端口（默认 8787，传 0 表示跳过）
    python scripts/preflight.py --project-dir D:\\somewhere\\WingMan
    python scripts/preflight.py --encoding console   # cmd 重定向到文件、要用 type 查看时

三条设计红线（改动前请先想清楚代价）：

1. **零依赖**：只 import 标准库。它要在「依赖没装齐、服务起不来」时当诊断工具用，
   自己先因为 import 失败而崩掉就毫无意义。
2. **零副作用**：不创建/迁移数据库、不建目录、不写 marker、不改配置、不长占端口
   （只 bind 探测后立即释放）。唯一允许的临时写入是「数据目录可写性探针」：
   Windows 上 ACL 明确拒绝写入时 os.access(dir, W_OK) 仍然返回 True（实测），
   不真写一次根本测不出只读目录。探针文件 0 字节、写完立刻删除、并恢复目录时间戳。
   所有可能卡住的文件系统调用都带超时看门狗 —— 自检自己绝不能挂住。
3. **中文 Windows 不乱码**：自己处理 stdout/stderr 编码（errors 兜底），
   不依赖调用方设 PYTHONUTF8/PYTHONIOENCODING。默认策略：连着真实控制台就用
   控制台编码（cmd 的 GBK 控制台照常显示中文）；输出接了管道/重定向就用 UTF-8
   （PowerShell 7.4+ 与各类采集端默认按 UTF-8 解码原生输出，用 GBK 字节喂它们
   才会真的乱码）；调用方显式设了 PYTHONIOENCODING 就尊重调用方。环境渲染不了
   中文时降级为纯 ASCII 输出（保留命令与路径），绝不吐乱码字节。
   需要精确控制时用 --encoding auto|utf-8|console|ascii，--json 永远是纯 ASCII。

退出码（与启动器契约对齐）：

    0   关键项全部通过（提醒项不影响）
    2   存在阻断项 —— 启动器可在「零副作用」阶段据此拒绝启动并展示修复建议
    1   自检自身异常（没跑完，不能据此判定环境好坏）

--json 顶层字段契约（schema_version = 1，字段只增不改）：

    ok        bool   退出码是否为 0
    blocked   bool   是否存在阻断项（等价于 exit_code == 2）
    status    str    "pass" | "blocked" | "error"
    exit_code int    进程退出码
    conclusion str   中文结论（"可以启动" / "有 N 项必须先修"）
    checks    list   [{id,title,status,blocking,optional,detail,hint,data}]
                     status: "pass" | "warn" | "fail"；只有 blocking 且 fail 的项才阻断启动
    blocking_ids list 触发阻断的检查 id
    environment dict Python / 平台 / 控制台编码等环境信息

JSON 一律 ensure_ascii（纯 ASCII 字节流），因此在 GBK / UTF-8 / 任何代码页下
都能直接 json.loads，不受控制台编码影响。自检结论是确定性的：同一环境下
连续运行两次，stdout 逐字节相同（不含时间戳），便于 diff 与哈希比对。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import json
import os
import socket
import sqlite3
import sys
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

# ---------------------------------------------------------------- 常量

SCHEMA_VERSION = 1
TOOL_NAME = "wingman-preflight"
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_BLOCKED = 2

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

MIN_PYTHON = (3, 11)

SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_PROJECT_DIR = SCRIPT_PATH.parents[1]

# (import 名, 发行包名, 用途)
REQUIRED_MODULES: tuple[tuple[str, str, str], ...] = (
    ("fastapi", "fastapi", "HTTP 框架"),
    ("uvicorn", "uvicorn", "ASGI 服务器"),
    ("pydantic", "pydantic", "数据模型 / 校验"),
    ("pydantic_settings", "pydantic-settings", "读取 .env 配置"),
    ("httpx", "httpx", "模型调用与端到端检查的 HTTP 客户端"),
    ("numpy", "numpy", "向量检索"),
    ("multipart", "python-multipart", "上传文件解析"),
)

OPTIONAL_MODULES: tuple[tuple[str, str, str], ...] = (
    ("soundcard", "soundcard", "通话 / 系统声音采集（可选）"),
    ("faster_whisper", "faster-whisper", "本地语音转写（可选）"),
)

REQUIRED_TABLES: tuple[str, ...] = (
    "chats",
    "messages",
    "embeddings",
    "facts",
    "summaries",
    "personas",
    "kv",
    "voice_log",
)

# 打印前要检查能否编码的探针字符串
_PROBE_ZH = "中文测试"

ASCII_ONLY = False  # 由 _configure_stdio() 设置：True = 环境渲染不了中文，降级 ASCII


# ---------------------------------------------------------------- 输出编码


def _can_encode(encoding: str | None, text: str) -> bool:
    if not encoding:
        return False
    try:
        text.encode(encoding, "strict")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _prepare_stream(stream: Any, forced_encoding: str | None = None) -> bool:
    """给一个流加上 errors=replace 兜底，并返回它能否渲染中文。"""
    encoding = forced_encoding or getattr(stream, "encoding", None)
    ok = _can_encode(encoding, _PROBE_ZH)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            # 关键：默认保留控制台原编码（GBK 控制台下中文本来就正常），只把错误策略换成
            # replace。这样即使后面漏了某个非 GBK 字符，也只会变成 '?'，不会抛
            # UnicodeEncodeError 把自检本身打断。
            if forced_encoding:
                reconfigure(encoding=forced_encoding, errors="replace")
            else:
                reconfigure(errors="replace")
        except Exception:
            pass
    return ok


def _configure_stdio(mode: str = "auto") -> None:
    """自检入口第一步：不依赖调用方设置，自己保证 stdout/stderr 不会因编码崩掉。

    mode 取值：
    - auto   ：默认。连着真实控制台就沿用控制台编码（cmd 的 GBK 控制台由
               WriteConsoleW 正常显示中文）；输出是管道/重定向文件则改用 UTF-8 ——
               PowerShell 7.4+ 与各类采集端默认按 UTF-8 解码原生输出，用 GBK
               字节喂它们才会真的乱码。调用方显式设了 PYTHONIOENCODING/PYTHONUTF8
               时，auto 尊重调用方的选择。
    - utf-8  ：强制 UTF-8（给启动器/CI 明确指定）。
    - console：强制沿用流自身编码（cmd 里 `python preflight.py > out.txt` 之后
               用 type 查看时选它，落到文件里就是 GBK）。
    - ascii  ：强制纯 ASCII 输出（中文说明降级为其中的命令与路径）。

    无论哪种模式都会把 errors 换成 replace：任何漏网字符只会变 '?'，
    绝不会抛 UnicodeEncodeError 把自检本身打断。
    """
    global ASCII_ONLY
    forced = {"utf-8": "utf-8", "ascii": "ascii"}.get(mode)
    caller_forced = bool(os.environ.get("PYTHONIOENCODING") or os.environ.get("PYTHONUTF8"))

    stdout_ok = False
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        target = forced
        if target is None and mode == "auto" and not caller_forced and not _is_console(stream):
            target = "utf-8"
        ok = _prepare_stream(stream, target)
        if name == "stdout":
            stdout_ok = ok
    ASCII_ONLY = not stdout_ok


def _is_console(stream: Any) -> bool:
    """判断一个流是否直接连着真实控制台（而不是管道 / 文件）。"""
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _prescan_encoding(argv: list[str]) -> str:
    """在正式解析参数前先拿 --encoding，保证连 argparse 的报错都不会乱码。"""
    for index, token in enumerate(argv):
        if token.startswith("--encoding="):
            return token.split("=", 1)[1]
        if token == "--encoding" and index + 1 < len(argv):
            return argv[index + 1]
    return "auto"


def _ascii(text: str) -> str:
    """ASCII 降级：只保留可打印 ASCII（命令、路径、版本号都在），中文说明退化为空白。

    这是给「控制台连中文都渲染不了」的环境（例如 cp437 / cp1252）准备的兜底，
    宁可少说几句，也不输出乱码字节。
    """
    kept = [ch if (ch.isascii() and (ch.isprintable() or ch == " ")) else " " for ch in text]
    return " ".join("".join(kept).split())


def t(zh: str, ascii_fallback: str | None = None) -> str:
    """按当前渲染能力返回中文或 ASCII 文本。"""
    if not ASCII_ONLY:
        return zh
    return ascii_fallback if ascii_fallback is not None else _ascii(zh)


def _display_width(text: str) -> int:
    """东亚全角字符按 2 列算，保证中文表格对齐。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _print(text: str = "") -> None:
    print(text)


def _eprint(text: str = "") -> None:
    print(text, file=sys.stderr)


# ---------------------------------------------------------------- 数据模型


@dataclass
class Check:
    id: str
    title: str
    title_en: str
    status: str
    blocking: bool = False
    optional: bool = False
    detail: str = ""
    hint: str = ""
    hint_en: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.status == STATUS_FAIL

    @property
    def blocks_start(self) -> bool:
        return self.status == STATUS_FAIL and self.blocking

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "title_en": self.title_en,
            "status": self.status,
            "blocking": bool(self.blocking),
            "optional": bool(self.optional),
            "detail": self.detail,
            "hint": self.hint,
            "data": self.data,
        }


@dataclass
class State:
    """一次自检的输入与环境快照。"""

    project_dir: Path
    backend_dir: Path
    data_dir: Path
    frontend_dir: Path
    samples_dir: Path
    db_path: Path
    config_py: Path
    env_files: list[Path]
    host: str
    port: int
    port_from_cli: bool
    config_info: dict[str, Any] = field(default_factory=dict)
    env_parsed: list[tuple[Path, dict[str, str]]] = field(default_factory=list)
    kv_overrides: dict[str, str] = field(default_factory=dict)
    interpreter: dict[str, Any] = field(default_factory=dict)

    def env_file_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.env_files:
            parsed = dict(self.env_parsed).get(path, {})
            rows.append({
                "path": str(path),
                "exists": path.is_file(),
                "variables": sorted(parsed.keys()),
                "variable_count": len(parsed),
            })
        return rows


# ---------------------------------------------------------------- 工具


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：只关心 KEY=VALUE 的键名与值（与 pydantic-settings 的口径一致）。"""
    out: dict[str, str] = {}
    try:
        text = _read_text(path)
    except OSError:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        out[key.upper()] = value
    return out


def _literal_or_none(node: ast.AST) -> Any:
    """把 AST 节点转成字面量，额外支持 frozenset({...}) / set(...) / tuple(...)。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("frozenset", "set", "tuple") and len(node.args) == 1:
            inner = _literal_or_none(node.args[0])
            if inner is None:
                return None
            return tuple(inner) if node.func.id == "tuple" else frozenset(inner)
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _parse_config_py(path: Path) -> dict[str, Any]:
    """用 ast 读 config.py 的默认值与键集合。

    刻意不 import app.config：那样会要求 pydantic / pydantic-settings 已安装，
    而本脚本最重要的使用场景恰恰是「依赖缺了」。ast 只读源码，零依赖零副作用。
    """
    info: dict[str, Any] = {
        "ok": False,
        "defaults": {},
        "editable": [],
        "secrets": [],
        "error": "",
    }
    try:
        tree = ast.parse(_read_text(path))
    except OSError as exc:
        info["error"] = f"读取失败：{exc}"
        return info
    except SyntaxError as exc:
        info["error"] = f"语法错误：{exc}"
        return info

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.value is not None
                ):
                    value = _literal_or_none(stmt.value)
                    if value is not None:
                        info["defaults"][stmt.target.id] = value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id not in ("EDITABLE_KEYS", "SECRET_KEYS"):
                    continue
                value = _literal_or_none(node.value)
                if value is None:
                    continue
                if target.id == "EDITABLE_KEYS":
                    info["editable"] = [str(v) for v in value]
                else:
                    info["secrets"] = [str(v) for v in value]
    info["ok"] = bool(info["defaults"])
    return info


_SECRET_NAME_HINTS = ("api_key", "apikey", "secret", "token", "password", "passwd")


def _is_secret_key(key: str, state: State | None = None) -> bool:
    secrets = set((state.config_info or {}).get("secrets") or ()) if state else set()
    if key in secrets:
        return True
    low = key.lower()
    return any(hint in low for hint in _SECRET_NAME_HINTS)


def _mask_value(value: str) -> str:
    """密钥掩码：沿用 config.mask_secret 的形如 sk-abc***xyz 口径，但不泄漏长度。

    mask_secret 对长度 <= 8 的值返回 "*" * len(value)，那等于把密钥长度直接说出来；
    这里对短密钥统一用固定的 "***"，长密钥才沿用 mask_secret 的展示形式。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}{'*' * 6}{value[-3:]}"


def _resolve(state: State, key: str) -> tuple[Any, str, str]:
    """按真实优先级解析一个配置键：运行时覆盖 > 进程环境变量 > .env > 代码默认值。

    返回 (值, 来源 id, 来源说明)。来源 id 取值：runtime / env / dotenv / default / unknown。
    """
    override = state.kv_overrides.get(key)
    if override:
        return override, "runtime", "控制台写入的运行时覆盖（数据库 kv 表）"

    env_value = os.environ.get(key.upper())
    if env_value:
        return env_value, "env", f"环境变量 {key.upper()}"

    # 后一个 .env 覆盖前一个，与 config.py 的 env_file=(项目根/.env, backend/.env) 一致
    for path, parsed in reversed(state.env_parsed):
        if key.upper() in parsed:
            return parsed[key.upper()], "dotenv", f".env 文件 {path}"

    defaults = (state.config_info or {}).get("defaults") or {}
    if key in defaults:
        return defaults[key], "default", "backend/app/config.py 里的默认值"
    return None, "unknown", "未在配置中找到该键"


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_bool_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _module_state(module_name: str, dist_name: str, purpose: str) -> dict[str, Any]:
    """真实 import 一次（比 find_spec 更接近「能不能用」），失败也不抛异常。"""
    row: dict[str, Any] = {
        "module": module_name,
        "distribution": dist_name,
        "purpose": purpose,
        "ok": False,
        "version": "",
        "error": "",
    }
    try:
        module = importlib.import_module(module_name)
    except BaseException as exc:  # noqa: BLE001 - 任何 import 期异常都算「不可用」
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    row["ok"] = True
    version = str(getattr(module, "__version__", "") or "")
    if not version:
        try:
            version = importlib.metadata.version(dist_name)
        except Exception:
            version = ""
    row["version"] = version
    return row


@contextmanager
def _dir_timestamp_guard(directory: Path) -> Iterator[None]:
    """记录并恢复目录时间戳。

    自检过程中的探针文件（可写性探测）与 SQLite sidecar 清理都会改动目录 mtime；
    恢复之后，「连续两次运行零文件变化」这条验收才真的成立。
    """
    state, stat_result = _call_with_timeout(directory.stat, 3.0)
    if state != "ok":
        stat_result = None
    try:
        yield
    finally:
        if stat_result is not None:
            _call_with_timeout(
                lambda: os.utime(
                    directory, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns)
                ),
                3.0,
            )


PROBE_TIMEOUT = 5.0


def _call_with_timeout(function: Callable[[], Any], timeout: float) -> tuple[str, Any]:
    """在守护线程里跑一个可能卡住的文件系统调用。返回 ("ok", 值) / ("error", 异常) / ("timeout", None)。

    为什么需要看门狗：本机实测，数据目录被 ACL 拒绝写入时 `tempfile.mkstemp()`
    会**直接卡死**而不是抛 PermissionError。自检宁可报「探测超时」，
    也不能陪着一起挂住 —— doctor 挂住比报错更难排查。
    """
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = function()
        except BaseException as exc:  # noqa: BLE001 - 连 KeyboardInterrupt 都收进盒子
            box["error"] = exc

    thread = threading.Thread(target=worker, name="wingman-preflight-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return "timeout", None
    if "error" in box:
        return "error", box["error"]
    return "ok", box.get("value")


def _probe_writable(directory: Path) -> tuple[str, str]:
    """真写一个 0 字节探针文件来判断目录可写，写完立刻删除。返回 (状态, 原因)。

    状态取值：writable / denied / timeout。

    为什么不用 os.access：Windows 上 ACL 明确拒绝写入时 os.access(dir, W_OK)
    依然返回 True（本机实测），只查权限位会漏报「data 目录只读」这种故障。
    为什么不用 tempfile.mkstemp：同一场景下它会卡死（实测）。
    这里用普通 create（不带 O_EXCL，不需要排他语义）+ 看门狗 + 唯一文件名。
    """
    probe = directory / f".wingman_preflight_{uuid.uuid4().hex}.tmp"

    def attempt() -> None:
        fd = os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)

    state, payload = _call_with_timeout(attempt, PROBE_TIMEOUT)
    if state == "timeout":
        return "timeout", f"写入探测 {PROBE_TIMEOUT:.0f} 秒没有返回（文件系统或安全软件可能卡住了）"
    if state == "error":
        if isinstance(payload, OSError):
            return "denied", f"{type(payload).__name__}: {payload.strerror or payload}"
        return "denied", f"{type(payload).__name__}: {payload}"
    # 清理探针文件也要限时，避免在这里二次卡住
    _call_with_timeout(lambda: probe.unlink(missing_ok=True), 3.0)
    return "writable", ""


def _port_probe(host: str, port: int) -> dict[str, Any]:
    """纯 bind 探测：成功说明端口空闲，失败说明被占用。

    刻意不设 SO_REUSEADDR —— Windows 上它会让 bind 在被占用时也成功，
    那样根本测不出端口冲突（实测：占用方设了 SO_REUSEADDR 时，普通 bind 会以
    WinError 10048 失败，正是我们要的信号）。
    """
    result: dict[str, Any] = {"host": host, "port": port, "bind_ok": False, "error": "", "errno": None}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        result["bind_ok"] = True
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc.strerror or exc}"
        result["errno"] = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
    finally:
        sock.close()  # 立即释放：自检不允许长时间占用端口
    return result


def _probe_existing_service(host: str, port: int) -> dict[str, Any] | None:
    """端口被占用时，试着问一句「是不是 WingMan 已经在跑了」。

    只读 GET，1.5 秒超时，显式绕开环境变量里的 HTTP 代理
    （设了 http_proxy 时，本地请求会被代理吃掉，e2e_check.py 踩过同一个坑）。
    """
    import urllib.error
    import urllib.request

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{probe_host}:{port}/api/health"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, headers={"User-Agent": "wingman-preflight"})
        with opener.open(request, timeout=1.5) as response:
            raw = response.read(8192)
    except Exception:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    if isinstance(payload, dict) and "version" in payload and "providers" in payload:
        return payload
    return None


# ---------------------------------------------------------------- 各检查项


def check_project_layout(state: State) -> Check:
    required = [state.backend_dir / "app" / "main.py", state.backend_dir / "requirements.txt"]
    missing = [p for p in required if not p.is_file()]
    if not missing:
        return Check(
            "project_layout",
            "项目结构",
            "project layout",
            STATUS_PASS,
            blocking=True,
            detail="backend/app/main.py 与 backend/requirements.txt 都在",
            data={"project_dir": str(state.project_dir), "missing": []},
        )
    names = "、".join(str(p) for p in missing)
    return Check(
        "project_layout",
        "项目结构",
        "project layout",
        STATUS_FAIL,
        blocking=True,
        detail=f"不像完整的 WingMan 仓库，缺少：{names}",
        hint=(
            f"确认仓库根目录是否正确（当前推断为 {state.project_dir}）。"
            "如果放在别处，请用 --project-dir 指定；也可以重新 git clone 或重新解压发行包。"
        ),
        hint_en=(
            f"Check the repository root (detected: {state.project_dir}). "
            "Use --project-dir, or re-clone / re-extract the release archive."
        ),
        data={"project_dir": str(state.project_dir), "missing": [str(p) for p in missing]},
    )


def check_python_version(state: State) -> Check:
    current = sys.version_info
    version = f"{current.major}.{current.minor}.{current.micro}"
    ok = (current.major, current.minor) >= MIN_PYTHON
    needed = ".".join(str(v) for v in MIN_PYTHON)
    ctx = state.interpreter
    where = ctx.get("kind_label", "")
    detail = f"{version}（需要 >= {needed}）· {where}"
    data = {
        "version": version,
        "required": f">={needed}",
        "executable": sys.executable,
        "interpreter_kind": ctx.get("kind", "unknown"),
    }
    if ok:
        return Check(
            "python_version", "Python 版本", "python version", STATUS_PASS,
            blocking=True, detail=detail, data=data,
        )
    return Check(
        "python_version", "Python 版本", "python version", STATUS_FAIL,
        blocking=True,
        detail=detail,
        hint=(
            f"当前 Python {version} 低于最低要求 {needed}。"
            "请安装 Python 3.11 或更高版本（https://www.python.org/downloads/），"
            "安装时勾选 Add python.exe to PATH，然后重新运行 wingman.cmd。"
        ),
        hint_en=(
            f"Python {version} is older than the required {needed}. "
            "Install Python 3.11+ (python.org) and re-run wingman.cmd."
        ),
        data=data,
    )


def check_backend_deps(state: State) -> Check:
    rows = [_module_state(name, dist, purpose) for name, dist, purpose in REQUIRED_MODULES]
    missing = [row for row in rows if not row["ok"]]
    data = {
        "required": {row["module"]: {"ok": row["ok"], "version": row["version"], "error": row["error"]}
                     for row in rows},
        "missing": [row["module"] for row in missing],
        "interpreter_kind": state.interpreter.get("kind", "unknown"),
        "project_venv": state.interpreter.get("venv_path", ""),
    }
    if not missing:
        versions = "、".join(f"{row['module']} {row['version']}" for row in rows if row["version"])
        return Check(
            "backend_deps", "后端依赖", "backend deps", STATUS_PASS,
            blocking=True,
            detail=f"{len(rows)}/{len(rows)} 个必需依赖可导入" + (f"（{versions}）" if versions else ""),
            data=data,
        )
    names = "、".join(f"{row['module']}（{row['distribution']}）" for row in missing)
    first_error = next((row["error"] for row in missing if row["error"]), "")
    venv_python = state.backend_dir / ".venv" / "Scripts" / "python.exe"
    hint = (
        f"缺少 {len(missing)} 个必需依赖：{names}。"
        f"在仓库根目录运行 wingman.cmd --setup-only 即可自动创建 {state.backend_dir / '.venv'} 并安装依赖；"
        "如果你是在用系统 Python 手工调试，请改用该虚拟环境里的 python.exe 运行本脚本。"
    )
    if venv_python.exists() and state.interpreter.get("kind") != "project-venv":
        hint += f" 当前解释器不是项目虚拟环境，但 {venv_python} 已存在，可直接用它重跑。"
    return Check(
        "backend_deps", "后端依赖", "backend deps", STATUS_FAIL,
        blocking=True,
        detail=f"{len(missing)}/{len(rows)} 个必需依赖不可用：{names}" + (f" · 首个错误 {first_error}" if first_error else ""),
        hint=hint,
        hint_en=(
            f"Missing required dependencies: {', '.join(row['module'] for row in missing)}. "
            "Run wingman.cmd --setup-only in the repository root, or use the interpreter "
            "from backend/.venv."
        ),
        data=data,
    )


def check_data_dir(state: State) -> Check:
    directory = state.data_dir
    exists = directory.exists()
    data: dict[str, Any] = {
        "path": str(directory),
        "exists": exists,
        "is_dir": directory.is_dir() if exists else False,
        "writable": False,
        "file_count": 0,
        "db_exists": state.db_path.is_file(),
    }

    if exists and not directory.is_dir():
        return Check(
            "data_dir", "数据目录", "data dir", STATUS_FAIL,
            blocking=True,
            detail=f"{directory} 存在但不是目录（是文件）",
            hint=(
                f"请把 {directory} 改回目录：先把它删掉或改名（例如 data.bak），"
                "然后重新运行 wingman.cmd，程序会自动重建数据目录。"
            ),
            hint_en=f"Remove or rename the file at {directory}; it must be a directory.",
            data=data,
        )

    if not exists:
        parent = directory.parent
        parent_state = "unavailable"
        if parent.is_dir():
            with _dir_timestamp_guard(parent):
                parent_state, _ = _probe_writable(parent)
        parent_ok = parent_state == "writable"
        data["parent_writable"] = parent_ok
        data["parent_probe_status"] = parent_state
        if parent_ok:
            return Check(
                "data_dir", "数据目录", "data dir", STATUS_WARN,
                blocking=True,
                detail=f"{directory} 尚不存在（首次启动会自动创建）",
                hint=(
                    f"不需要手工处理：wingman.cmd 启动时会自动创建 {directory}。"
                    "如果你希望它放在别处，请确认仓库目录没有被移动。"
                ),
                hint_en=f"No action needed: {directory} is created automatically on first start.",
                data=data,
            )
        if not parent.is_dir():
            return Check(
                "data_dir", "数据目录", "data dir", STATUS_FAIL,
                blocking=True,
                detail=f"{directory} 不存在，上级目录 {parent} 也不存在（仓库路径不对？）",
                hint=(
                    f"看起来 {state.project_dir} 不是完整的 WingMan 仓库。"
                    "请确认在仓库根目录运行，或用 --project-dir 指定正确路径；"
                    "重新 git clone / 重新解压发行包也能解决。"
                ),
                hint_en=(
                    f"{state.project_dir} does not look like a complete WingMan repository. "
                    "Run from the repository root, pass --project-dir, or re-clone/re-extract."
                ),
                data=data,
            )
        return Check(
            "data_dir", "数据目录", "data dir", STATUS_FAIL,
            blocking=True,
            detail=f"{directory} 不存在，且上级目录 {parent} 不可写，无法自动创建",
            hint=(
                f"给 {parent} 写入权限（或把仓库移到有写权限的目录，例如用户目录下），"
                "然后重新运行 wingman.cmd。"
            ),
            hint_en=f"Grant write permission on {parent} (or move the repository), then re-run wingman.cmd.",
            data=data,
        )

    with _dir_timestamp_guard(directory):
        probe_status, reason = _probe_writable(directory)
    count_state, entries = _call_with_timeout(lambda: list(directory.iterdir()), 3.0)
    data["file_count"] = len(entries) if count_state == "ok" and isinstance(entries, list) else 0
    data["writable"] = probe_status == "writable"
    data["probe_status"] = probe_status

    if probe_status == "writable":
        return Check(
            "data_dir", "数据目录", "data dir", STATUS_PASS,
            blocking=True,
            detail=f"{directory} 可写（已有 {data['file_count']} 个文件）",
            data=data,
        )
    if probe_status == "timeout":
        return Check(
            "data_dir", "数据目录", "data dir", STATUS_WARN,
            blocking=False,
            detail=f"{directory} 可写性未知：{reason}",
            hint=(
                "写入探测没有在限时内返回，通常是杀毒软件 / 同步盘 / 网络盘在拦截写入。"
                f"请把 {directory} 加入杀毒软件白名单，或把仓库移到本地普通目录后"
                "再运行 wingman.cmd --doctor 复查；启动后如果报 database is locked 之类错误，优先查这里。"
            ),
            hint_en=(
                f"Write probe timed out on {directory}. Add it to your antivirus allow-list "
                "or move the repository to a local folder."
            ),
            data=data,
        )
    return Check(
        "data_dir", "数据目录", "data dir", STATUS_FAIL,
        blocking=True,
        detail=f"{directory} 不可写：{reason}",
        hint=(
            f"数据库就放在这个目录里，不可写会直接导致启动失败。修复方式二选一："
            f"（1）恢复写权限，在命令行执行 icacls \"{directory}\" /grant \"%USERNAME%\":(OI)(CI)F ；"
            "（2）把只读属性/杀软/同步盘的占用解除后重试。"
            "改完再运行 wingman.cmd --doctor 确认。"
        ),
        hint_en=(
            f"Restore write permission on {directory}: "
            f'icacls "{directory}" /grant "%USERNAME%":(OI)(CI)F'
        ),
        data=data,
    )


def check_database(state: State) -> Check:
    db = state.db_path
    data: dict[str, Any] = {
        "path": str(db),
        "exists": db.is_file(),
        "schema_ready": False,
        "missing_tables": [],
        "counts": {},
        "journal_mode": "",
        "size_bytes": 0,
    }
    if not db.exists():
        return Check(
            "database", "数据库", "database", STATUS_PASS,
            blocking=True,
            detail="数据库文件尚不存在（首次启动会自动创建，不影响启动）",
            data=data,
        )
    if not db.is_file():
        return Check(
            "database", "数据库", "database", STATUS_FAIL,
            blocking=True,
            detail=f"{db} 存在但不是文件",
            hint=f"删掉或改名这个路径后重试；程序需要它作为 SQLite 数据库文件。",
            hint_en=f"Remove or rename {db}; it must be a SQLite database file.",
            data=data,
        )

    data["size_bytes"] = db.stat().st_size
    # 只读打开一个 WAL 库会顺手建出 -wal / -shm，SQLite 关闭连接后不一定删掉。
    # 为了「自检零副作用」，这里记录打开前已存在的 sidecar，结束后只清理本次新建的。
    sidecars = [db.with_name(db.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    pre_existing = {str(p) for p in sidecars if p.exists()}
    tables: list[str] = []
    counts: dict[str, int] = {}
    kv: dict[str, str] = {}

    with _dir_timestamp_guard(db.parent):
        try:
            # 只读 URI + query_only：不建表、不迁移、不写 WAL checkpoint
            conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10.0)
            try:
                conn.execute("PRAGMA query_only = ON")
                data["journal_mode"] = str(
                    conn.execute("PRAGMA journal_mode").fetchone()[0]
                )
                tables = sorted(
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                )
                for table in REQUIRED_TABLES:
                    if table not in tables:
                        continue
                    if table == "voice_log":
                        counts["voice_segments"] = int(
                            conn.execute("SELECT COUNT(*) FROM voice_log").fetchone()[0]
                        )
                    else:
                        counts[table] = int(
                            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        )
                if "kv" in tables:
                    kv = {
                        str(row[0]): "" if row[1] is None else str(row[1])
                        for row in conn.execute("SELECT key, value FROM kv")
                    }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return Check(
                "database", "数据库", "database", STATUS_FAIL,
                blocking=True,
                detail=f"无法打开数据库 {db}：{type(exc).__name__}: {exc}",
                hint=(
                    "常见原因：文件被占用/损坏，或所在目录不可写。"
                    f"可以先把它改名备份（例如 wingman.db.bak-1），再运行 wingman.cmd，"
                    "程序会自动新建一个空库；旧库里的聊天记录仍可用 SQLite 工具导出。"
                ),
                hint_en=(
                    f"Cannot open {db}. Rename it (e.g. wingman.db.bak-1) and re-run wingman.cmd "
                    "to get a fresh database."
                ),
                data=data,
            )
        finally:
            for path in sidecars:
                if str(path) in pre_existing:
                    continue
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass

    missing = [table for table in REQUIRED_TABLES if table not in tables]
    data["tables"] = tables
    data["missing_tables"] = missing
    data["counts"] = counts
    state.kv_overrides = {k: v for k, v in kv.items() if v}

    if missing:
        return Check(
            "database", "数据库", "database", STATUS_WARN,
            blocking=True,
            detail=f"库可读，但缺 {len(missing)} 张表：{'、'.join(missing)}（启动时会自动建表）",
            hint="不需要手工处理：首启动会用 CREATE TABLE IF NOT EXISTS 补齐。",
            hint_en="No action needed: missing tables are created automatically at startup.",
            data=data,
        )

    summary = "、".join(f"{k} {v}" for k, v in counts.items())
    return Check(
        "database", "数据库", "database", STATUS_PASS,
        blocking=True,
        detail=f"可读且 schema 就绪（{summary}）" if summary else "可读且 schema 就绪",
        data=data,
    )


def check_port(state: State) -> Check:
    if state.port == 0:
        return Check(
            "port", "端口可用性", "port", STATUS_PASS,
            blocking=True,
            detail="已按参数跳过端口占用检查（--port 0）",
            data={"port": 0, "skipped": True},
        )
    host = state.host or DEFAULT_HOST
    result = _port_probe(host, state.port)
    configured_port, port_source, _ = _resolve(state, "port")
    data: dict[str, Any] = {
        "host": host,
        "port": state.port,
        "from_cli": state.port_from_cli,
        "occupied": False,
        "configured_port": _as_int(configured_port),
        "configured_source": port_source,
    }
    if result["bind_ok"]:
        detail = f"{host}:{state.port} 可用" + ("" if state.port == DEFAULT_PORT else f"（非默认端口，默认 {DEFAULT_PORT}）")
        return Check(
            "port", "端口可用性", "port", STATUS_PASS,
            blocking=True, detail=detail, data=data,
        )

    data["occupied"] = True
    data["errno"] = result["errno"]
    existing = _probe_existing_service(host, state.port)
    if existing is not None:
        data["existing_service"] = {
            "kind": "wingman",
            "version": str(existing.get("version", "")),
            "db": str(existing.get("db", "")),
        }
        return Check(
            "port", "端口可用性", "port", STATUS_FAIL,
            blocking=True,
            detail=f"{host}:{state.port} 已被占用，占用者是已经在运行的 WingMan（版本 {existing.get('version')}）",
            hint=(
                f"先看看是不是自己开着的：浏览器打开 http://127.0.0.1:{state.port} 就能用，"
                "不需要再启动一个；确实要再起一个的话，用 wingman.cmd --port 8788 换端口。"
            ),
            hint_en=(
                f"WingMan is already running on http://127.0.0.1:{state.port}. "
                "Open it, or start another instance with wingman.cmd --port 8788."
            ),
            data=data,
        )

    return Check(
        "port", "端口可用性", "port", STATUS_FAIL,
        blocking=True,
        detail=f"{host}:{state.port} 已被其他程序占用（{result['error']}）",
        hint=(
            f"查占用者：netstat -ano | findstr :{state.port}（最后一列是 PID，"
            "再执行 tasklist /fi \"pid eq <PID>\" 看是哪个程序）。"
            f"要么关掉它，要么换端口启动：wingman.cmd --port 8788"
        ),
        hint_en=(
            f"Find the owner: netstat -ano | findstr :{state.port} , then "
            f'tasklist /fi "pid eq <PID>". Or start on another port: wingman.cmd --port 8788'
        ),
        data=data,
    )


def check_frontend(state: State) -> Check:
    index = state.frontend_dir / "index.html"
    data = {"frontend_dir": str(state.frontend_dir), "index_html": str(index), "exists": index.is_file()}
    if index.is_file():
        return Check(
            "frontend", "前端控制台", "frontend", STATUS_PASS,
            blocking=True,
            detail=f"frontend/index.html 存在（{index.stat().st_size} 字节）",
            data=data,
        )
    return Check(
        "frontend", "前端控制台", "frontend", STATUS_FAIL,
        blocking=True,
        detail="frontend/index.html 缺失，浏览器打开只会看到 404（后端 API 仍在）",
        hint=(
            "仓库文件不完整：请重新 git clone 或重新解压发行包；"
            f"如果你移动过目录，确认没有把 frontend 目录删掉（期望位置 {state.frontend_dir}）。"
        ),
        hint_en=(
            "frontend/index.html is missing. Re-clone or re-extract the release archive "
            f"(expected at {state.frontend_dir})."
        ),
        data=data,
    )


def check_samples(state: State) -> Check:
    count = 0
    names: list[str] = []
    if state.samples_dir.is_dir():
        try:
            files = sorted(p for p in state.samples_dir.iterdir() if p.is_file())
            count = len(files)
            names = [p.name for p in files[:5]]
        except OSError:
            count = 0
    data = {"samples_dir": str(state.samples_dir), "count": count, "names": names}
    if count:
        return Check(
            "samples", "示例数据", "samples", STATUS_PASS,
            blocking=False, optional=True,
            detail=f"samples/ 有 {count} 个示例文件（例如 {names[0]}）",
            data=data,
        )
    return Check(
        "samples", "示例数据", "samples", STATUS_WARN,
        blocking=False, optional=True,
        detail="samples/ 不存在或为空（不影响启动，只是没法一键导入示例聊天）",
        hint="重新 git clone 或重新解压发行包可恢复示例；也可以直接导入你自己的聊天记录导出文件。",
        hint_en="Re-clone / re-extract the release to get the sample files back.",
        data=data,
    )


def _optional_module_check(
    state: State, module: str, dist: str, purpose: str, check_id: str, title: str, title_en: str
) -> Check:
    row = _module_state(module, dist, purpose)
    installed = bool(row["ok"])
    detail = (
        f"已安装 {module} {row['version']}".strip() + "（可选）"
        if installed
        else f"未安装 {module}（可选，不影响启动）"
    )
    data = {
        "module": module,
        "installed": installed,
        "version": row["version"],
        "purpose": purpose,
        "error": row["error"],
        "asr_engine": _resolve(state, "asr_engine")[0],
    }
    if installed:
        return Check(check_id, title, title_en, STATUS_PASS, blocking=False, optional=True,
                     detail=detail, data=data)
    return Check(
        check_id, title, title_en, STATUS_WARN,
        blocking=False, optional=True,
        detail=detail,
        hint=f"需要「{purpose}」时执行 wingman.cmd --with-asr 一次装齐语音依赖；不装也能正常启动。",
        hint_en="Optional: run wingman.cmd --with-asr to install the voice dependencies.",
        data=data,
    )


def _provider_summary(state: State) -> list[dict[str, Any]]:
    """离线推断各 provider 的可用性，规则与 app/llm|asr|memory 的工厂保持一致。

    工厂的原则是「永不抛异常，配置不全就退化为 Mock」，所以这里也只在
    「配置写了 A 但实际会退化为 B」时说清楚，而不是判失败。
    """
    out: list[dict[str, Any]] = []

    llm_kind = str(_resolve(state, "llm_provider")[0] or "mock").lower().strip()
    base_url = str(_resolve(state, "llm_base_url")[0] or "").strip()
    model = str(_resolve(state, "llm_model")[0] or "").strip()
    api_key = _as_bool_text(_resolve(state, "llm_api_key")[0])
    if llm_kind in ("ollama", "local"):
        host = str(_resolve(state, "ollama_host")[0] or "http://127.0.0.1:11434")
        ollama_model = str(_resolve(state, "ollama_model")[0] or "qwen2.5:7b")
        reachable = _tcp_reachable(host)
        out.append({
            "kind": "llm",
            "name": "ollama",
            "available": reachable,
            "note": f"{host} · {ollama_model}" + ("（服务可达）" if reachable else "（连不上，调用时会报错）"),
        })
    elif llm_kind in ("openai", "openai_compat", "cloud", "api"):
        if base_url and model:
            out.append({
                "kind": "llm",
                "name": "openai_compat",
                "available": True,
                "note": f"{base_url} · {model}" + ("" if api_key else "（未填 api_key，看服务端要求）"),
            })
        else:
            missing = "、".join(
                name for name, value in (("llm_base_url", base_url), ("llm_model", model)) if not value
            )
            out.append({
                "kind": "llm",
                "name": "mock",
                "available": True,
                "note": f"llm_provider={llm_kind} 但 {missing} 未配置，实际会退化为 Mock（不会调用真模型）",
            })
    else:
        out.append({
            "kind": "llm",
            "name": "mock",
            "available": True,
            "note": "演示用 Mock，输出只用于跑通流程；接真模型见 docs/MODELS.md",
        })

    embed_mode = str(_resolve(state, "embedder")[0] or "auto").lower()
    embed_base = str(_resolve(state, "embed_base_url")[0] or "").strip()
    if embed_mode != "hash" and not embed_base and llm_kind in ("openai", "openai_compat", "cloud", "api"):
        embed_base = base_url
    if embed_mode in ("auto", "cloud") and embed_base:
        out.append({
            "kind": "embedder",
            "name": "cloud",
            "available": True,
            "note": f"{embed_base} · {_resolve(state, 'embed_model')[0]}",
        })
    else:
        note = "本地哈希向量，永远可用（语义泛化弱）"
        if embed_mode == "cloud":
            note = "embedder=cloud 但未配置 embed_base_url，实际会退化为本地哈希向量"
        out.append({"kind": "embedder", "name": "hash", "available": True, "note": note})

    asr_kind = str(_resolve(state, "asr_engine")[0] or "mock").lower()
    if asr_kind in ("local", "whisper", "faster_whisper"):
        row = _module_state("faster_whisper", "faster-whisper", "本地语音转写")
        out.append({
            "kind": "asr",
            "name": "local" if row["ok"] else "mock",
            "available": bool(row["ok"]),
            "note": (
                f"本机 faster-whisper · {_resolve(state, 'whisper_model')[0]}"
                if row["ok"] else "asr_engine=local 但未安装 faster-whisper，实际会退化为 Mock"
            ),
        })
    elif asr_kind in ("cloud", "api", "openai"):
        asr_base = str(_resolve(state, "asr_base_url")[0] or "").strip()
        out.append({
            "kind": "asr",
            "name": "cloud" if asr_base else "mock",
            "available": bool(asr_base),
            "note": asr_base or "asr_engine=cloud 但未配置 asr_base_url，实际会退化为 Mock",
        })
    else:
        out.append({"kind": "asr", "name": "mock", "available": True,
                    "note": "Mock 语音（/api/voice/ingest 可手工注入文本试通链路）"})
    return out


def _tcp_reachable(host_url: str, timeout: float = 1.0) -> bool:
    """对 http://host:port 做一次 TCP 连接探测（不发明文请求，纯连通性）。"""
    raw = host_url.strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if ":" in raw:
        host, _, port_text = raw.rpartition(":")
        port = _as_int(port_text) or 80
    else:
        host, port = raw, 80
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_config(state: State) -> Check:
    env_rows = state.env_file_rows()
    hit_env_files = [row["path"] for row in env_rows if row["exists"]]
    runtime_keys = sorted(state.kv_overrides.keys())

    watch_keys = (
        "host",
        "port",
        "llm_provider",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "ollama_host",
        "ollama_model",
        "embedder",
        "embed_base_url",
        "embed_api_key",
        "embed_model",
        "asr_engine",
        "asr_base_url",
        "asr_api_key",
        "asr_model",
    )
    keys: dict[str, Any] = {}
    for key in watch_keys:
        value, source, source_label = _resolve(state, key)
        secret = _is_secret_key(key, state)
        entry: dict[str, Any] = {
            "source": source,
            "source_label": source_label,
            "secret": secret,
            "set": bool(_as_bool_text(value)) if not isinstance(value, bool) else bool(value),
        }
        if secret:
            # 安全红线：密钥只以掩码形式出现，且不外泄长度（见 _mask_value）
            entry["value"] = _mask_value(str(value or "")) if value else ""
        else:
            entry["value"] = value
        keys[key] = entry

    providers = _provider_summary(state)
    provider_notes = "；".join(f"{p['kind']}={p['name']}{'' if p['available'] else '(不可用)'}" for p in providers)
    data = {
        "env_files": env_rows,
        "env_files_hit": hit_env_files,
        "runtime_override_keys": runtime_keys,
        "runtime_override_count": len(runtime_keys),
        "keys": keys,
        "providers": providers,
        "config_py_parsed": bool((state.config_info or {}).get("ok")),
    }

    degraded = [p for p in providers if not p["available"] or p["name"] == "mock"]
    mock_llm = any(p["kind"] == "llm" and p["name"] == "mock" for p in providers)
    both_default = not hit_env_files and not runtime_keys

    detail = f"配置来源：.env {len(hit_env_files)} 个、运行时覆盖 {len(runtime_keys)} 项 · provider：{provider_notes}"
    if both_default and mock_llm:
        return Check(
            "config", "配置与 Provider", "config & providers", STATUS_WARN,
            blocking=False,
            detail=f"{detail} · 全部走默认值（无 .env、无运行时覆盖）",
            hint=(
                "当前是默认的 Mock 配置：不配模型也能跑通全流程，但回复只是流程演示，"
                "不是真模型输出。要接真实模型，见 docs/MODELS.md（复制 .env.example 为 .env，"
                "填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，或启动后在控制台「设置」里改）。"
            ),
            hint_en=(
                "Running with the default Mock configuration. Copy .env.example to .env and set "
                "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL to use a real model (see docs/MODELS.md)."
            ),
            data=data,
        )
    if degraded:
        names = "、".join(f"{p['kind']}={p['name']}" for p in degraded)
        return Check(
            "config", "配置与 Provider", "config & providers", STATUS_WARN,
            blocking=False,
            detail=f"{detail}",
            hint=(
                f"有配置项会退化为兜底实现（{names}）：不影响启动，但效果与预期不同。"
                "请按 docs/MODELS.md 补齐对应配置，然后用 wingman.cmd --doctor 复查。"
            ),
            hint_en="Some providers fall back to a default implementation; see docs/MODELS.md.",
            data=data,
        )
    return Check(
        "config", "配置与 Provider", "config & providers", STATUS_PASS,
        blocking=False, detail=detail, data=data,
    )


def check_optional_soundcard(state: State) -> Check:
    return _optional_module_check(
        state, "soundcard", "soundcard", "通话 / 系统声音采集（可选）",
        "optional_soundcard", "语音依赖 soundcard", "optional soundcard",
    )


def check_optional_faster_whisper(state: State) -> Check:
    return _optional_module_check(
        state, "faster_whisper", "faster-whisper", "本地语音转写（可选）",
        "optional_faster_whisper", "语音依赖 faster-whisper", "optional faster-whisper",
    )


# 报告顺序 = 执行顺序。数据库检查会顺手读出 kv 覆盖，供后面的配置检查使用。
CHECKS: tuple[Callable[[State], Check], ...] = (
    check_project_layout,
    check_python_version,
    check_backend_deps,
    check_data_dir,
    check_database,
    check_port,
    check_frontend,
    check_samples,
    check_optional_soundcard,
    check_optional_faster_whisper,
    check_config,
)


# ---------------------------------------------------------------- 环境与执行


def _interpreter_info(backend_dir: Path) -> dict[str, Any]:
    executable = Path(sys.executable)
    kind = "system"
    venv_path = ""
    for venv_name in (".venv", ".venv-baseline"):
        scripts = backend_dir / venv_name / ("Scripts" if os.name == "nt" else "bin")
        try:
            same = executable.parent == scripts.resolve() or executable.parent == scripts
        except OSError:
            same = False
        if same:
            kind, venv_path = "project-venv", str(backend_dir / venv_name)
            break
    if kind == "system" and sys.prefix != sys.base_prefix:
        kind, venv_path = "other-venv", sys.prefix
    labels = {
        "project-venv": "项目虚拟环境",
        "other-venv": "其他虚拟环境",
        "system": "系统 Python（未使用项目虚拟环境）",
    }
    return {
        "kind": kind,
        "kind_label": labels.get(kind, kind),
        "venv_path": venv_path,
        "executable": sys.executable,
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "prefix": sys.prefix,
    }


def build_state(args: argparse.Namespace) -> State:
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else DEFAULT_PROJECT_DIR
    backend_dir = project_dir / "backend"
    state = State(
        project_dir=project_dir,
        backend_dir=backend_dir,
        data_dir=backend_dir / "data",
        frontend_dir=project_dir / "frontend",
        samples_dir=project_dir / "samples",
        db_path=backend_dir / "data" / "wingman.db",
        config_py=backend_dir / "app" / "config.py",
        env_files=[project_dir / ".env", backend_dir / ".env"],
        host=args.host,
        port=args.port if args.port is not None else DEFAULT_PORT,
        port_from_cli=args.port is not None,
    )
    state.config_info = _parse_config_py(state.config_py)
    state.env_parsed = [(path, _parse_env_file(path)) for path in state.env_files]
    state.interpreter = _interpreter_info(backend_dir)
    return state


def run_checks(state: State) -> list[Check]:
    checks: list[Check] = []
    for function in CHECKS:
        try:
            check = function(state)
        except Exception as exc:  # noqa: BLE001 - 单个检查崩掉不能拖垮整个自检
            check = Check(
                id=function.__name__.replace("check_", "") or function.__name__,
                title="检查项异常",
                title_en="check crashed",
                status=STATUS_WARN,
                blocking=False,
                detail=f"该项检查自身出错（{type(exc).__name__}: {exc}），结果不可信",
                hint="这是自检脚本的问题，不影响 WingMan 启动；请把本行原文反馈给维护者。",
                hint_en="A preflight check crashed; it does not affect WingMan startup.",
            )
        # 不变式：非阻断项永远不产生 fail（否则会错误地把退出码顶成非零）
        if check.status == STATUS_FAIL and not check.blocking:
            check.status = STATUS_WARN
        checks.append(check)
    return checks


def build_payload(state: State, checks: list[Check], exit_code: int) -> dict[str, Any]:
    blockers = [c for c in checks if c.blocks_start]
    warnings = [c for c in checks if c.status == STATUS_WARN]
    passed = [c for c in checks if c.status == STATUS_PASS]
    if exit_code == EXIT_OK:
        conclusion = "可以启动"
    elif exit_code == EXIT_BLOCKED:
        conclusion = f"有 {len(blockers)} 项必须先修"
    else:
        conclusion = "自检未跑完"
    status = {EXIT_OK: "pass", EXIT_BLOCKED: "blocked"}.get(exit_code, "error")
    return {
        "tool": TOOL_NAME,
        "schema_version": SCHEMA_VERSION,
        "ok": exit_code == EXIT_OK,
        "blocked": exit_code == EXIT_BLOCKED,
        "status": status,
        "exit_code": exit_code,
        "conclusion": conclusion,
        "project_dir": str(state.project_dir),
        "port": state.port,
        "summary": {
            "total": len(checks),
            "passed": len(passed),
            "warnings": len(warnings),
            "failed": len([c for c in checks if c.status == STATUS_FAIL]),
            "blocking": len(blockers),
        },
        "checks": [c.to_json() for c in checks],
        "blocking_ids": [c.id for c in blockers],
        "warning_ids": [c.id for c in warnings],
        "environment": {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "python_executable": sys.executable,
            "interpreter_kind": state.interpreter.get("kind", "unknown"),
            "project_venv": state.interpreter.get("venv_path", ""),
            "platform": sys.platform,
            "stdout_encoding": getattr(sys.stdout, "encoding", "") or "",
            "ascii_only": ASCII_ONLY,
            "config_py_parsed": bool((state.config_info or {}).get("ok")),
        },
    }


# ---------------------------------------------------------------- 渲染


_STATUS_LABELS = {
    STATUS_PASS: ("通过", "OK"),
    STATUS_WARN: ("提醒", "WARN"),
    STATUS_FAIL: ("阻断", "FAIL"),
}


def _status_label(check: Check) -> str:
    zh, ascii_form = _STATUS_LABELS.get(check.status, ("未知", "????"))
    return f"[{ascii_form if ASCII_ONLY else zh}]"


def render_human(state: State, checks: list[Check], exit_code: int) -> str:
    blockers = [c for c in checks if c.blocks_start]
    warnings = [c for c in checks if c.status == STATUS_WARN]
    passed = [c for c in checks if c.status == STATUS_PASS]
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(t("WingMan 启动前自检 · preflight", "WingMan preflight (doctor)"))
    lines.append("=" * 64)
    lines.append(t(f"{_pad('仓库目录', 10)}：{state.project_dir}", f"Project : {state.project_dir}"))
    lines.append(t(
        f"{_pad('Python', 10)}：{state.interpreter['version']} · {state.interpreter['executable']}"
        f"（{state.interpreter['kind_label']}）",
        f"Python  : {state.interpreter['version']} {state.interpreter['executable']}",
    ))
    encoding = getattr(sys.stdout, "encoding", "") or "未知"
    lines.append(t(
        f"{_pad('控制台', 10)}：{encoding}" + ("（渲染不了中文，已降级为 ASCII 输出）" if ASCII_ONLY else ""),
        f"Console : {encoding}" + (" (ASCII-only fallback)" if ASCII_ONLY else ""),
    ))
    port_note = "命令行指定" if state.port_from_cli else "默认值"
    if state.port == 0:
        port_note = "已跳过检查"
    lines.append(t(
        f"{_pad('待查端口', 10)}：{state.port}（{port_note}）",
        f"Port    : {state.port} ({port_note})",
    ))
    lines.append("")

    total = len(checks)
    for index, check in enumerate(checks, start=1):
        title = check.title if not ASCII_ONLY else check.title_en
        head = f"[{index:>2}/{total}] {_pad(title, 22)} {_pad(_status_label(check), 8)} {check.detail}"
        lines.append(t(head))
        if check.hint and check.status in (STATUS_WARN, STATUS_FAIL):
            hint = check.hint if not ASCII_ONLY else (check.hint_en or _ascii(check.hint))
            lines.append(t(f"           修复建议：{hint}", f"           Fix: {hint}"))

    lines.append("")
    lines.append("-" * 64)
    if warnings:
        lines.append(t(f"提醒（不阻断启动，{len(warnings)} 项）",
                      f"WARNINGS (non-blocking, {len(warnings)})"))
        for index, check in enumerate(warnings, start=1):
            lines.append(t(f"  {index}. [{check.id}] {check.detail}", f"  {index}. [{check.id}] {_ascii(check.detail)}"))
        lines.append("")
    if blockers:
        lines.append(t(f"必须先修（{len(blockers)} 项）", f"MUST FIX ({len(blockers)})"))
        for index, check in enumerate(blockers, start=1):
            lines.append(t(f"  {index}. [{check.id}] {check.detail}", f"  {index}. [{check.id}] {_ascii(check.detail)}"))
            if check.hint:
                hint = check.hint if not ASCII_ONLY else (check.hint_en or _ascii(check.hint))
                lines.append(t(f"     修复建议：{hint}", f"     Fix: {hint}"))
        lines.append("")
    summary = t(
        f"{total} 项检查：{len(passed)} 通过 / {len(warnings)} 提醒 / {len(blockers)} 阻断",
        f"{total} checks: {len(passed)} OK / {len(warnings)} WARN / {len(blockers)} BLOCKED",
    )
    if exit_code == EXIT_OK:
        lines.append(t(f"结论：可以启动（{summary}）", f"RESULT: READY TO START ({summary})"))
        lines.append(t("下一步：在仓库根目录运行 wingman.cmd；需要语音能力就加 --with-asr。",
                       "Next: run wingman.cmd in the repository root (--with-asr for voice)."))
    elif exit_code == EXIT_BLOCKED:
        lines.append(t(f"结论：有 {len(blockers)} 项必须先修（{summary}）",
                       f"RESULT: {len(blockers)} PROBLEM(S) MUST BE FIXED FIRST ({summary})"))
        lines.append(t("按上面的「修复建议」处理后，重新运行 wingman.cmd --doctor 复查。",
                       "Fix the items above, then re-run wingman.cmd --doctor."))
    else:
        lines.append(t(f"结论：自检未跑完（{summary}）", f"RESULT: PREFLIGHT DID NOT FINISH ({summary})"))
    lines.append("-" * 64)
    return "\n".join(lines)


def render_json_summary(state: State, checks: list[Check], exit_code: int) -> str:
    """--json 模式下给 stderr 的人类摘要（stdout 只放 JSON，方便机器解析）。"""
    blockers = [c for c in checks if c.blocks_start]
    warnings = [c for c in checks if c.status == STATUS_WARN]
    lines = [
        t(f"[preflight] 结论：{'可以启动' if exit_code == EXIT_OK else f'有 {len(blockers)} 项必须先修'}",
          f"[preflight] result: {'READY' if exit_code == EXIT_OK else f'{len(blockers)} BLOCKED'}"),
        f"[preflight] exit_code={exit_code} checks={len(checks)} warnings={len(warnings)} blocking={len(blockers)}",
    ]
    for check in blockers:
        lines.append(t(f"[preflight] 阻断 [{check.id}] {check.detail} → {check.hint}",
                       f"[preflight] BLOCKED [{check.id}] {_ascii(check.detail)} -> {_ascii(check.hint)}"))
    for check in warnings:
        lines.append(t(f"[preflight] 提醒 [{check.id}] {check.detail}",
                       f"[preflight] WARN [{check.id}] {_ascii(check.detail)}"))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


class _ArgumentParser(argparse.ArgumentParser):
    """参数错误一律用退出码 1（自检自身异常），不和「2 = 有阻断项」抢语义。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        _eprint(t(f"参数错误：{message}", f"argument error: {_ascii(message)}"))
        _eprint(t("提示：python scripts/preflight.py --help 查看全部参数",
                  "hint: python scripts/preflight.py --help"))
        raise SystemExit(EXIT_INTERNAL)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="preflight.py",
        description="WingMan 启动前自检（doctor）：起不来说清原因，起得来能自证健康。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 关键项全通过 / 2 存在阻断项 / 1 自检自身异常\n"
            "--json 输出纯 ASCII JSON（stdout），人类摘要走 stderr，便于启动器与 CI 直接消费。\n"
            "示例：\n"
            "  python scripts/preflight.py\n"
            "  python scripts/preflight.py --json\n"
            "  python scripts/preflight.py --port 8788\n"
        ),
    )
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON（纯 ASCII，stdout；人类摘要打到 stderr）")
    parser.add_argument("--port", type=int, default=None,
                        help=f"要检查的端口，默认 {DEFAULT_PORT}；传 0 表示跳过端口检查")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"端口探测使用的地址，默认 {DEFAULT_HOST}")
    parser.add_argument("--project-dir", default=None,
                        help="仓库根目录，默认由本脚本位置推断")
    parser.add_argument("--encoding", default="auto", choices=("auto", "utf-8", "console", "ascii"),
                        help="输出编码：auto=连着控制台用控制台编码、输出管道/重定向时用 UTF-8（默认）；"
                             "utf-8=强制 UTF-8；console=强制沿用流自身编码"
                             "（cmd 里重定向到文件再用 type 查看时选它）；ascii=强制纯 ASCII")
    return parser


def _emit_internal_error(json_mode: bool, message: str) -> int:
    if json_mode:
        payload = {
            "tool": TOOL_NAME,
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "blocked": False,
            "status": "error",
            "exit_code": EXIT_INTERNAL,
            "conclusion": "自检未跑完",
            "checks": [],
            "blocking_ids": [],
            "internal_error": message,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    _eprint(t(f"[preflight] 自检自身异常：{message}", f"[preflight] internal error: {_ascii(message)}"))
    _eprint(t("[preflight] 这不代表 WingMan 有问题，请把上一行原文反馈给维护者。",
              "[preflight] This does not mean WingMan is broken; please report the line above."))
    return EXIT_INTERNAL


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _configure_stdio(_prescan_encoding(argv))
    args = build_parser().parse_args(argv)

    if args.port is not None and not 0 <= args.port <= 65535:
        return _emit_internal_error(
            args.json, t(f"端口超出范围：{args.port}（合法范围 1-65535，0 表示跳过检查）",
                         f"port out of range: {args.port}")
        )

    try:
        state = build_state(args)
    except Exception as exc:  # noqa: BLE001
        return _emit_internal_error(args.json, f"{type(exc).__name__}: {exc}")

    try:
        checks = run_checks(state)
    except Exception as exc:  # noqa: BLE001
        return _emit_internal_error(args.json, f"{type(exc).__name__}: {exc}")

    exit_code = EXIT_BLOCKED if any(c.blocks_start for c in checks) else EXIT_OK
    payload = build_payload(state, checks, exit_code)

    if args.json:
        # 纯 ASCII 输出：任何代码页下都能直接 json.loads
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        _eprint(render_json_summary(state, checks, exit_code))
    else:
        print(render_human(state, checks, exit_code))
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _eprint(t("已中断。", "interrupted."))
        raise SystemExit(EXIT_INTERNAL)
    except Exception as exc:  # noqa: BLE001 - 最后一道防线：绝不把堆栈当输出给用户
        raise SystemExit(_emit_internal_error("--json" in sys.argv[1:], f"{type(exc).__name__}: {exc}"))
