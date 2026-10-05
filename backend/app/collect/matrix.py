"""客户端支持矩阵：哪些版本的 QQ / 微信能连、怎么连、连不上该怎么办。

为什么要把「版本」做成软件里的数据，而不是写在 README 里：

读本地聊天库这件事，**唯一真正的变量就是客户端版本**。同一个 QQ，
9.9.x 的库前面有 1024 字节自定义头、参数是一套，换成别的版本就可能全变。
如果这个知识只存在于文档里，用户在界面上只会看到一句「采集失败」——
他不知道是版本不对、还是没登录、还是密钥没取到。

所以这里的目标是：**让软件自己说得清「你的版本我支不支持、不支持该怎么办」**。
所有结论都来自实测，矩阵里明确区分「我实测过的版本」和「理论上也能用」。

注意：`auto_readable` 表示「这台机器上的自动读取链路是否真的走得通」，
它和「支持」不是一回事 —— 微信 4.x 我支持它的库格式（能解密），
但如果用户本机压根没建消息库（实测过：只装了 4.1.13.12、消息库不存在），
自动读取就无米下锅。这两件事必须分开说，否则用户会以为是自己操作错了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 版本


def version_tuple(text: str) -> tuple[int, ...]:
    """把 '9.9.20.37051' 变成 (9, 9, 20, 37051)。认不出的返回空元组。"""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) if nums else ()


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def version_in_range(version: str, low: str = "", high: str = "") -> bool:
    """判断版本是否落在 [low, high] 内。空的一端表示不限。

    逐段比较而不是比字符串：`'9.9.9' > '9.9.20'` 在字符串下是真的，
    在版本语义下是假的。这类错会让「受支持」判反，而且很难被发现。
    """
    got = version_tuple(version)
    if not got:
        return False
    if low:
        a, b = _pad(got, version_tuple(low))
        if a < b:
            return False
    if high:
        a, b = _pad(got, version_tuple(high))
        if a > b:
            return False
    return True


# ---------------------------------------------------------------- 规格


@dataclass(frozen=True)
class DataLayout:
    """一个客户端在磁盘上的样子。路径里 `{...}` 会被探测时替换。"""

    root_candidates: tuple[str, ...]
    db_relative: tuple[str, ...]        # 消息库相对数据目录的路径（可能多处）
    extra_dbs: tuple[str, ...] = ()     # 其它有用的库（联系人、群资料等）
    header_bytes: int = 0               # SQLCipher 数据前的自定义头长度
    account_hint: str = ""
    note: str = ""


@dataclass(frozen=True)
class ClientSpec:
    key: str
    display_name: str
    exe_names: tuple[str, ...]
    low: str
    high: str
    tested: tuple[str, ...]                    # 实测过的版本
    layout: DataLayout
    cipher_profile: str                        # 对应 sqlcipher.Profile 的名字
    can_auto_read: bool
    why_not: str = ""                          # 不能自动读的原因（要具体）
    guide: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


# 微信 4.x（新架构，进程名 Weixin.exe）。实测版本 4.1.13.12。
WECHAT4 = ClientSpec(
    key="wechat",
    display_name="微信 4.x",
    exe_names=("Weixin.exe",),
    low="4.0.0",
    high="",
    tested=("4.1.13.12",),
    layout=DataLayout(
        root_candidates=(
            r"{docs}\xwechat_files",
            r"{home}\Documents\xwechat_files",
            r"{userprofile}\Documents\xwechat_files",
        ),
        db_relative=(r"{account}\db_storage\message",),
        extra_dbs=(
            r"{account}\db_storage\contact\contact.db",
            r"{account}\db_storage\session\session.db",
            r"all_users\login\{account}\key_info.db",
        ),
        header_bytes=0,
        account_hint="wxid_ 开头，目录名形如 wxid_xxxxxx_ab12",
        note="消息库按会话分片存在 db_storage\\message\\ 下；"
             "contact.db 是联系人，session.db 是会话列表。",
    ),
    cipher_profile="wechat4/sha512-80",
    can_auto_read=True,
    guide=(
        "保持微信 4.x 处于登录状态并停留在主界面（不要退出到登录页）。",
        "如果消息列表是空的，先在微信里逐一点开要采集的会话 —— "
        "消息库是按需建立的，没打开过的会话可能还没有本地库。",
    ),
    risks=(
        "微信 4.x 的数据库密钥只存在于运行中的进程内存里，需要读取进程内存才能取得；"
        "部分安全软件会把这种行为判定为可疑。",
    ),
)

# 微信 3.x（老架构，进程名 WeChat.exe）。保留是为了给「版本不对」的用户明确指引。
WECHAT3 = ClientSpec(
    key="wechat3",
    display_name="微信 3.x（旧版）",
    exe_names=("WeChat.exe",),
    low="3.0.0",
    high="3.9.99",
    tested=(),
    layout=DataLayout(
        root_candidates=(r"{docs}\WeChat Files", r"{userprofile}\Documents\WeChat Files"),
        db_relative=(r"{account}\Msg",),
        extra_dbs=(r"{account}\Msg\MicroMsg.db",),
        header_bytes=0,
        account_hint="wxid_ 开头",
        note="老版本把消息放在 Msg\\MSG0.db ~ MSG5.db，按时间分片。",
    ),
    cipher_profile="wechat3/sha1-48",
    can_auto_read=True,
    guide=(
        "旧版的库格式和新版不同，本程序对新版支持更完整，建议优先使用微信 4.x。",
    ),
)

# QQ NT（进程名 QQ.exe，安装目录 QQNT）。实测版本 9.9.20.37051。
QQ_NT = ClientSpec(
    key="qq",
    display_name="QQ NT（9.9.x）",
    exe_names=("QQ.exe",),
    low="9.9.0",
    high="9.9.99",
    tested=("9.9.20.37051",),
    layout=DataLayout(
        root_candidates=(
            r"{docs}\Tencent Files",
            r"{userprofile}\Documents\Tencent Files",
            r"{home}\Documents\Tencent Files",
        ),
        db_relative=(r"{account}\nt_qq\nt_db",),
        extra_dbs=(
            r"{account}\nt_qq\nt_db\profile_info.db",
            r"{account}\nt_qq\nt_db\group_info.db",
            r"{account}\nt_qq\nt_db\recent_contact.db",
            r"nt_qq\global\nt_db\login.db",
        ),
        header_bytes=1024,
        account_hint="纯数字 QQ 号，例如 2213914174",
        note="nt_msg.db 是消息主库（还有 buddy_msg_fts.db / group_msg_fts.db "
             "两个全文索引库）；文件开头有 1024 字节自定义头，"
             "必须剥掉才能被 SQLite 识别。",
    ),
    cipher_profile="qq-nt/sha512-80",
    can_auto_read=True,
    guide=(
        "保持 QQ 处于登录状态并让主窗口开着（最小化到托盘也可以，但不要退出登录）。",
        "采集前翻一下要采集的会话，让最近的记录落到本地库。",
    ),
    risks=(
        "QQ NT 的库同样靠进程内存里的密钥解密；"
        "官方客户端更新后加密参数可能变化，届时需要等待适配。",
    ),
)

SUPPORT_MATRIX: dict[str, ClientSpec] = {
    WECHAT4.key: WECHAT4,
    WECHAT3.key: WECHAT3,
    QQ_NT.key: QQ_NT,
}

# 进程名 → 规格。用来从「跑着的进程」反查是哪个客户端。
EXE_TO_SPEC: dict[str, ClientSpec] = {
    exe.lower(): spec for spec in SUPPORT_MATRIX.values() for exe in spec.exe_names
}


# ---------------------------------------------------------------- 判定


@dataclass
class Support:
    """一个客户端实例的支持结论。字段都是给人看的结论，不是内部状态。"""

    client: str
    display_name: str
    installed: bool = False
    running: bool = False
    version: str = ""
    verdict: str = "unknown"        # supported | untested | unsupported | not_installed
    headline: str = ""
    actions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.verdict == "supported"


def judge(spec: ClientSpec, version: str, *, installed: bool,
          running: bool) -> Support:
    """给出「这台机器上的这个客户端，能不能自动采集」的结论与下一步动作。"""
    sup = Support(client=spec.key, display_name=spec.display_name,
                  installed=installed, running=running, version=version)
    sup.risks = list(spec.risks)

    if not installed:
        sup.verdict = "not_installed"
        sup.headline = f"没有找到 {spec.display_name}"
        sup.actions = [f"装好 {spec.display_name} 并登录后再回来。"]
        return sup

    if version and not version_in_range(version, spec.low, spec.high):
        sup.verdict = "unsupported"
        span = f"{spec.low} ~ {spec.high}" if spec.high else f"{spec.low} 及以上"
        sup.headline = f"{spec.display_name} {version} 不在已验证范围内（{span}）"
        sup.actions = [
            f"我实测过的版本是 {'、'.join(spec.tested) or '（暂无）'}，"
            f"你的 {version} 参数可能不同。",
            "可以照常试一次自动采集 —— 如果不成功，程序会明确告诉你是哪一步失败。",
            "仍然不行就先用「半自动采集」：点哪条抓哪条，不依赖版本和加密参数。",
        ]
        return sup

    if not running:
        sup.verdict = "supported"
        sup.headline = f"{spec.display_name} {version} 已安装，但当前没有在运行"
        sup.actions = [
            "先把客户端打开并登录。",
            "密钥只存在于运行中的进程里，关着的时候取不到。",
        ]
        return sup

    if version and version not in spec.tested:
        sup.verdict = "untested"
        sup.headline = f"{spec.display_name} {version} 在支持范围内，但我没实测过这个具体版本"
        sup.actions = [
            f"我实测过的是 {'、'.join(spec.tested)}。",
            "直接试一次自动采集即可 —— 参数不对时会明确报「密钥或参数不匹配」，不会写进错数据。",
        ]
        return sup

    sup.verdict = "supported"
    sup.headline = f"{spec.display_name} {version}，可以自动采集"
    sup.actions = ["直接开始采集；取密钥失败时会自动退到「手动粘贴密钥」。"]
    return sup


def guide_for(client: str) -> tuple[str, ...]:
    spec = SUPPORT_MATRIX.get(client)
    return spec.guide if spec else ()


def supported_versions_table() -> list[dict[str, str]]:
    """给前端「配置指引」用的一张表。"""
    rows = []
    for spec in SUPPORT_MATRIX.values():
        rows.append({
            "client": spec.key,
            "name": spec.display_name,
            "range": f"{spec.low} ~ {spec.high}" if spec.high else f"≥ {spec.low}",
            "tested": "、".join(spec.tested) or "（未实测）",
            "can_auto_read": "是" if spec.can_auto_read else "否",
            "layout": spec.layout.note,
            "account_hint": spec.layout.account_hint,
            "guide": list(spec.guide),
        })
    return rows
