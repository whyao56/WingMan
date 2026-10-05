"""守卫：**采集层在非 Windows 上必须能被导入**（哪怕一行功能都用不了）。

## 这条守卫拦的是什么

采集是给 Windows 桌面写的（读进程内存、读剪贴板都是 Win32）。所以「非 Windows 上
功能不可用」是设计的一部分，没问题。但「**非 Windows 上连 import 都炸**」不是。

真实踩到的坑：`ctypes.wintypes` 里有这么一段

```python
class VARIANT_BOOL(ctypes._SimpleCData):
    _type_ = "v"
```

`'v'` 这个类型码**只有 Windows 版的 `_ctypes` 认识**，所以在 Linux 上
`from ctypes import wintypes` 会直接抛 `ValueError: _type_ 'v' not supported`。
而它在采集层里是**模块级语句** —— 只要有人 `import` 就炸，跟有没有调用无关。

后果特别难查：CI 跑在 `ubuntu-latest`，采集的路由（`routes_collect` → `detect`/`keys`
→ `winapi`，→ `semi` → `clipboard`）会连带导入它。于是表现是
**本地 Windows 怎么跑都是绿的、一推上去整个测试矩阵全红**，
而报错信息（`_type_ 'v' not supported`）跟「采集」两个字看起来毫无关系。

## 怎么测

没有 Linux 可用，所以**模拟**那个环境，并且逐个模块地测：

1. `sys.platform` 改成 `"linux"`（模块据此算 `IS_WINDOWS`）；
2. `builtins.__import__` 打补丁：凡是 `from ctypes import wintypes` 一律抛
   `ValueError` —— 精确复现 Linux 上的失败；
3. 清掉 `sys.modules`，把**采集层每一个模块**重新导入一遍，断言不抛异常。

每个模块单独导入（而不是只导 `routes_collect`）是有意的：真出问题时能直接指出
是哪个文件干的，而不是给一个「反正整层都导不进来」的结论。

最后把环境恢复干净 —— 不能因为测了一次「假装在 Linux」，就让后面的用例受影响。
"""

from __future__ import annotations

import builtins
import ctypes
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 采集层的全部模块。新增文件时这里要跟着加 —— 漏一个就等于漏一条通道，
# 而被漏掉的那个恰恰是最新写的、最可能带这种错的。
COLLECT_MODULES = (
    "app.collect.matrix",
    "app.collect.sqlcipher",
    "app.collect.winapi",
    "app.collect.clipboard",
    "app.collect.detect",
    "app.collect.keys",
    "app.collect.reader",
    "app.collect.pipeline",
    "app.collect.semi",
)

COLLECT_PACKAGE = "app.collect"

# 除了采集层本身，还要把路由模块也摘出缓存 —— 它是整层的入口。
# 只导采集层的话，`routes_collect` 会命中缓存、根本不重新执行，这条就成了空测。
ALSO_RELOAD = ("app.api.routes_collect",)


class _PretendLinux:
    """上下文管理器：把「Linux 上导不进 ctypes.wintypes」这件事装出来。

    只动两件东西：`sys.platform` 和 `builtins.__import__`。
    调用方要自己保证**第三方库已经先导过一遍**（见 `test_the_whole_layer_...`
    里的说明）—— 这是仿真的边界，不是被测代码的问题。
    """

    def __enter__(self) -> "_PretendLinux":
        self._platform = sys.platform
        self._import = builtins.__import__
        self._saved: dict[str, object] = {}
        self._had_ctypes_wintypes = hasattr(ctypes, "wintypes")
        self._ctypes_wintypes = getattr(ctypes, "wintypes", None)

        # 把这些模块从缓存里摘掉，否则 import_module 直接命中缓存、根本不会重新执行。
        for name in (COLLECT_PACKAGE, *ALSO_RELOAD, *COLLECT_MODULES):
            if name in sys.modules:
                self._saved[name] = sys.modules.pop(name)
        if "ctypes.wintypes" in sys.modules:
            self._saved["ctypes.wintypes"] = sys.modules.pop("ctypes.wintypes")

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            # 精确复现 Linux：取 wintypes 就失败。
            if name == "ctypes" and fromlist and "wintypes" in fromlist:
                raise ValueError("_type_ 'v' not supported")
            return self._import(name, globals, locals, fromlist, level)

        if hasattr(ctypes, "wintypes"):
            del ctypes.wintypes
        sys.platform = "linux"
        builtins.__import__ = fake_import
        return self

    def __exit__(self, *exc_info) -> bool:
        sys.platform = self._platform
        builtins.__import__ = self._import

        # 先把「假装」期间导入的模块清掉，再把原来那份放回去，
        # 免得后面的用例拿到一个装着替身的模块对象。
        for name in (*COLLECT_MODULES, *ALSO_RELOAD, COLLECT_PACKAGE):
            sys.modules.pop(name, None)
        if self._had_ctypes_wintypes:
            ctypes.wintypes = self._ctypes_wintypes
        elif hasattr(ctypes, "wintypes"):
            del ctypes.wintypes
        for name, mod in self._saved.items():
            sys.modules[name] = mod
        return False


