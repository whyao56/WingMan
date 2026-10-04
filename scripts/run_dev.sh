#!/usr/bin/env bash
# ================================================================
#  WingMan 启动脚本（macOS / Linux / Git Bash）
#
#  与 Windows 的 wingman.cmd + scripts\bootstrap.ps1 行为对齐：
#    发现 Python(>=3.11) → 建 backend/.venv → 按 backend/requirements.lock.txt
#    安装依赖 → 检查端口 → 启动服务（默认不开 --reload）→ 轮询 /api/health
#    → 打印访问地址/数据目录 → 打开浏览器（--no-browser 关闭）
#
#  用法：scripts/run_dev.sh [--with-asr] [--port N] [--setup-only]
#                           [--doctor] [--no-browser] [--help]
#
#  退出码：0 成功 / 2 前置自检未通过（零副作用）/ 3 依赖准备失败 / 1 其他错误
#
#  路径契约：虚拟环境只使用 backend/.venv（Windows 布局为 Scripts/python.exe，
#  POSIX 布局为 bin/python —— 同一个 venv 的两种平台形态，不存在第二候选）。
#
#  编码：脚本本身是 UTF-8；终端不支持 UTF-8 时设 WINGMAN_ASCII=1 可降级为纯 ASCII。
# ================================================================

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
LOCK="$BACKEND/requirements.lock.txt"
ASR_REQ="$BACKEND/requirements-asr.txt"
DATA="$BACKEND/data"
STAMP="$VENV/.wingman-deps.json"

MIN_MINOR=11
MAX_TESTED_MINOR=13
HEALTH_TIMEOUT=90

WITH_ASR=0; SETUP_ONLY=0; DOCTOR=0; NO_BROWSER=0
PORT="${PORT:-8787}"; PORT_FROM_ENV=0

ASCII_MODE=0
if [ "${WINGMAN_ASCII:-0}" = "1" ]; then
  ASCII_MODE=1
elif command -v locale >/dev/null 2>&1; then
  if [ "$(locale charmap 2>/dev/null | tr 'A-Z' 'a-z')" != "utf-8" ]; then ASCII_MODE=1; fi
fi

say() {
  local line="$1"
  if [ "$ASCII_MODE" = "1" ]; then
    line="$(printf '%s' "$line" | sed 's/[^ -~]//g')"
  fi
  printf '%s\n' "$line"
}
line()  { say "================================================================"; }
bad()   { say "✗ $1"; }
warn()  { say "! $1"; }
ok()    { say "✓ $1"; }

usage() {
  line
  say " WingMan 启动脚本（macOS / Linux / Git Bash）— 用法"
  line
  say "用法：scripts/run_dev.sh [选项]"
  say ""
  say "  （无参数）       安装（如需要）并启动，默认端口 8787，自动打开浏览器"
  say "  --port N         指定端口（1024-65535）"
  say "  --setup-only     只做安装与自检，不启动服务"
  say "  --doctor         只做体检，不创建、不安装、不启动"
  say "  --with-asr       额外安装语音（ASR）可选依赖；失败不影响主服务"
  say "  --no-browser     启动后不自动打开浏览器"
  say "  --help           显示这份帮助"
  say ""
  say "退出码：0 成功 / 2 前置自检未通过（零副作用）/ 3 依赖准备失败 / 1 其他错误"
  say "固定路径：虚拟环境 backend/.venv   依赖锁 backend/requirements.lock.txt"
  say "停止服务：在运行窗口按 Ctrl+C，服务会优雅退出。"
  line
}

die_preflight() { say ""; say "退出码 2 = 前置自检未通过，没有创建或修改任何文件"; exit 2; }
die_deps()      { say ""; say "退出码 3 = 依赖准备失败"; exit 3; }

while [ $# -gt 0 ]; do
  case "$1" in
    --with-asr)    WITH_ASR=1; shift ;;
    --setup-only)  SETUP_ONLY=1; shift ;;
    --doctor)      DOCTOR=1; shift ;;
    --no-browser)  NO_BROWSER=1; shift ;;
    --port)
      if [ $# -lt 2 ]; then
        bad "--port 后面缺少端口号，例如：scripts/run_dev.sh --port 8788"
        die_preflight
      fi
      PORT="$2"; shift 2 ;;
    --port=*)      PORT="${1#--port=}"; shift ;;
    --help|-h)     usage; exit 0 ;;
    *)             bad "无法识别的参数：$1"; say "      查看说明：scripts/run_dev.sh --help"; die_preflight ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*) bad "端口必须是数字，收到：$PORT"; die_preflight ;;
