"""一键打包成 Windows 桌面程序。

用法（在仓库根目录）：

    python scripts/build_exe.py              # 标准包（云 ASR 可用，不带本地语音模型）
    python scripts/build_exe.py --with-asr   # 完整包（额外带上 faster-whisper，体积 +300MB）
    python scripts/build_exe.py --zip        # 打包完顺手压成 zip 方便分发

产物：``dist/WingMan/WingMan.exe`` —— 整个 ``dist/WingMan`` 目录就是绿色免安装版，
拷到任何 Windows 机器双击即可（对方不需要装 Python）。

为什么默认不带 ASR：
    faster-whisper 会拖进 ctranslate2 / onnxruntime / PyAV，还会在首次使用时
    下载 150–500MB 的模型权重。默认带上的话，一个「聊天助手」安装包会变成
    400MB+，而多数人第一次只跑文字链路。所以做成可选，需要的人自己开开关。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SPEC = ROOT / "build" / "wingman.spec"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "_pyinstaller"
APP_NAME = "WingMan"

def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT), **kw)


def dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total / 1024 / 1024


def main() -> int:
    ap = argparse.ArgumentParser(description="把 WingMan 打包成 Windows 桌面程序")
    ap.add_argument("--with-asr", action="store_true",
                    help="把本地语音识别（faster-whisper）也打进包，体积会大幅增加")
    ap.add_argument("--no-webview", action="store_true",
                    help="不带原生窗口，改为自动打开浏览器（体积小约 40MB）")
    ap.add_argument("--zip", action="store_true", help="构建完压缩成 zip")
    ap.add_argument("--clean", action="store_true",
                    help="构建前清掉本项目的输出目录与工作目录（不影响 dist/ 下其它东西）")
    args = ap.parse_args()

    if args.clean:
        # 只清本项目的目标目录与工作目录。
        # 不要 rmtree(DIST) —— dist/ 下可能还有同目录其它产物或上一次的备份，
        # 一刀切会把它们一起删掉。
        for d in (DIST / APP_NAME, WORK):
            if d.exists():
                log(f"清理 {d}")
                shutil.rmtree(d, ignore_errors=True)

    # PyInstaller 会拒绝覆盖已存在的输出目录（除非 --noconfirm）。
    # 这里额外兜一层：把残留目录改名归档，而不是删掉 —— 出问题还能回退。
    app_dir = DIST / APP_NAME
    if app_dir.exists():
        stamp = time.strftime("%H%M%S")
        bak = DIST / f".prev-{stamp}"
        log(f"目标已存在，归档为 {bak.name}")
        try:
            app_dir.rename(bak)
        except OSError:
            shutil.rmtree(app_dir, ignore_errors=True)

    # 依赖自检：缺什么先补上，别等构建到一半才报错
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("缺少 PyInstaller，正在安装…")
        run([sys.executable, "-m", "pip", "install", "-q", "-r",
             str(BACKEND / "requirements-desktop.txt")])

    env = dict(os.environ)
    env["WINGMAN_WITH_ASR"] = "1" if args.with_asr else "0"
    env["WINGMAN_WITH_WEBVIEW"] = "0" if args.no_webview else "1"

    flavor = "完整包（含本地语音）" if args.with_asr else "标准包"
    # 两种风味的 hiddenimports 差别很大（完整包要多收 ctranslate2 / onnxruntime 等），
    # 共用同一个 workpath 时 PyInstaller 的增量分析缓存容易「串味」，
    # 出现「这次明明开了 ASR，包里却没有」这种难查的问题。分开最省心。
    workpath = WORK / ("full" if args.with_asr else "lite")
    log(f"开始构建：{flavor}")
    t0 = time.time()

    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", str(DIST),
        "--workpath", str(workpath),
        str(SPEC),
    ], env=env)

    elapsed = time.time() - t0
    app_dir = DIST / APP_NAME
    exe = app_dir / f"{APP_NAME}.exe"

    if not exe.exists():
        log(f"构建失败：找不到 {exe}")
        return 1

    size = dir_size_mb(app_dir)
    log(f"完成，耗时 {elapsed:.0f} 秒")
    log(f"产物：{exe}")
    log(f"整个目录 {size:.0f} MB")

    if args.zip:
        stamp = time.strftime("%Y%m%d")
        tag = "full" if args.with_asr else "lite"
        zp = DIST / f"{APP_NAME}-{tag}-{stamp}.zip"
        log(f"压缩到 {zp.name} …")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in app_dir.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(DIST))
        log(f"zip 大小 {zp.stat().st_size / 1024 / 1024:.0f} MB")

    log("")
    log("分发方式：把整个 dist/WingMan 目录给对方，双击 WingMan.exe 即可。")
    log("对方机器不需要装 Python，也不需要联网（除非要用云模型）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
