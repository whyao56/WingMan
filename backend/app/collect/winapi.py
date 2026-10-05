"""Win32 的最小封装：列进程、读进程路径、读文件版本、按区域读进程内存。

为什么自己写而不用 psutil / pywin32：
这个功能是**可选**的（不装也能用导入和半自动），而它要的本事只有四件，
用 ctypes 直接调 Win32 就够了。为了四件事往一个桌面工具里塞两个原生依赖，
会让安装包和排障都变复杂 —— 装机时少一个依赖、少一个失败点。

内存读取这里有个必须记住的坑：`ReadProcessMemory` **只要目标范围里有一段
不可读就整块失败**，返回 0 且不写任何数据。所以绝不能按「起点 ± N 字节」
去读，必须按 `VirtualQueryEx` 给出的区域为单位读。按固定窗口读会得到
一片空白，而且不报错 —— 表现为「扫了 831 MB，一个候选都没试」。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def _fallback_wintypes() -> object:
    """非 Windows 上的一小份替身，只覆盖采集层真正用到的那几个类型。

    `ctypes.wintypes` 里有个 `VARIANT_BOOL` 用了 `'v'` 类型码，而只有 Windows 版的
    `_ctypes` 认识这个码 —— 所以**在 Linux 上 `from ctypes import wintypes` 会直接抛
    ValueError**。这条导入是模块级的，意味着「仅仅 import 一下」都会炸。
    CI 跑在 Ubuntu 上、且采集层的路由会连带导入本模块，所以必须让它在非 Windows 上
    也能被导入（真正调 Win32 的函数在 `_k32()` 里就拦住了，走不到这些结构体）。

    这份替身是**全采集层唯一的一份**（`clipboard.py` 也从这里取），
    所以往下面加名字时，顺手把用到它的模块一起想一遍。
    """
    class _Fallback:
        # 值要和 Windows 上的真身等价：那两个平台上 c_ulong 的宽度不一样，
        # 替身统一按 Windows 的语义（DWORD/UINT 都是 4 字节）来。
        BOOL = ctypes.c_long
        DWORD = ctypes.c_uint32
        UINT = ctypes.c_uint
        HANDLE = ctypes.c_void_p
        HGLOBAL = ctypes.c_void_p      # wintypes 里 HGLOBAL 就是 HANDLE
        HWND = ctypes.c_void_p         # 同上

    return _Fallback()


try:
    from ctypes import wintypes
except Exception:                      # pragma: no cover - 只在非 Windows 平台走到
    if IS_WINDOWS:
        # Windows 上导不进来是真故障，不许悄悄用替身把它盖过去。
        raise
    wintypes = _fallback_wintypes()


# ---- 权限 / 状态常量
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
# 可读的页保护位：R / RW / WRCOPY / EXEC_R / EXEC_RW / EXEC_WRCOPY
_READABLE_PROTECT = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


class Win32Error(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _k32():
    if not IS_WINDOWS:
        raise Win32Error("这些能力只在 Windows 上可用")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.ReadProcessMemory.restype = wintypes.BOOL
    dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    return dll


# ---------------------------------------------------------------- 进程


def list_processes() -> list[ProcInfo]:
    """列出所有进程（只要 pid 和 exe 名）。"""
    dll = _k32()
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    snap = dll.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID or not snap:
        raise Win32Error(f"CreateToolhelp32Snapshot 失败（{ctypes.get_last_error()}）")
    out: list[ProcInfo] = []
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = dll.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(ProcInfo(pid=int(entry.th32ProcessID),
                                name=str(entry.szExeFile or "")))
            ok = dll.Process32NextW(snap, ctypes.byref(entry))
    finally:
        dll.CloseHandle(snap)
    return out


def find_processes(names: tuple[str, ...]) -> list[ProcInfo]:
    wanted = {n.lower() for n in names}
    return [p for p in list_processes() if p.name.lower() in wanted]


def process_path(pid: int) -> str:
    """拿进程的可执行文件路径。取不到返回空串（权限不足很常见，不抛异常）。"""
    dll = _k32()
    for access in (PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_QUERY_INFORMATION):
        h = dll.OpenProcess(access, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if dll.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            dll.CloseHandle(h)
    return ""


# ---------------------------------------------------------------- 文件版本


def file_version(path: str | Path) -> str:
    """读 exe 的 FileVersion（例如 '9.9.20.37051'）。

    用 GetFileVersionInfo 系列而不是「跑一次 exe --version」：
    后者会真的把客户端再启动一遍，在用户机器上做这种事不合适。
    """
    p = Path(path)
    if not IS_WINDOWS or not p.is_file():
        return ""
    ver_dll = ctypes.WinDLL("version", use_last_error=True)
    ver_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    size = ver_dll.GetFileVersionInfoSizeW(str(p), None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver_dll.GetFileVersionInfoW(str(p), 0, size, buf):
        return ""
    r = ctypes.c_void_p()
    length = wintypes.UINT()

    def _clean(text: str) -> str:
        # VerQueryValue 返回的长度**含结尾的 NUL**，wstring_at 会把它带进来；
        # 结果是版本号字符串末尾多一个 '\x00'，和矩阵里的版本永远比不相等，
        # 于是「实测过的版本」被一路判成「没实测过」。
        return text.replace("\x00", "").strip()

    # 0x0804 = 简体中文，0x0409 = 英文；两个都试，取先命中的
    for lang in (0x0804, 0x0409, 0):
        trans = f"\\StringFileInfo\\{lang:04x}04b0\\FileVersion"
        if ver_dll.VerQueryValueW(buf, trans, ctypes.byref(r), ctypes.byref(length)) and length.value:
            return _clean(ctypes.wstring_at(r, length.value))
    # 退一步：从固定版本信息里拼
    if ver_dll.VerQueryValueW(buf, "\\", ctypes.byref(r), ctypes.byref(length)) and length.value:
        class _FIXED(ctypes.Structure):
            _fields_ = [("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                        ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                        ("dwProductVersionMS", wintypes.DWORD),
                        ("dwProductVersionLS", wintypes.DWORD),
                        ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
                        ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
                        ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
                        ("dwFileDateLS", wintypes.DWORD)]
        fx = ctypes.cast(r, ctypes.POINTER(_FIXED)).contents
        return (f"{fx.dwFileVersionMS >> 16}.{fx.dwFileVersionMS & 0xFFFF}."
                f"{fx.dwFileVersionLS >> 16}.{fx.dwFileVersionLS & 0xFFFF}")
    return ""


def exe_version_search(paths: tuple[str, ...], basenames: tuple[str, ...]) -> tuple[str, str]:
    """在几个候选目录里找一个 exe，返回 (完整路径, 版本号)。"""
    for base in paths:
        d = Path(base)
        if not d.is_dir():
            continue
        for name in basenames:
            exe = d / name
            if exe.is_file():
                return str(exe), file_version(exe)
    return "", ""


# ---------------------------------------------------------------- 已知文件夹

# 这几个 GUID 是 Windows 规定的固定值，不能自己编。
_FOLDER_IDS = {
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "localappdata": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "appdata": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    "userprofile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
}


def known_folder(name: str) -> str:
    """取 Windows 的「已知文件夹」真实路径。

    不能假设 `%USERPROFILE%\\Documents` —— 用户的「文档」经常被改到别的盘
    （这台机器上就是 `D:\\文档`），而微信/QQ 按系统给的位置存数据。
    按约定路径去找会「明明装好了却找不到数据」，且不会报任何错。
    """
    if not IS_WINDOWS:
        return ""
    guid = _FOLDER_IDS.get(name)
    if not guid:
        return ""
    try:
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        g = _GUID()
        if ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(g)) != 0:
            return ""
        out = ctypes.c_wchar_p()
        # SHGetKnownFolderPath(rfid, dwFlags, hToken, ppszPath)
        if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None,
                                        ctypes.byref(out)) != 0:
            return ""
        path = out.value or ""
        if path:
            ctypes.WinDLL("ole32").CoTaskMemFree(out)
        return path
    except Exception:
        return ""


# ---------------------------------------------------------------- 进程内存


class MemoryReader:
    """按区域读一个进程的内存。

    用法固定是「先枚举区域、再逐区域回调」，因为读内存会失败，
    而失败的粒度是「一整个请求」—— 按区域为单位才可控。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self._dll = _k32()
        self.handle = self._dll.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not self.handle:
            err = ctypes.get_last_error()
            raise Win32Error(
                f"无法读取进程 {pid} 的内存（错误码 {err}）。"
                "常见原因：进程不属于当前用户，或需要以管理员身份运行。")

    def __enter__(self) -> "MemoryReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.handle:
            self._dll.CloseHandle(self.handle)
            self.handle = None

    def read(self, addr: int, size: int) -> bytes:
        """读一段内存。任何一部分不可读都会失败，此时返回空字节串。"""
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = self._dll.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf,
                                        size, ctypes.byref(got))
        return buf.raw[: got.value] if ok else b""

    def regions(self, *, max_region: int = 0x4000000, private_only: bool = False):
        """产出 (基址, 内容) 或者 (基址, 长度) —— `with_content=False` 时只报位置。

        区域上限 `max_region` 是防呆：有些映射会声明几百 MB，
        整块读回来会把内存吃爆，而里面通常没有我们要的东西。
        """
        mbi = _MBI()
        addr = 0
        limit = 0x7FFFFFFFFFFF
        while addr < limit:
            if not self._dll.VirtualQueryEx(self.handle, ctypes.c_void_p(addr),
                                            ctypes.byref(mbi), ctypes.sizeof(mbi)):
                addr += 0x1000
                continue
            size = mbi.RegionSize or 0x1000
            readable = (mbi.State == MEM_COMMIT
                        and not (mbi.Protect & PAGE_GUARD)
                        and mbi.Protect != PAGE_NOACCESS
                        and mbi.Protect in _READABLE_PROTECT
                        and size <= max_region)
            if readable and private_only and mbi.Type != MEM_PRIVATE:
                readable = False
            if readable:
                yield addr, size
            addr += size

    def iterate(self, callback, *, max_region: int = 0x4000000,
                private_only: bool = False) -> tuple[int, int]:
        """逐区域读出内容并交给 callback(基址, 内容)。返回 (区域数, 总字节数)。

        用回调而不是把整块内存攒在列表里：一个 800 MB 的进程攒满了，
        进程自己就要被换页拖垮，而调用方通常只需要「边读边判」。
        """
        count = total = 0
        for base, size in self.regions(max_region=max_region,
                                       private_only=private_only):
            blob = self.read(base, size)
            if not blob:
                continue
            count += 1
            total += len(blob)
            result = callback(base, blob)
            if result is False:      # 调用方可以提前收工
                break
        return count, total

    def find(self, needle: bytes, *, max_hits: int = 40,
             max_region: int = 0x4000000) -> list[int]:
        """在内存里找一段字节，返回虚拟地址列表。"""
        hits: list[int] = []

        def cb(base: int, blob: bytes) -> bool:
            start = 0
            while len(hits) < max_hits:
                i = blob.find(needle, start)
                if i < 0:
                    return True
                hits.append(base + i)
                start = i + 1
            return False

        self.iterate(cb, max_region=max_region)
        return hits