esac
if [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  bad "端口 $PORT 超出可用范围 1024-65535"
  say "      1024 以下的端口需要管理员权限，本脚本不请求管理员权限"
  die_preflight
fi
if [ "$SETUP_ONLY" = "1" ] && [ "$DOCTOR" = "1" ]; then
  bad "--setup-only 与 --doctor 不能同时使用，请二选一"
  die_preflight
fi

# ---------------------------------------------------------------- 工具函数

slug_version() {  # $1 = python 可执行文件；输出 x.y.z 或空
  "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

find_python() {
  FOUND_PY=""
  for cand in python3 python py; do
    if ! command -v "$cand" >/dev/null 2>&1; then
      say "        $(printf '%-9s' "$cand") → 未找到"
      continue
    fi
    case "$(command -v "$cand")" in
      */WindowsApps/python*|*/WindowsApps/py*)
        # Windows 的 “应用执行别名”：运行它会弹出微软商店，而不是 Python
        say "        $(printf '%-9s' "$cand") → Microsoft Store 别名（不是真正的 Python），已跳过"
        continue ;;
    esac
    ver="$(slug_version "$cand")"
    if [ -z "$ver" ]; then
      say "        $(printf '%-9s' "$cand") → 无法获取版本号"
      continue
    fi
    major="${ver%%.*}"
    rest="${ver#*.}"
    minor="${rest%%.*}"
    if [ "$major" = "3" ] && [ "$minor" -ge "$MIN_MINOR" ]; then
      say "        $(printf '%-9s' "$cand") → Python $ver  ✓"
      FOUND_PY="$cand"; FOUND_VER="$ver"; FOUND_MINOR="$minor"
      return 0
    fi
    say "        $(printf '%-9s' "$cand") → Python $ver（版本过低，需要 >= 3.$MIN_MINOR）"
  done
  return 1
}

venv_python() {  # 输出 venv 解释器路径（Windows 布局 / POSIX 布局）
  if [ -x "$VENV/Scripts/python.exe" ]; then printf '%s' "$VENV/Scripts/python.exe"; return 0; fi
  if [ -x "$VENV/bin/python" ]; then printf '%s' "$VENV/bin/python"; return 0; fi
  return 1
}

deps_check() {  # $1 = venv 解释器；0 = 依赖与锁文件一致且 app.main 可导入
  ( cd "$BACKEND" && "$1" - "$LOCK" <<'PY' >/dev/null 2>&1
import json, sys, importlib.metadata as md
lock = sys.argv[1]
pins = {}
for raw in open(lock, encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#") or "==" not in line:
        continue
    name = line.split("==")[0].split("[")[0].strip()
    pins[name] = line.split("==", 1)[1].strip()
problems = []
for name, want in pins.items():
    try:
        got = md.version(name)
    except Exception:
        problems.append(name)
        continue
    if got != want:
        problems.append(name)
for mod in ("fastapi", "uvicorn", "pydantic", "pydantic_settings", "httpx", "numpy", "starlette"):
    try:
        __import__(mod)
    except Exception:
        problems.append(mod)
try:
    import app.main  # noqa: F401
except Exception as exc:
    problems.append("app.main: %s" % exc)
print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=True))
sys.exit(0 if not problems else 1)
PY
  )
}

lock_fingerprint() {  # $1 = venv 解释器
  "$1" - "$LOCK" "$ASR_REQ" "$WITH_ASR" <<'PY'
import hashlib, sys, os
parts = []
for path in (sys.argv[1], sys.argv[2]):
    if os.path.isfile(path) and sys.argv[3] == "1":
        parts.append(hashlib.sha256(open(path, "rb").read()).hexdigest())
    elif path == sys.argv[1]:
        parts.append(hashlib.sha256(open(path, "rb").read()).hexdigest())
parts.append(sys.version.split()[0])
print(hashlib.sha256("|".join(parts).encode()).hexdigest())
PY
}

stamp_matches() {  # $1 = venv 解释器，$2 = 期望指纹
  [ -f "$STAMP" ] || return 1
  "$1" - "$STAMP" "$2" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("fingerprint") == sys.argv[2] else 1)
PY
}

