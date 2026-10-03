#!/usr/bin/env bash
# ChatWing 开发启动脚本（macOS / Linux / Git Bash）
# 自动建虚拟环境、装依赖、起服务。

set -euo pipefail
cd "$(dirname "$0")/../backend"

PY=${PYTHON:-python3}
PORT=${PORT:-8787}

if [ ! -d ".venv" ]; then
  echo "→ 创建虚拟环境 .venv"
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
  source .venv/Scripts/activate     # Git Bash on Windows
fi

echo "→ 安装依赖"
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

# 语音能力是可选依赖，装了才用得上；装失败不影响主服务
if [ "${WITH_ASR:-0}" = "1" ]; then
  echo "→ 安装语音依赖（可选）"
  python -m pip install -q -r requirements-asr.txt || echo "  ! 语音依赖安装失败，已跳过"
fi

echo
echo "→ 启动 http://127.0.0.1:${PORT}"
echo "  （控制台就是这个地址，Ctrl+C 停止）"
echo
exec python -m uvicorn app.main:app --reload --host 127.0.0.1 --port "$PORT"
