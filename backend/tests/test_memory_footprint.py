"""内存占用的守卫。

这里盯的是一笔**很容易被忽略、数额却最大**的开销：OpenBLAS 的线程池。

numpy 在本项目里只干一件事 —— 几千条 512 维向量算余弦相似度
（`memory/retriever.py`）。这个规模用不上多线程 BLAS，但 OpenBLAS 默认按
**逻辑核数**开线程，并给每个线程预留工作缓冲。实测（24 逻辑核、同一 venv、
只 `import numpy`）：

    默认                      提交 760.6 MB   线程 27
    OPENBLAS_NUM_THREADS=1    提交  19.4 MB   线程  4

也就是说：**光把 numpy 导进来这一下，就占了这个程序内存的九成**。限流之后
整个服务（含 FastAPI、SQLite、本项目全部代码）也才 43 MB。

限流必须发生在 numpy 被导入之前 —— OpenBLAS 在 import 期就把线程池建好了，
之后再设环境变量等于没设。所以这里用**干净的子解释器**验证真实顺序，
而不是在当前进程里断言环境变量：那时 numpy 早被别的用例导入过了，
即使变量设错也看不出来。

两种跑法都支持：

    cd backend
    python -m pytest tests/test_memory_footprint.py -q
    python tests/test_memory_footprint.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

try:  # pytest 可选：没有 pytest 时用本文件的 main() 直跑
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

_IS_WINDOWS = sys.platform == "win32"

# 在干净解释器里跑。三行输出，顺序固定：
#   1) 包导入后 OPENBLAS_NUM_THREADS 的值
#   2) 那一刻 numpy 有没有被顺带拽进来（应为 False —— 拽进来了限流就晚了）
#   3) 再导入 numpy 之后的提交内存（MB）；非 Windows 报 -1
_PROBE = r'''
import sys
sys.path.insert(0, r"{backend}")

import app          # 先让包把自己的默认值设上（它必须早于 numpy）
import os

print(os.environ.get("OPENBLAS_NUM_THREADS"))
print("numpy" in sys.modules)

import numpy        # 现在才把 numpy 放进来
assert hasattr(numpy, "ndarray"), "numpy 没真的导进来，这个探针就没意义了"

_used = -1.0
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 这一行不能省：不显式声明 restype，伪句柄会被当成 32 位 int 截断，
    # 整个调用会静默失败（返回 0、字段全是 0）。
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.argtypes = []

    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    _p = _PMC()
    _p.cb = ctypes.sizeof(_PMC)
    _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(_p), _p.cb)
    _used = round(_p.PagefileUsage / 1048576, 1)

print(_used)
'''


def _run_probe() -> list[str]:
    """在干净子解释器里跑探针，返回它的非空输出行。"""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.replace("{backend}", str(BACKEND))],
        capture_output=True, text=True, timeout=180, cwd=str(BACKEND),
    )
    assert proc.returncode == 0, (
        f"探针子进程退出码 {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 3, f"探针输出不像预期：{proc.stdout!r}"
    return lines


def test_the_package_caps_blas_threads_before_numpy_arrives() -> None:
    """`import app` 就要把 BLAS 线程限流，且必须**早于** numpy。

    两件事一起验，缺一不可：

    1. 变量真的被设成了 1（否则 OpenBLAS 按逻辑核数开线程）；
    2. 设的那一刻 numpy 还没进来 —— 设得对但时机晚，效果一样是零。
    """
    lines = _run_probe()
    assert lines[0] == "1", (
        f"OPENBLAS_NUM_THREADS 应为 '1'，实际 {lines[0]!r}。"
        "不设的话 OpenBLAS 会按逻辑核数开线程并预留缓冲 —— "
        "24 核机器上光 import numpy 就提交 760 MB。"
    )
    assert lines[1] == "False", (
        "`import app` 的时候 numpy 就已经被导入了 —— 限流设得太晚，等于没设。"
        "环境变量必须在任何模块碰 numpy 之前设好（现在是 app/__init__.py）。"
    )


def test_importing_numpy_stays_within_a_sane_memory_budget() -> None:
    """限流生效后，导入 numpy 带来的提交内存要留在合理量级。

    这条是「效果」守卫，比只检查环境变量更实在：变量设了但没起作用
    （比如被别处的赋值盖掉、或 numpy 换了后端）同样会在这里暴露。
    """
    if not _IS_WINDOWS:
        return          # 提交内存的读法只在 Windows 上做了；CI 在 ubuntu 上跑

    used = float(_run_probe()[2])
    # 实测：限流后 19.4 MB，不限流 760.6 MB。阈值取 200 MB ——
    # 离两边都有充足余量，不会因为机器负载抖动而误报。
    assert used < 200, (
        f"导入 app + numpy 后用掉了 {used:.0f} MB 提交内存。"
        "正常应该在 30 MB 上下；到了几百 MB 说明 BLAS 线程限流没生效。"
    )


def _collect_tests() -> list:
    return [
        obj for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]


def main() -> int:
    failed = 0
    for fn in _collect_tests():
        try:
            fn()
        except AssertionError as exc:
            print(f"[FAIL] {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:      # pragma: no cover - 直跑时的兜底
            print(f"[ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"[OK]   {fn.__name__}")
    print(f"\n{'全部通过' if not failed else f'{failed} 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