port_busy() {  # $1 = venv/base 解释器；0 = 被占用
  "$1" - "$PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
try:
    s.bind(("127.0.0.1", port))
    s.close()
    sys.exit(1)
except OSError:
    sys.exit(0)
PY
}

port_owner() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -n 3
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ":$PORT " | head -n 3
  fi
}

health_ready() {  # $1 = venv 解释器；0 = /api/health 就绪
  "$1" - "$PORT" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/api/health" % port, timeout=3) as resp:
        data = json.load(resp)
except Exception:
    sys.exit(1)
ok = all(k in data for k in ("version", "db", "counts", "providers"))
sys.exit(0 if ok else 1)
PY
}

open_browser() {
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 &
  elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "http://127.0.0.1:$PORT/" >/dev/null 2>&1 &
  else return 1
  fi
  return 0
}

# ---------------------------------------------------------------- 前置检查

line
say " WingMan 启动脚本（macOS / Linux / Git Bash）"
line
say "      仓库目录    : $ROOT"
say "      Python 要求 : >= 3.$MIN_MINOR（3.$MIN_MINOR-3.$MAX_TESTED_MINOR 已实测）"

if [ ! -f "$BACKEND/app/main.py" ] || [ ! -f "$FRONTEND/index.html" ] || [ ! -f "$LOCK" ]; then
  bad "仓库不完整：需要 backend/app/main.py、frontend/index.html、backend/requirements.lock.txt"
  say "      怎么修：重新完整克隆或解压 WingMan"
  die_preflight
fi

say ""
say "· 1/3 前置自检"
say "      探测 Python："
if ! find_python; then
  say ""
  bad "没有找到可用的 Python 3.$MIN_MINOR+"
  say "      检测到什么：上面 3 个候选里没有一个满足 Python >= 3.$MIN_MINOR"
  say "      需要什么  ：Python 3.$MIN_MINOR - 3.$MAX_TESTED_MINOR"
  say "      怎么修    ："
  say "        1) macOS：brew install python@3.12   /  Linux：sudo apt install python3 python3-venv"
  say "        2) 或到 https://www.python.org/downloads/ 安装官方版本"
  die_preflight
fi

if [ "$SETUP_ONLY" = "1" ]; then
  say "      端口        : 跳过（--setup-only 不启动服务，不占用端口）"
elif [ "$DOCTOR" != "1" ]; then
  if port_busy "$FOUND_PY"; then
    say ""
    bad "端口 $PORT 已被占用，无法启动"
    owner="$(port_owner)"
    [ -n "$owner" ] && say "      占用者：$owner"
    say "      解决："
    say "        1) 换端口：scripts/run_dev.sh --port $((PORT + 1))"
    say "        2) 或结束占用进程（kill <PID>）"
    die_preflight
  fi
  say "      端口        : 127.0.0.1:$PORT 可用"
fi

# ---------------------------------------------------------------- 依赖准备

if [ "$DOCTOR" = "1" ]; then
  say ""
  say "· 体检（不会创建、安装、启动任何东西）"
  problems=0
  vpy="$(venv_python || true)"
  if [ -n "$vpy" ]; then ok "虚拟环境    : $vpy"; else bad "虚拟环境    : backend/.venv 不存在 → scripts/run_dev.sh --setup-only"; problems=1; fi
  if [ -n "$vpy" ] && deps_check "$vpy"; then ok "依赖完整性  : 与 requirements.lock.txt 一致，app.main 可导入"; else bad "依赖完整性  : 不满足 → scripts/run_dev.sh --setup-only"; problems=1; fi
  if [ -d "$DATA" ]; then ok "数据目录    : $DATA"; else warn "数据目录    : 不存在（启动时会创建）"; fi
  if port_busy "$FOUND_PY"; then bad "端口        : 127.0.0.1:$PORT 已被占用（若是你自己启动的 WingMan 可忽略）"; problems=1; else ok "端口        : 127.0.0.1:$PORT 可用"; fi
  say ""
  if [ "$problems" != "0" ]; then die_deps; fi
  ok "体检通过：可以运行 scripts/run_dev.sh 启动服务"
  exit 0
fi

say ""
say "· 2/3 准备依赖"
if ! vpy="$(venv_python)"; then
  say "      创建虚拟环境：backend/.venv（用 $FOUND_PY）"
  if ! "$FOUND_PY" -m venv "$VENV"; then
    bad "创建虚拟环境失败"
    say "      怎么修：Linux 需要先装 python3-venv；确认磁盘可写"
    die_deps
  fi
  vpy="$(venv_python)" || { bad "创建后仍找不到虚拟环境里的解释器"; die_deps; }
