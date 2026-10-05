# -*- mode: python ; coding: utf-8 -*-
"""WingMan 打包配置。

用 onedir 而不是 onefile：
- onefile 每次启动都要把上百 MB 解压到临时目录，首次启动要等十几秒，
  而且临时目录退出即删 —— 用户会以为「数据丢了」。
- onedir 启动是秒级的，日志和排错也直观（东西都看得见）。

语音相关的大依赖（faster-whisper / ctranslate2）默认**不打进包**：
带上它们体积会从 ~90MB 涨到 ~400MB，而大多数用户第一次跑只会用文字链路。
要带就设环境变量 ``WINGMAN_WITH_ASR=1`` 再构建。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821  (PyInstaller 注入)
PROJECT_ROOT = SPEC_DIR.parent
BACKEND = PROJECT_ROOT / "backend"

WITH_ASR = os.environ.get("WINGMAN_WITH_ASR", "0") == "1"
# 原生窗口（pywebview）。关掉它 exe 会退回「自动开浏览器」，体积小约 40MB。
WITH_WEBVIEW = os.environ.get("WINGMAN_WITH_WEBVIEW", "1") == "1"

# ---------------------------------------------------------------- 静态资源

datas = [
    (str(PROJECT_ROOT / "frontend"), "frontend"),
    (str(PROJECT_ROOT / "docs"), "docs"),
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

# 音频采集库：**两种包都要带**。
# 很容易误以为「只有本地转写才需要录音」—— 不是的：走云 ASR 时同样要先用
# soundcard 抓系统回环/麦克风，再把音频发给云端。少了它，两条语音路线都废。
# 它本身是纯 Python（Windows 下走 COM），体积可以忽略，没有理由不打进去。
hiddenimports += ["soundcard", "soundcard.mediafoundation"]

binaries: list = []
if WITH_WEBVIEW:
    # pywebview 在 Windows 上靠 pythonnet 调 .NET 版 WebView2，
    # 这些 DLL 静态分析看不到，必须整体收集。
    _wv_datas, _wv_binaries, _wv_hidden = collect_all("webview")
    datas += _wv_datas
    binaries += _wv_binaries
    hiddenimports += _wv_hidden
    hiddenimports += ["clr_loader", "pythonnet"]

if WITH_ASR:
    hiddenimports += collect_submodules("faster_whisper")
    hiddenimports += ["ctranslate2", "tokenizers", "av", "onnxruntime"]
    # 不要对 ctranslate2 用 collect_all：它会把整个包目录当数据收进来（+60MB），
    # 而 PyInstaller 默认已经正确带上了 ctranslate2.dll / _ext.pyd / libiomp5md.dll，
    # 并且会**自动剔除** cudnn64_9.dll 这类只在 GPU 上才需要的库。实测够用。

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
    # （一个纯二进制 .pyd，标准版里占 9.06MB —— 相当于标准版的 12%）。
    #
    # 我们永远用不到它，所以剔除是**纯赚**，不是取舍：
    #   1) apply_endpoint() 无条件设 HF_HUB_DISABLE_XET=1，
    #      huggingface_hub 的 is_xet_available() 第一行就直接 return False，
    #      那 8 处 `from hf_xet import ...` 一个都不会执行（全是函数内导入）；
    #   2) 退一步说，就算哪个调用点忘了设这个环境变量，包没打进去时
    #      is_package_available("hf_xet") 同样返回 False → 自动回落经典 HTTP 下载。
    # 换句话说：剔除它顺手消灭了「Xet CAS 服务器国内被拦 → 401」这个故障模式本身。
    #
    # 剔除后 huggingface_hub 的 snapshot_download / hf_hub_download / HfApi
    # 全部照常可用（已实测）。
    "hf_xet",
]

if not WITH_ASR:
    excludes += ["faster_whisper", "ctranslate2", "tokenizers", "onnxruntime", "av"]


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
