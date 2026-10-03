@echo off
REM WingMan 开发启动脚本（Windows）
REM 自动建虚拟环境、装依赖、起服务。
setlocal
cd /d "%~dp0..\backend"

if "%PORT%"=="" set PORT=8787

if not exist ".venv" (
  echo -^> 创建虚拟环境 .venv
  python -m venv .venv
  if errorlevel 1 (
    echo 创建虚拟环境失败，请确认已安装 Python 3.11+ 并加入 PATH。
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo -^> 安装依赖
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo 依赖安装失败。
  exit /b 1
)

if "%WITH_ASR%"=="1" (
  echo -^> 安装语音依赖（可选）
  python -m pip install -q -r requirements-asr.txt
  if errorlevel 1 echo   ! 语音依赖安装失败，已跳过（不影响主服务）
)

echo.
echo -^> 启动 http://127.0.0.1:%PORT%
echo   控制台就是这个地址，按 Ctrl+C 停止
echo.
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port %PORT%

endlocal