fi

[ -d "$DATA" ] || mkdir -p "$DATA" || { bad "无法创建数据目录 backend/data"; die_deps; }

want_fp="$(lock_fingerprint "$vpy")"
if deps_check "$vpy" && stamp_matches "$vpy" "$want_fp"; then
  ok "依赖已就绪（与锁文件一致），跳过安装"
else
  say "      安装：backend/requirements.lock.txt"
  if ! "$vpy" -m pip install --disable-pip-version-check --no-input --progress-bar off --upgrade -r "$LOCK"; then
    bad "pip 安装失败"
    say "      常见原因：网络不可达 / 代理 / pip 源不可用 / Python 版本或架构不匹配"
    say "      可尝试：export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
    die_deps
  fi
  if ! deps_check "$vpy"; then
    bad "依赖安装完成但校验不通过 → 删除 backend/.venv 后重试"
    die_deps
  fi
fi

if [ "$WITH_ASR" = "1" ]; then
  say "      安装可选语音（ASR）依赖（失败不影响主服务）"
  if ! "$vpy" -m pip install --disable-pip-version-check --no-input --progress-bar off -r "$ASR_REQ"; then
    warn "语音依赖安装失败，已跳过"
  fi
fi

{
  printf '{'
  printf '"schema":1,'
  printf '"fingerprint":"%s",' "$want_fp"
  printf '"python":"%s",' "$(slug_version "$vpy")"
  printf '"installed_at":"%s"' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '}\n'
} > "$STAMP" 2>/dev/null || true

if [ "$SETUP_ONLY" = "1" ]; then
  line
  say " 安装完成"
  line
  say "      虚拟环境    : $vpy"
  say "      数据目录    : $DATA"
  say "      下一步      : scripts/run_dev.sh 启动服务（默认 http://127.0.0.1:8787/）"
  line
  exit 0
fi

# ---------------------------------------------------------------- 启动与自证

say ""
say "· 3/3 启动服务并自证健康"
say "      地址：http://127.0.0.1:$PORT/    数据目录：backend/data"
say "      ---- 以下为 WingMan 服务原始日志 ----"

(
  cd "$BACKEND" || exit 1
  if [ "$ASCII_MODE" = "1" ]; then
    PYTHONIOENCODING=ascii:replace PYTHONUTF8=0 PYTHONUNBUFFERED=1 PORT="$PORT" \
      exec "$vpy" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
  else
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 PYTHONUNBUFFERED=1 PORT="$PORT" \
      exec "$vpy" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
  fi
) &
SERVER_PID=$!

stopping=0
cleanup() {
  if [ "$stopping" = "0" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    stopping=1
    say ""
    say "正在停止 WingMan（等待服务优雅退出）…"
    kill -TERM "$SERVER_PID" 2>/dev/null
  fi
}
trap 'cleanup' INT TERM

deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    bad "服务进程提前退出"
    say "      以上是 uvicorn 的原始输出；可先跑 scripts/run_dev.sh --doctor 体检"
    say ""
    say "退出码 1 = 其他错误"
    exit 1
  fi
  if health_ready "$vpy"; then ready=1; break; fi
  sleep 0.5
done

if [ "$ready" != "1" ]; then
  bad "等待 $HEALTH_TIMEOUT 秒后 /api/health 仍未就绪"
  say "      怎么修：另开一个窗口执行 scripts/run_dev.sh --doctor；或换端口 --port 8789"
  cleanup
  say ""
  say "退出码 1 = 其他错误"
  exit 1
fi

say ""
line
say " WingMan 已就绪"
line
say "      访问地址    : http://127.0.0.1:$PORT/"
say "      数据目录    : $DATA"
say "      服务进程    : PID $SERVER_PID"
line
say "停止服务：在这个窗口按 Ctrl+C（服务会优雅退出），或直接关闭窗口。"
line

if [ "$NO_BROWSER" = "1" ]; then
  say "      已按 --no-browser 跳过打开浏览器"
elif ! open_browser; then
  warn "没能自动打开浏览器，请手动访问 http://127.0.0.1:$PORT/"
fi

wait "$SERVER_PID"
code=$?
say ""
if [ "$code" -ne 0 ]; then
  say "WingMan 已退出（退出码 $code）"
  exit 1
fi
say "WingMan 已退出，窗口可以关闭了。"
exit 0