# ---------------------------------------------------------------- 核心守卫


@pytest.mark.parametrize("module_name", COLLECT_MODULES)
def test_collect_module_imports_off_windows(module_name: str):
    """**这条就是全部意义所在。** 在 Linux 那样的环境里 import 一遍，不许抛异常。"""
    with _PretendLinux():
        module = importlib.import_module(module_name)
        assert module is not None


def test_the_whole_layer_still_imports_together_off_windows():
    """整层的入口也要过：FastAPI 路由就是从这个方向把整条链拉起来的。

    这里**必须先把整条链在真实平台上导一遍**再「假装在 Linux」：
    numpy 这类第三方库会在 import 时读 `sys.platform`（在 Linux 分支里去调
    `os.uname()`）。它们一旦进了缓存，下面那轮重导入就不会再执行它们的模块级代码，
    我们改的 platform 也就只影响采集层自己 —— 否则测出来的失败跟被测的东西无关。
    """
    importlib.import_module("app.main")
    with _PretendLinux():
        routes = importlib.import_module("app.api.routes_collect")
        assert routes.router is not None


def test_winapi_reports_itself_as_not_windows_and_says_so():
    """降级要降得干净：给 `Win32Error`，不给 `AttributeError` 之类的怪错。"""
    with _PretendLinux():
        winapi = importlib.import_module("app.collect.winapi")
        assert winapi.IS_WINDOWS is False
        with pytest.raises(winapi.Win32Error) as excinfo:
            winapi.list_processes()
        assert "Windows" in str(excinfo.value)


def test_clipboard_off_windows_degrades_to_empty_instead_of_crashing():
    """剪贴板同理：非 Windows 上要**安静地**退化成「没有内容」，而不是炸。

    这里刻意断言「返回空值」而不是「抛异常」—— 因为半自动采集是靠轮询的，
    每一轮都抛异常会把日志刷满、也会把采集循环拖垮。读不到就是读不到。
    """
    with _PretendLinux():
        cb = importlib.import_module("app.collect.clipboard")
        assert cb.IS_WINDOWS is False
        assert cb.sequence_number() == 0
        assert cb.read_text() == ""
        assert cb.write_text("随便写点") is False


def test_fallback_wintypes_covers_every_name_the_layer_uses():
    """替身要备齐采集层真正用到的每一个类型，缺一个就会在类体/调用点炸。

    这里把名字逐个钉住，而不是「断言它不是空的」—— 后者在有新模块用新类型名时
    会悄悄放过（替身仍在，但少一项，报错发生在很远的地方）。
    """
    with _PretendLinux():
        wt = importlib.import_module("app.collect.winapi").wintypes
        for name in ("BOOL", "DWORD", "UINT", "HANDLE", "HGLOBAL", "HWND"):
            assert hasattr(wt, name), f"替身少了 {name}"
            # 必须是 ctypes 类型：随手给个 int 也能「有属性」，
            # 但塞进 Structure 的 _fields_ 时会在别处以别的方式炸。
            assert isinstance(getattr(wt, name), type), f"{name} 不是 ctypes 类型"


def test_structures_can_be_constructed_off_windows():
    """结构体的类体在 import 时就求值了，所以「能构造」= 类体没炸。"""
    with _PretendLinux():
        winapi = importlib.import_module("app.collect.winapi")
        assert winapi._MBI().RegionSize == 0
        entry = winapi._PROCESSENTRY32()
        assert entry.dwSize == 0
        # 字段宽度要按 Windows 语义来，不能因为换平台就悄悄变宽/变窄。
        assert ctypes.sizeof(winapi._PROCESSENTRY32) >= 260 * ctypes.sizeof(
            ctypes.c_wchar
        )


# ---------------------------------------------------------------- 反向守卫


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有意义")
def test_on_windows_the_real_wintypes_is_used():
    """反向守卫：真身在场时不许拿替身糊弄过去。

    如果哪天有人把 `try/except` 的范围写大了、连 Windows 上也走替身，
    这条会红 —— 那才是真正的静默错误（类型宽度不对，读内存的结果会莫名其妙）。
    """
    winapi = importlib.import_module("app.collect.winapi")
    cb = importlib.import_module("app.collect.clipboard")
    assert winapi.IS_WINDOWS is True
    from ctypes import wintypes as real

    assert winapi.wintypes is real
    assert cb.wintypes is real


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有意义")
def test_guard_can_still_see_a_bare_ctypes_wintypes_import():
    """反向守卫：这条守卫本身别变成「永远通过」。

    它靠「打补丁拦住 `from ctypes import wintypes`」来发现问题。如果哪天导入机制变了、
    补丁拦不住了，上面那批用例会**静默变成空测**（全都过，但什么都没验）。
    所以这里验一次补丁确实生效：在被拦的环境里，自己去导一下必须失败。
    """
    with _PretendLinux():
        with pytest.raises(ValueError):
            exec("from ctypes import wintypes", {})
