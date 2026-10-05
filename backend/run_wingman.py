"""PyInstaller / 直接运行 的统一入口。

打包后进程的入口就是这里；开发时也可以 ``python run_wingman.py``，
行为与 exe 一致（方便对照排查「源码能跑、exe 不能跑」的问题）。
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path

# 冻结态要把 backend 目录加进 sys.path，否则 `import app` 找不到
if getattr(sys, "frozen", False):
    _base = Path(sys.executable).resolve().parent
    for _cand in (_base, _base / "_internal"):
        if (_cand / "app").is_dir() and str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    from app.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    # 冻结后子进程会重新执行入口脚本，必须挡在最前面
    multiprocessing.freeze_support()
    # 高 DPI 下界面别糊（Windows）
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
