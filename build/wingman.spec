# -*- mode: python ; coding: utf-8 -*-
"""WingMan 打包配置。

用 onedir 而不是 onefile：
- onefile 每次启动都要把上百 MB 解压到临时目录，首次启动要等十几秒，
  而且临时目录退出即删 —— 用户会以为「数据丢了」。
- onedir 启动是秒级的，日志和排错也直观（东西都看得见）。

语音能力已从产品中撤下（暂停转写，界面改为「敬请期待」+ 设计思路），
因此 asr 相关的第三方依赖不再进入构建 —— 它们曾让完整版膨胀到 237MB。
将来要恢复，把 hiddenimports 里的 faster-whisper / ctranslate2 / soundcard 加回来即可。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821  (PyInstaller 注入)
PROJECT_ROOT = SPEC_DIR.parent
BACKEND = PROJECT_ROOT / "backend"

# 原生窗口（pywebview）。关掉它 exe 会退回「自动开浏览器」，体积小约 40MB。
WITH_WEBVIEW = os.environ.get("WINGMAN_WITH_WEBVIEW", "1") == "1"

# ---------------------------------------------------------------- 静态资源

# docs/ 里**刻意不带 releases/**：发行说明印着这个包自己的 SHA256，
# 而一个文件不可能包含自己的哈希 —— 无论打包顺序怎么排，打进包里的那份必然是旧的。
# 这不是理论问题：v0.1.1 包里那份的「完整性校验」一节就是空的，
# v0.1.2 更难看 —— 印着 `__SHA256__` 占位符。发行说明的读者在下载页上，
# 不在程序内部，排除它顺带也少掉几个旧版本的历史噪音。
_DOCS_DIR = PROJECT_ROOT / "docs"
docs_datas = [(str(p), "docs") for p in sorted(_DOCS_DIR.glob("*.md"))]
if (_DOCS_DIR / "images").is_dir():
    docs_datas.append((str(_DOCS_DIR / "images"), "docs/images"))

datas = [
    (str(PROJECT_ROOT / "frontend"), "frontend"),
    *docs_datas,
]
samples = PROJECT_ROOT / "samples"
if samples.is_dir():
    datas.append((str(samples), "samples"))

# ---------------------------------------------------------------- 隐藏导入

# uvicorn 的协议/事件循环实现全是运行时按字符串挑的，静态分析看不见，
# 不显式列出来就会出现「源码能跑、exe 一启动就 ImportError」。
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
]

# 我们自己的包：全部子模块都收进来，避免某个路由没被静态分析到
hiddenimports += collect_submodules("app")

binaries: list = []
if WITH_WEBVIEW:
    # pywebview 在 Windows 上靠 pythonnet 调 .NET 版 WebView2，
    # 这些 DLL 静态分析看不到，必须整体收集。
    _wv_datas, _wv_binaries, _wv_hidden = collect_all("webview")
    datas += _wv_datas
    binaries += _wv_binaries
    hiddenimports += _wv_hidden
    hiddenimports += ["clr_loader", "pythonnet"]

# ---------------------------------------------------------------- 排除

# 这些是开发期才用的，进包纯属浪费体积
excludes = [
    "tkinter",
    "matplotlib",
    "pandas",
    "scipy",
    "IPython",
    "pytest",
    "PyInstaller",
    "setuptools",
    "pip",
    "PIL",
    "sqlalchemy",
    # hf_xet 是 huggingface_hub 的**可选**加速后端，被自动探测到就会进包
    # （一个纯二进制 .pyd，曾经占标准版的 12%）。
    # 剔除是纯赚：包不在时 is_package_available("hf_xet") 返回 False，
    # huggingface_hub 自动回落经典 HTTP 下载 —— 顺手消灭了
    # 「Xet CAS 服务器国内被拦 → 401」这个故障模式本身。
    "hf_xet",
    # 语音能力已撤下，这些不再进包（它们曾让完整版膨胀到 237MB）。
    # 将来恢复语音时，把这几个从 excludes 里挪回 hiddenimports。
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "av",
    "soundcard",
]


a = Analysis(                                  # noqa: F821
    [str(BACKEND / "run_wingman.py")],
    pathex=[str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)                              # noqa: F821

exe = EXE(                                     # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WingMan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 不要黑窗口；出错走日志文件 + 弹窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "build" / "wingman.ico")
    if (PROJECT_ROOT / "build" / "wingman.ico").exists() else None,
)

coll = COLLECT(                                # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WingMan",
)
