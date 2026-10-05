"""探测本机的 QQ / 微信：装在哪、什么版本、开着没、数据在哪、能不能读。

这一步的产物是**给人看的结论**，不是内部状态：用户要的是
「你的微信 4.1.13.12 我支持，但消息库还没建立，先去点开一个会话」
而不是「detect() 返回了 3 个对象」。

设计约束（都是真实踩过的）：
- 「文档」目录不能假设成 `%USERPROFILE%\\Documents` —— 用户会把它改到别的盘，
  这台机器上就是 `D:\\文档`。按约定路径找会「明明装好了却找不到数据」，还不报错。
- 「没装」「没开」「版本不在范围」「版本在范围但格式变了」「库里没有消息」
  是**五件不同的事**，给用户的下一步动作完全不同，所以必须分开表达。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .matrix import (
    EXE_TO_SPEC, SUPPORT_MATRIX, ClientSpec, Support, judge,
)
from . import winapi


# ---------------------------------------------------------------- 安装位置

# 版本不同安装目录会变，所以只列常见位置；真正可靠的是从运行中的进程取路径。
_INSTALL_HINTS: dict[str, tuple[str, ...]] = {
    "qq": (
        r"D:\Program Files\Tencent\QQNT",
        r"C:\Program Files\Tencent\QQNT",
        r"C:\Program Files (x86)\Tencent\QQNT",
        r"{localappdata}\Programs\Tencent\QQNT",
        r"D:\Program Files\Tencent\QQ",
        r"C:\Program Files\Tencent\QQ",
    ),
    "wechat": (
        r"D:\Program Files\Tencent\Weixin",
        r"C:\Program Files\Tencent\Weixin",
        r"C:\Program Files (x86)\Tencent\Weixin",
        r"{localappdata}\Programs\Tencent\Weixin",
    ),
    "wechat3": (
        r"D:\Program Files (x86)\Tencent\WeChat",
        r"C:\Program Files (x86)\Tencent\WeChat",
        r"C:\Program Files\Tencent\WeChat",
    ),
}

_INSTALL_BASENAMES: dict[str, tuple[str, ...]] = {
    "qq": ("QQ.exe",),
    "wechat": ("Weixin.exe",),
    "wechat3": ("WeChat.exe",),
}


def _expand(template: str) -> str:
    """展开 `{docs}` / `{home}` / `{userprofile}` 之类的占位符。"""
    docs = winapi.known_folder("documents")
    home = winapi.known_folder("userprofile")
    return (template
            .replace("{docs}", docs or str(Path.home() / "Documents"))
            .replace("{home}", home or str(Path.home()))
            .replace("{userprofile}", home or str(Path.home()))
            .replace("{localappdata}", winapi.known_folder("localappdata"))
            .replace("{appdata}", winapi.known_folder("appdata")))


# ---------------------------------------------------------------- 数据结构


@dataclass
class AccountData:
    """某个账号在本机留下的数据。"""

    account: str
    root: str
    db_dir: str = ""
    message_dbs: list[str] = field(default_factory=list)
    wal_bytes: int = 0
    extra_dbs: list[str] = field(default_factory=list)

    @property
    def has_messages(self) -> bool:
        return bool(self.message_dbs)

    @property
    def total_bytes(self) -> int:
        return sum(Path(p).stat().st_size for p in self.message_dbs
                   if Path(p).is_file()) if self.message_dbs else 0


@dataclass
class Detected:
    spec: ClientSpec
    installed: bool = False
    exe_path: str = ""
    version: str = ""
    running: bool = False
    pids: list[int] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    accounts: list[AccountData] = field(default_factory=list)
    support: Support | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.spec.key


# ---------------------------------------------------------------- 账号目录

_WXID_DIR = re.compile(r"^wxid_[0-9a-zA-Z_\-]+$", re.I)


def _is_account_dir(spec: ClientSpec, name: str) -> bool:
    if spec.key.startswith("wechat"):
        if name.lower() in ("all_users", "applet", "temp", "backup"):
            return False
        return bool(_WXID_DIR.match(name)) or name.lower().startswith("wxid_")
    if spec.key == "qq":
        return name.isdigit()
    return False


def _rel_under_account(rel: str) -> Path:
    """把矩阵里的相对路径（可能带 `{account}\\` 前缀）变成纯相对路径。"""
    cleaned = rel.replace("{account}/", "").replace("{account}\\", "")
    return Path(cleaned.replace("\\", "/"))


def _scan_account(spec: ClientSpec, root: Path, account_dir: Path) -> AccountData:
    acc = AccountData(account=account_dir.name, root=str(root))
    for rel in spec.layout.db_relative:
        target = account_dir / _rel_under_account(rel)
        if target.is_dir():
            acc.db_dir = str(target)
            # `*.db` 会把 -wal / -shm 之外的才算进来；附属文件另算
            acc.message_dbs = sorted(str(p) for p in target.glob("*.db") if p.is_file())
            acc.wal_bytes = sum(p.stat().st_size for p in target.glob("*.db-wal")
                                if p.is_file())
            break
    for rel in spec.layout.extra_dbs:
        p = account_dir / _rel_under_account(rel)
        if p.is_file():
            acc.extra_dbs.append(str(p))
    return acc


def _find_roots(spec: ClientSpec) -> list[str]:
    out: list[str] = []
    for tpl in spec.layout.root_candidates:
        p = Path(_expand(tpl))
        if p.is_dir() and str(p) not in out:
            out.append(str(p))
    return out


# ---------------------------------------------------------------- 主入口


def detect_client(key: str, *, deep: bool = True) -> Detected | None:
    """探测一个客户端。`deep=False` 时只查进程与安装，不扫数据目录。"""
    spec = SUPPORT_MATRIX.get(key)
    if spec is None:
        return None
    det = Detected(spec=spec)

    # --- 进程
    if winapi.IS_WINDOWS:
        try:
            procs = winapi.find_processes(spec.exe_names)
        except winapi.Win32Error as exc:
            det.problems.append(f"无法枚举进程：{exc}")
            procs = []
        det.pids = [p.pid for p in procs]
        det.running = bool(procs)
        for p in procs:
            path = winapi.process_path(p.pid)
            # 微信 4.x 的子进程也叫 Weixin.exe，取第一个能读出路径的当主进程
            if path and Path(path).name.lower() in {n.lower() for n in spec.exe_names}:
                det.exe_path = path
                det.version = winapi.file_version(path)
                break

    # --- 安装（进程读不到路径时兜底）
    if not det.exe_path:
        hints = tuple(_expand(h) for h in _INSTALL_HINTS.get(key, ()))
        path, version = winapi.exe_version_search(hints, _INSTALL_BASENAMES.get(key, ()))
        if path:
            det.exe_path, det.version = path, version
    det.installed = bool(det.exe_path) or det.running

    if deep and det.installed:
        det.roots = _find_roots(spec)
        for root in det.roots:
            try:
                entries = [d for d in Path(root).iterdir() if d.is_dir()]
            except OSError as exc:
                det.problems.append(f"读不到 {root}：{exc}")
                continue
            for d in entries:
                if _is_account_dir(spec, d.name):
                    det.accounts.append(_scan_account(spec, Path(root), d))
            if det.accounts:
                break   # 第一个命中的根目录就是它

    det.support = judge(spec, det.version, installed=det.installed,
                        running=det.running)
    # 版本没问题但库里没东西时，要把「为什么」说得更具体
    if det.support.supported and det.installed and det.accounts:
        if not any(a.has_messages for a in det.accounts):
            det.support.headline = (
                f"{det.spec.display_name} {det.version} 可以读，但本机还没有消息库")
            det.support.actions = [
                "在客户端里**打开几个聊天窗口**（消息库是按需建立的，"
                "没点开过的会话可能还没有本地文件）。",
                "打开后再点一次「探测」。",
                "如果一直不出现，说明这个版本改成了不落地消息，那就只能走半自动采集。",
            ]
    elif det.support.supported and det.installed and det.roots and not det.accounts:
        det.support.headline = (
            f"找到了 {det.spec.display_name} 的数据目录，但里面没有账号数据")
        det.support.actions = [
            f"数据目录：{'、'.join(det.roots)}",
            "确认客户端已经**登录**过（只是打开、没登录的话不会生成账号目录）。",
        ]
    return det


def detect_all(*, deep: bool = True) -> list[Detected]:
    out: list[Detected] = []
    for key in SUPPORT_MATRIX:
        det = detect_client(key, deep=deep)
        if det is not None:
            out.append(det)
    return out


def summarise(dets: list[Detected]) -> dict:
    """给前端的总览：能自动读的有哪些、需要用户做什么。"""
    readable = [d for d in dets if d.support and d.support.supported
                and d.installed and any(a.has_messages for a in d.accounts)]
    todo: list[str] = []
    for d in dets:
        if d.support:
            todo.extend(f"{d.spec.display_name}：{a}" for a in d.support.actions[:1])
    return {
        "auto_ready": [d.key for d in readable],
        "clients": [describe(d) for d in dets],
        "next_steps": todo,
    }


def describe(det: Detected) -> dict:
    sup = det.support
    return {
        "client": det.key,
        "name": det.spec.display_name,
        "installed": det.installed,
        "running": det.running,
        "version": det.version,
        "exe_path": det.exe_path,
        "verdict": sup.verdict if sup else "unknown",
        "headline": sup.headline if sup else "",
        "actions": sup.actions if sup else [],
        "risks": sup.risks if sup else [],
        "roots": det.roots,
        "accounts": [{
            "account": a.account,
            "db_dir": a.db_dir,
            "message_db_count": len(a.message_dbs),
            "message_dbs": [Path(p).name for p in a.message_dbs],
            "database_bytes": sum(Path(p).stat().st_size for p in a.message_dbs
                                  if Path(p).is_file()),
            "unmerged_wal_bytes": a.wal_bytes,
            "has_messages": a.has_messages,
        } for a in det.accounts],
        "problems": det.problems,
    }
