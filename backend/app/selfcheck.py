"""启动自检：用大白话告诉用户「你现在能用什么、缺什么、怎么补」。

为什么单独做这个模块？因为打包成 exe 之后，用户看不到控制台输出，
出了问题只能靠猜。自检面板是他唯一能自己排查的地方 —— 所以每一条
都不许只报「失败」，必须带一句人话说明和一个可执行的下一步。

状态只有三档，含义固定：
- ``ok``   可用，不用管
- ``warn`` 能用但会打折，或者非必需项缺失
- ``fail`` 这项功能现在是坏的，会给用户错误结果
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    APP_DIR,
    DATA_DIR,
    DOCS_DIR,
    FRONTEND_DIR,
    IS_FROZEN,
    LOG_DIR,
    RESOURCE_DIR,
)

# ---------------------------------------------------------------- 数据结构


@dataclass
class CheckItem:
    key: str
    label: str
    status: str          # ok | warn | fail
    detail: str
    fix: str = ""        # 只有非 ok 时才填，写清「点哪里能修好」
    group: str = "其他"

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "group": self.group,
        }


@dataclass
class SelfCheck:
    items: list[CheckItem] = field(default_factory=list)

    @property
    def worst(self) -> str:
        if any(i.status == "fail" for i in self.items):
            return "fail"
        if any(i.status == "warn" for i in self.items):
            return "warn"
        return "ok"

    def summary(self) -> str:
        fails = [i for i in self.items if i.status == "fail"]
        warns = [i for i in self.items if i.status == "warn"]
        if fails:
            return f"有 {len(fails)} 项必须处理，否则功能不完整：{'、'.join(i.label for i in fails)}"
        if warns:
            return f"核心功能可用。有 {len(warns)} 项可以再补：{'、'.join(i.label for i in warns)}"
        return "全部就绪，可以直接用了。"

    def as_dict(self) -> dict[str, Any]:
        return {
            "worst": self.worst,
            "summary": self.summary(),
            "items": [i.as_dict() for i in self.items],
        }


# ---------------------------------------------------------------- 检查项


def _check_runtime(s: SelfCheck) -> None:
    mode = "打包版（exe）" if IS_FROZEN else "源码运行"
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    s.items.append(CheckItem(
        key="runtime",
        label="运行环境",
        status="ok",
        detail=f"WingMan v{__version__} · {mode} · Python {py} · {platform.system()} {platform.release()}",
        group="运行环境",
    ))


def _check_storage(s: SelfCheck) -> None:
    """数据目录可写 —— 这是冻结态最容易坏、也最要命的一项。"""
    probe = DATA_DIR / ".write_probe"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
        err = ""
    except OSError as exc:
        writable = False
        err = str(exc)

    s.items.append(CheckItem(
        key="storage",
        label="数据目录可写",
        status="ok" if writable else "fail",
        detail=f"数据存在 {DATA_DIR}" if writable else f"无法写入 {DATA_DIR}：{err}",
        fix="" if writable else "检查该目录权限，或把程序换到有写入权限的位置再试。",
        group="运行环境",
    ))

    s.items.append(CheckItem(
        key="log_dir",
        label="日志目录",
        status="ok",
        detail=str(LOG_DIR),
        group="运行环境",
    ))


def _check_frontend(s: SelfCheck) -> None:
    index = FRONTEND_DIR / "index.html"
    ok = index.exists()
    s.items.append(CheckItem(
        key="frontend",
        label="界面资源",
        status="ok" if ok else "fail",
        detail=f"已加载 {index}" if ok else f"找不到界面文件：{index}",
        fix="" if ok else "程序文件不完整，重新下载完整的程序包。",
        group="运行环境",
    ))
    if not DOCS_DIR.exists():
        s.items.append(CheckItem(
            key="docs",
            label="文档",
            status="warn",
            detail="未随包附带文档目录",
            fix="不影响使用。文档可在 GitHub 仓库查看。",
            group="运行环境",
        ))


def _check_database(s: SelfCheck, ctx: Any) -> None:
    try:
        counts = ctx.store.counts()
        db_path = str(ctx.store.db_path)
        size = Path(db_path).stat().st_size if Path(db_path).exists() else 0
        detail = (
            f"{counts['chats']} 个会话 · {counts['messages']} 条消息 · "
            f"{counts['facts']} 条事实 · 库文件 {size / 1024:.0f} KB"
        )
        if counts["chats"] == 0:
            s.items.append(CheckItem(
                key="database",
                label="记忆库",
                status="warn",
                detail="记忆库是空的，还没有导入任何聊天记录",
                fix="去「导入」页上传或粘贴一段聊天记录，之后所有分析都会基于它。",
                group="核心能力",
            ))
        else:
            s.items.append(CheckItem(
                key="database",
                label="记忆库",
                status="ok",
                detail=detail,
                group="核心能力",
            ))
    except Exception as exc:
        s.items.append(CheckItem(
            key="database",
            label="记忆库",
            status="fail",
            detail=f"打不开数据库：{type(exc).__name__}: {exc}",
            fix="如果反复失败，把数据目录里的 .db 文件改名备份，重建一个空库试试。",
            group="核心能力",
        ))


def _check_llm(s: SelfCheck, ctx: Any) -> None:
    try:
        p = ctx.llm
        name = getattr(p, "name", "unknown")
        if name == "mock":
            s.items.append(CheckItem(
                key="llm",
                label="大模型",
                status="warn",
                detail="当前用的是内置演示引擎（Mock），给出的分析和回复是固定规则生成的，**不能当真实建议用**",
                fix="去「设置」页填 base_url / api_key / model，模型名写实际要调的模型。",
                group="核心能力",
            ))
        else:
            s.items.append(CheckItem(
                key="llm",
                label="大模型",
                status="ok",
                detail=f"{name} · {getattr(p, 'note', '')}",
                group="核心能力",
            ))
    except Exception as exc:
        s.items.append(CheckItem(
            key="llm",
            label="大模型",
            status="fail",
            detail=f"初始化失败：{type(exc).__name__}: {exc}",
            fix="到「设置」页点「测试」看具体报错，常见原因是 api_key 或 base_url 写错。",
            group="核心能力",
        ))


def _check_embedder(s: SelfCheck, ctx: Any) -> None:
    try:
        e = ctx.embedder
        name = getattr(e, "name", "unknown")
        if name == "hash":
            s.items.append(CheckItem(
                key="embedder",
                label="记忆检索",
                status="warn",
                detail="用的是本地兜底向量（按字面哈希），只能匹配字面相近的内容，**不认同义词**",
                fix="配好大模型后，在「设置」里把 embedder 设为 auto 或 cloud，检索质量会明显变好。",
                group="核心能力",
            ))
        else:
            s.items.append(CheckItem(
                key="embedder",
                label="记忆检索",
                status="ok",
                detail=f"{name} · 维度 {getattr(e, 'dim', '?')}",
                group="核心能力",
            ))
    except Exception as exc:
        s.items.append(CheckItem(
            key="embedder",
            label="记忆检索",
            status="fail",
            detail=f"初始化失败：{type(exc).__name__}: {exc}",
            fix="把 embedder 设为 hash 可以立即恢复（精度较低但一定可用）。",
            group="核心能力",
        ))


# ---------------------------------------------------------------- 入口


def run(ctx: Any) -> SelfCheck:
    """跑一遍全部检查。同步函数，调用方用 asyncio.to_thread 包一下。"""
    s = SelfCheck()
    _check_runtime(s)
    _check_storage(s)
    _check_frontend(s)
    _check_database(s, ctx)
    _check_llm(s, ctx)
    _check_embedder(s, ctx)
    return s


def runtime_paths() -> dict[str, str]:
    """给「打开文件夹」按钮用。"""
    return {
        "data": str(DATA_DIR),
        "logs": str(LOG_DIR),
        "app": str(APP_DIR),
        "resource": str(RESOURCE_DIR),
    }
