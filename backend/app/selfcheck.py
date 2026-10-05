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


def _check_asr(s: SelfCheck, ctx: Any) -> None:
    """语音转写这一项，重点是**分清「哪一步没做」**。

    用户面对的是「两条路线二选一」：云 ASR 还是本地模型。选了一条但没配全时，
    factory 会**静默退化成 Mock** —— 界面上看不出区别，只是识别结果是假的。
    所以这里反过来读「用户本来想走哪条路」，再指出那条路上缺的那一环，
    而不是笼统地说一句「语音不可用」。
    """
    try:
        eng = ctx.asr
        name = getattr(eng, "name", "unknown")
        # 用 ready 而不是 available：available 只说明「引擎对象构造得出来」，
        # 本地 whisper 在模型没下载时 available 也是 True —— 只看它会把这
        # 种情况报成绿色的「已完成」，用户配完发现没反应又找不到原因。
        ready = bool(getattr(eng, "ready", getattr(eng, "available", True)))

        if name != "mock" and ready:
            s.items.append(CheckItem(
                key="asr",
                label="语音转写",
                status="ok",
                detail=f"{name} · {getattr(eng, 'note', '')}",
                group="语音能力",
            ))
            return

        from .asr import models as M

        # 用户「想」走哪条路（配置值），和实际生效的引擎可能不是一回事
        want = str(ctx.cfg("asr_engine", "mock") or "mock").strip().lower()

        def add(status: str, detail: str, fix: str) -> None:
            s.items.append(CheckItem(
                key="asr", label="语音转写", status=status, detail=detail,
                fix=fix, group="语音能力",
            ))

        # ---- 路线 A：选了本地模型，但没跑起来
        if want in ("local", "whisper", "faster_whisper"):
            if not M.library_available():
                add(
                    "warn",
                    "你选了「本地模型」，但当前这个程序没有内置语音识别库，"
                    "所以实际走的是 Mock（结果是假的）",
                    "改用带语音的「完整版」安装包；或者到「设置 → 语音识别」"
                    "切成「云 ASR」并填好接口地址。",
                )
            else:
                installed = M.installed_sizes()
                want_model = str(ctx.cfg("whisper_model", "small") or "small")
                if M.is_installed(want_model):
                    add(
                        "warn",
                        f"本地语音库与模型 {want_model} 都已就绪，"
                        f"但引擎配置没能生效（实际仍是 mock），保存一次设置即可",
                        "到「设置 → 语音识别」把引擎重新选一次「本地模型」并保存。",
                    )
                elif installed:
                    add(
                        "warn",
                        f"本地语音库已就绪，但你要用的模型 {want_model} 还没下载"
                        f"（已下载：{'、'.join(installed)}）",
                        f"到「设置 → 语音」下载 {want_model}，或把引擎的模型规格"
                        f"改成已下载的 {'、'.join(installed)}。",
                    )
                else:
                    add(
                        "warn",
                        "你选了「本地模型」，语音库也在，但模型权重还没下载 —— "
                        "没权重就完全跑不起来",
                        "到「设置 → 语音」选一个规格点「下载」。"
                        "首次建议 small：中文明显更准，CPU 也扛得住。"
                        "国内网络记得勾上「使用国内镜像」，否则容易超时。",
                    )
            return

        # ---- 路线 B：选了云 ASR，但没填地址
        if want in ("cloud", "api", "openai"):
            if not str(ctx.cfg("asr_base_url", "") or "").strip():
                add(
                    "warn",
                    "你选了「云 ASR」，但没填接口地址，所以实际走的是 Mock（结果是假的）",
                    "到「设置 → 语音识别」填上接口地址（任何 OpenAI 兼容的 "
                    "audio/transcriptions 接口都行）和密钥，保存即生效。"
                    "注意：这条路线会把通话音频传到第三方。",
                )
            else:
                add(
                    "warn",
                    "云 ASR 配置看起来是完整的，但引擎没能初始化，"
                    "请确认接口地址是否可访问",
                    "到「设置 → 语音识别」检查地址格式（要以 http:// 或 https:// 开头），"
                    "保存后再跑一次自检。",
                )
            return

        # ---- 路线 C：明确就是 mock（还没做选择）
        if not M.library_available():
            add(
                "warn",
                "语音还是 Mock（识别结果是假的），文字链路完全可用",
                "语音两条路线二选一：① 云 ASR —— 到「设置 → 语音识别」填接口地址和密钥，"
                "立刻能用，代价是音频会传到第三方；"
                "② 本地转写 —— 换用带语音的完整版安装包，音频不出本机。",
            )
        else:
            add(
                "warn",
                "语音还是 Mock（识别结果是假的），但本地语音库已经内置，随时可切",
                "到「设置 → 语音识别」把引擎切成「本地模型」，再下载一个模型规格即可。"
                "国内网络记得勾上「使用国内镜像」。",
            )
    except Exception as exc:
        s.items.append(CheckItem(
            key="asr",
            label="语音转写",
            status="fail",
            detail=f"初始化失败：{type(exc).__name__}: {exc}",
            fix="先用文字链路，语音可以稍后再排查。",
            group="语音能力",
        ))


def _check_audio(s: SelfCheck) -> None:
    """能不能真的录到声音。枚举设备比「装没装库」更能说明问题。"""
    try:
        from .asr.capture import list_audio_devices

        devices = list_audio_devices()
    except Exception as exc:
        s.items.append(CheckItem(
            key="audio",
            label="音频设备",
            status="warn",
            detail=f"枚举设备失败：{type(exc).__name__}: {exc}",
            fix="安装音频采集依赖：pip install soundcard",
            group="语音能力",
        ))
        return

    if not devices:
        s.items.append(CheckItem(
            key="audio",
            label="音频设备",
            status="warn",
            detail="没找到任何音频设备（可能未安装采集依赖，或这台机器没有声卡）",
            fix="安装音频采集依赖：pip install soundcard；装完重启程序再看这里。",
            group="语音能力",
        ))
        return

    mics = [d for d in devices if d.kind == "microphone"]
    loops = [d for d in devices if d.kind == "loopback"]
    if not loops:
        s.items.append(CheckItem(
            key="audio",
            label="音频设备",
            status="warn",
            detail=f"找到 {len(mics)} 个麦克风，但没有「系统回环」设备 —— 通话时听不到对方的声音",
            fix="回环设备只有 Windows 才有。确认系统里存在「立体声混音」之类设备，"
                "或在「通话」页手动指定要监听的设备。",
            group="语音能力",
        ))
    else:
        s.items.append(CheckItem(
            key="audio",
            label="音频设备",
            status="ok",
            detail=f"{len(mics)} 个麦克风 · {len(loops)} 个回环设备（回环用来听对方）",
            group="语音能力",
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
    _check_asr(s, ctx)
    _check_audio(s)
    return s


def runtime_paths() -> dict[str, str]:
    """给「打开文件夹」按钮用。"""
    return {
        "data": str(DATA_DIR),
        "logs": str(LOG_DIR),
        "app": str(APP_DIR),
        "resource": str(RESOURCE_DIR),
    }
