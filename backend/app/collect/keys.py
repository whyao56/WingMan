"""取密钥：能自动就自动，取不到就让人来，但**两条路都过同一套自证校验**。

## 为什么这个模块写得这么「不自信」

因为这是整条自动采集链上唯一真正不确定的一环，而它失败的方式很坏：
拿错密钥去解密，得到的是 4096 字节随机数据。如果代码用「解出来非空」当成功，
就会把垃圾写进用户的记忆里 —— 不报错、不可逆。所以：

- 密钥**只有**通过 `sqlcipher.check_raw_key`（能解出 SQLite 头 + HMAC 对得上）
  才算取到，其它一律算没取到；
- 取不到时返回的是「试过什么、为什么失败、下一步做什么」，不是空值。

## 实测结论（本机，2026-10，QQ 9.9.20.37051 / 微信 4.1.13.12）

自动取密钥这条路在**当前这两个版本上走不通**，而且是有依据的走不通：

| 手段 | 实测结果 |
|---|---|
| `x'<64位hex>'` / 引号包裹的十六进制字符串 | QQ 内存里找到 30 处、去重 8 个候选，**全部校验失败**；微信 **0 处** |
| 裸的 64 位十六进制串（附近有 sqlite 字样） | 无有效候选 |
| 32 字节二进制滑窗穷举 | 单候选约 4 ms（PBKDF2 不在我们这边，成本主要在 AES + 校验），QQ 可读内存 1.1 GB ⇒ **单线程约 1243 小时，8 核约 155 小时**，不可行 |
| 按 salt 命中点收缩到 88 MB 再穷举 | 已实现并有预算控制，实测未命中 |

所以本模块的定位是：
**自动尝试是有预算、有结论的；手动粘贴是一等公民，不是「失败后的安慰」。**
用户从任何渠道拿到密钥（社区工具、自己的脚本、以后版本的自动提取）粘进来，
后面的库定位、去头、解密、解析、增量入库全都是真跑的。

## 密钥缓存

验证通过的密钥会按「库的 salt」缓存。用 salt 当键而不是用账号或文件名：
salt 是每个库自己随机生成的，库被重建（换密钥）时 salt 必然变，
缓存自动失效 —— 不会出现「拿着旧密钥一直解密失败，还找不到原因」。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import winapi
from .sqlcipher import (
    CRYPTO_AVAILABLE, CRYPTO_REASON, Profile, check_raw_key, probe_profile,
)

# hex 字符串候选：先找带包裹的（信号最强），再找裸的
_HEX_WRAPPED = (
    re.compile(rb"x'([0-9a-fA-F]{64})'"),
    re.compile(rb"X'([0-9a-fA-F]{64})'"),
    re.compile(rb'"([0-9a-fA-F]{64})"'),
    re.compile(rb"'([0-9a-fA-F]{64})'"),
)
_HEX_BARE = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")

DEFAULT_MEMORY_BUDGET_S = 180.0


@dataclass
class KeyAttempt:
    """一次取密钥的结果。`detail` 是给人看的一句话。"""

    ok: bool = False
    key_hex: str = ""
    profile: str = ""
    method: str = ""            # memory-hex | memory-scan | pasted | cache
    detail: str = ""
    tried: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    candidates: int = 0
    # 是否因为「到点收工」而提前结束。调用方据此决定要不要为同一个客户端的
    # 下一个库再搜一遍内存 —— 内存内容几秒内不会变，重复搜只是再等一遍。
    budget_hit: bool = False

    @property
    def key_bytes(self) -> bytes:
        return bytes.fromhex(self.key_hex) if self.key_hex else b""


# ---------------------------------------------------------------- 密钥规范化


def normalise_key(text: str) -> bytes | None:
    """把用户粘贴的内容变成 32 字节密钥。

    用户会从各种地方复制，形态五花八门，所以全部容忍：
        `x'1a2b..'` / `"1a2b.."` / `1a2b..` / 带空格的 / 大写 /
        甚至整段 `PRAGMA key = "x'..'";`
    只认「去掉非十六进制字符后正好 64 位」这一种，避免把别的东西当密钥。
    """
    if not text:
        return None
    s = str(text).strip()
    # 先看有没有 x'..' 形式，优先取它（一段文本里可能同时有别的数字）
    m = re.search(r"[xX]'([0-9a-fA-F]{64})'", s)
    if m:
        return bytes.fromhex(m.group(1))
    hexonly = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(hexonly) == 64:
        return bytes.fromhex(hexonly)
    return None


def looks_like_key(text: str) -> bool:
    return normalise_key(text) is not None


# ---------------------------------------------------------------- 校验


def read_page1(db: Path, profile: Profile) -> bytes:
    with Path(db).open("rb") as fh:
        fh.seek(profile.file_header)
        return fh.read(profile.page_size)


def verify_key(raw_key: bytes, db: Path, profiles: tuple[Profile, ...]) -> Profile | None:
    """密钥对不对？对就返回匹配的参数，不对返回 None。"""
    if len(raw_key) != 32:
        return None
    for prof in profiles:
        try:
            page1 = read_page1(db, prof)
        except OSError:
            continue
        if len(page1) < prof.page_size:
            continue
        if check_raw_key(raw_key, page1, prof):
            return prof
    return None


def salt_key_for(db: Path, profile: Profile) -> str:
    """缓存的键：库的 salt 十六进制。库换密钥 → salt 变 → 缓存自动失效。"""
    try:
        with Path(db).open("rb") as fh:
            fh.seek(profile.file_header)
            return fh.read(16).hex()
    except OSError:
        return ""


# ---------------------------------------------------------------- 内存扫描


def _procs_for(exe_names: tuple[str, ...]) -> list[int]:
    if not winapi.IS_WINDOWS:
        return []
    try:
        return [p.pid for p in winapi.find_processes(exe_names)]
    except winapi.Win32Error:
        return []


# ---------------------------------------------------------------- 时间预算
#
# 这个类是**用一次实测换来的**：扫描发布版在真机上跑，一台开着 QQ 的机器有
# 7 个 QQ.exe 进程、2.9 GB 可读内存，光「读一遍 + 正则扫十六进制」就要 43 秒
# （逐进程实测 12.5 / 9.6 / 2.0 / 9.1 / 5.8 / 1.9 / 2.5 秒）。
# 而界面承诺「预演 12 秒、采集 60 秒」。
#
# 之前预算写在参数里、只在外层循环查了一次，于是：
#   · 第 1 级（hex 扫描）**一次都没查**，43 秒直接超支；
#   · 第 2 级的内层是 `for off in range(len(blob) - 32)`，一个 8.4 MB 的区域
#     就是 840 万个候选 × 2 套参数 × 9.4 µs ≈ 157 秒，同样查不到；
# 结果「12 秒的预演」实际要跑三分钟以上，界面上就是转圈到用户放弃。
#
# 教训记在这里：**预算必须贴在每一层循环上**，漏一层，预算就等于没有。


class Budget:
    """一次操作的剩余时间。`expired()` 每一层循环都要查。"""

    def __init__(self, seconds: float) -> None:
        self.t0 = time.perf_counter()
        self.seconds = max(0.0, float(seconds))

    @property
    def used(self) -> float:
        return time.perf_counter() - self.t0

    @property
    def left(self) -> float:
        return self.seconds - self.used

    def expired(self) -> bool:
        return self.left <= 0

    def describe(self) -> str:
        return f"预算 {self.seconds:.0f}s，实际用了 {self.used:.0f}s"


def scan_memory_for_key(
    db: Path,
    profiles: tuple[Profile, ...],
    *,
    exe_names: tuple[str, ...],
    budget_s: float = DEFAULT_MEMORY_BUDGET_S,
    allow_bruteforce: bool = True,
) -> KeyAttempt:
    """在客户端进程内存里找密钥。有预算、有结论，不假装一定能成。

    两级：
    1. **十六进制字符串**（便宜）：`x'..'` / 引号包裹 / 裸 64 位串。
       一个进程几百 MB 用正则扫一遍是秒级，值得每个进程都扫。
    2. **32 字节滑窗穷举**（贵）：只在「含 salt 的内存区域」里做，且受预算约束。
       为什么只在这些区域：全内存穷举按实测是 1000+ 小时量级，不可能。

    **返回时间被预算兜住**：`budget_s` 是上限，不是目标。超时不是失败，
    是「到点收工 + 如实说扫到哪了」—— 说「试过了、没找到」比让界面转三分钟
    更有用。
    """
    budget = Budget(budget_s)
    res = KeyAttempt(method="memory")
    pids = _procs_for(exe_names)
    if not pids:
        res.detail = f"{'/'.join(exe_names)} 没有在运行，取不到内存里的密钥。"
        res.tried.append("进程未运行")
        return res

    profiles_by_name = {p.name: p for p in profiles}
    main_profile = profiles[0]
    try:
        page1 = read_page1(db, main_profile)
    except OSError as exc:
        res.detail = f"读不到 {db.name}：{exc}"
        return res
    if len(page1) < main_profile.page_size:
        res.detail = f"{db.name} 不足一页，可能不是 SQLCipher 库。"
        return res
    salt = page1[:16]

    # ---------------- 第 1 级：hex 字符串
    seen: dict[str, str] = {}
    scanned = 0
    skipped_pids: list[int] = []
    truncated = False           # 这次扫描是「没扫完就收工」的吗
    for pid in pids:
        if budget.expired():
            skipped_pids.append(pid)
            continue
        try:
            reader = winapi.MemoryReader(pid)
        except winapi.Win32Error as exc:
            res.tried.append(f"PID {pid} 打不开：{exc}")
            continue
        try:
            def cb(_base: int, blob: bytes, _pid: int = pid) -> bool:
                """一个区域扫完就回头看一眼预算 —— 别攒到进程级别才查。"""
                for pat in _HEX_WRAPPED:
                    for m in pat.finditer(blob):
                        seen.setdefault(m.group(1).decode().lower(), f"PID{_pid}")
                for m in _HEX_BARE.finditer(blob):
                    h = m.group(1).decode().lower()
                    if h in seen:
                        continue
                    ctx = blob[max(0, m.start() - 160):m.start()]
                    if b"sqlite" in ctx.lower() or b"PRAGMA" in ctx:
                        seen.setdefault(h, f"PID{_pid}(近 sqlite)")
                return not budget.expired()

            _, n = reader.iterate(cb, should_stop=budget.expired)
            scanned += n
            # 走到这儿预算已经没了，说明后面的区域（或进程）没读到 ——
            # 这就是「扫描量是部分的」，必须让用户知道，而不是当成扫完了没有。
            if budget.expired():
                truncated = True
        finally:
            reader.close()

    res.candidates = len(seen)
    res.tried.append(
        f"扫了 {scanned / 1e6:.0f} MB 进程内存，找到 {len(seen)} 个十六进制候选")
    for hexkey, where in seen.items():
        prof = verify_key(bytes.fromhex(hexkey), db, profiles)
        if prof is not None:
            res.ok = True
            res.key_hex = hexkey
            res.profile = prof.name
            res.method = "memory-hex"
            res.detail = f"在 {where} 的内存里找到并校验通过的密钥。"
            res.elapsed_s = budget.used
            return res

    if skipped_pids or truncated:
        res.budget_hit = True
        res.tried.append(
            f"这次是到点收工：{len(skipped_pids)} 个进程没轮到，"
            "扫描量是部分的 —— 不等于「都扫过了、没有」")

    if not allow_bruteforce:
        if truncated or skipped_pids:
            # 结论一样是「没找到」，但依据不一样：一个扫完了，一个没扫完。
            # 混成一句话，用户会把它当成「这台机器上一定没有」。
            res.detail = (f"扫过 {scanned / 1e6:.0f} MB 进程内存，没有可用的密钥。"
                          f"注意这次是到点收工（{budget.describe()}），没扫完。")
        else:
            res.detail = f"扫过 {len(pids)} 个进程的内存，没有可用的密钥。"
        res.elapsed_s = budget.used
        return res

    # ---------------- 第 2 级：锚点区域 + 32 字节滑窗（有预算）
    if budget.left <= 5:
        res.budget_hit = True
        res.detail = (f"一级搜索（十六进制串）用完了 {budget_s:.0f}s 预算，"
                      "没有余额做穷举。")
        res.elapsed_s = budget.used
        return res

    tried = 0
    regions_bytes = 0
    for pid in pids:
        if budget.expired():
            truncated = True
            break
        try:
            reader = winapi.MemoryReader(pid)
        except winapi.Win32Error:
            continue
        try:
            # 先找含 salt / 库文件名的区域，缩小范围。
            # 这一步本身也要时间（每个 needle 都是把内存读一遍），所以同样带预算。
            anchors = set()
            for needle in (salt, db.name.encode("utf-8", "ignore")):
                if not needle or budget.left <= 2:
                    break
                for va in reader.find(needle, max_hits=8,
                                      should_stop=budget.expired):
                    anchors.add(va & ~0xFFFF)      # 对齐到 64 KB 边界
            if not anchors:
                continue
            for base, size in reader.regions():
                if budget.expired():
                    truncated = True
                    break
                start = max(0, base)
                if not any(start <= a < start + size for a in anchors):
                    continue
                blob = reader.read(base, size)
                if not blob:
                    continue
                regions_bytes += len(blob)
                for prof in profiles[:2]:      # 只上最可能的 2 套参数
                    p1 = page1
                    for off in range(len(blob) - 32):
                        # 内层也要查预算：一个区域最多 840 万个候选，
                        # 全跑完要两分半，而这里本来只承诺几秒。
                        # 每 4096 个查一次 —— 单次开销约 9.4 µs，
                        # 4096 个约 38 ms，核算频率与精度都够。
                        if (off & 0xFFF) == 0 and budget.expired():
                            truncated = True
                            break
                        tried += 1
                        if check_raw_key(blob[off:off + 32], p1, prof):
                            key = blob[off:off + 32].hex()
                            res.ok = True
                            res.key_hex = key
                            res.profile = prof.name
                            res.method = "memory-scan"
                            res.detail = f"在 PID {pid} 的内存里穷举命中。"
                            res.elapsed_s = budget.used
                            res.candidates += tried
                            return res
                    if truncated:
                        break
                if truncated:
                    break
        finally:
            reader.close()
        if truncated:
            break

    res.candidates += tried
    res.elapsed_s = budget.used
    res.budget_hit = res.budget_hit or truncated
    res.tried.append(
        f"穷举了锚点附近 {regions_bytes / 1e6:.1f} MB、{tried:,} 个候选"
        + ("（到点收工，没穷尽）" if truncated else ""))
    if truncated:
        res.detail = (
            f"自动取密钥没有成功：{budget.describe()} 后按预算收工，"
            f"已经扫了 {scanned / 1e6:.0f} MB 内存、试了 {tried:,} 个候选，"
            "剩下的没试完。这个结论是「预算内没找到」，不是「这台机器上一定没有」。"
            "可以先用「半自动采集」，或者手动粘贴密钥。")
    else:
        res.detail = (
            f"自动取密钥没有成功（用了 {res.elapsed_s:.0f}s）。"
            "这台机器上的客户端版本很可能把密钥换成了别的存放方式。"
            "可以先用「半自动采集」，或者手动粘贴密钥。")
    return res


# ---------------------------------------------------------------- 对外入口


def obtain_key(
    db: Path,
    profiles: tuple[Profile, ...],
    *,
    exe_names: tuple[str, ...],
    pasted: str = "",
    cached: str = "",
    allow_memory: bool = True,
    budget_s: float = DEFAULT_MEMORY_BUDGET_S,
    memory_skip_note: str = "",
) -> KeyAttempt:
    """按「缓存 → 用户粘贴 → 自动搜内存」的顺序取密钥。

    顺序是有讲究的：缓存和粘贴都是**已经验证过**的，零成本零风险；
    自动搜内存要读别的进程、可能触发安全软件，所以放最后。

    `memory_skip_note` 是「这次不搜内存」时给用户看的那句话。有它才说得清
    「是你关了自动取密钥」还是「同一个客户端刚搜过一遍、没必要再搜一遍」——
    两种情况对用户的下一步是同一个建议，但原因不一样，不能共用一句含糊的话。
    """
    if not CRYPTO_AVAILABLE:
        # 缺依赖时要说清楚「装什么」，而不是走到最后报一个
        # 「密钥校验失败」——那是完全不同的两件事，会让人白查半天。
        return KeyAttempt(ok=False, method="unavailable", detail=CRYPTO_REASON)
    if pasted:
        raw = normalise_key(pasted)
        if raw is None:
            return KeyAttempt(
                ok=False, method="pasted",
                detail="粘贴的内容不是 32 字节密钥。"
                       "它应该是 64 个十六进制字符，形如 "
                       "`x'1a2b3c…'` 或直接 64 位十六进制。")
        prof = verify_key(raw, db, profiles)
        if prof is None:
            return KeyAttempt(
                ok=False, method="pasted",
                detail="密钥能解析，但通不过校验（解不出 SQLite 头）。"
                       "可能是：① 密钥对应的是另一个库；② 参数版本不同；"
                       "③ 复制时少了几位。")
        return KeyAttempt(ok=True, key_hex=raw.hex(), profile=prof.name,
                          method="pasted",
                          detail=f"粘贴的密钥校验通过（参数 {prof.name}）。")

    if cached:
        raw = normalise_key(cached)
        if raw is not None:
            prof = verify_key(raw, db, profiles)
            if prof is not None:
                return KeyAttempt(ok=True, key_hex=raw.hex(), profile=prof.name,
                                  method="cache", detail="用上次记住的密钥。")

    if not allow_memory:
        return KeyAttempt(ok=False, method="memory-skipped",
                          detail=memory_skip_note or "已关闭自动取密钥。")
    return scan_memory_for_key(db, profiles, exe_names=exe_names, budget_s=budget_s)
