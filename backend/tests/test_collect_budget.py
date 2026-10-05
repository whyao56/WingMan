"""回归：取密钥的时间预算是真的。

「自动找密钥」这一步最贵的是把客户端所有进程的内存读一遍并按区域扫。它的失败
方式很隐蔽 —— **预算写在签名和注释里，但没有贴在每一层循环上**：

    第 1 级（读内存 + 正则扫十六进制）一次都没查预算；
    第 2 级的内层是 `for off in range(len(blob) - 32)`，同样查不到。

实测（发布版在真机上跑，本机开着 QQ，7 个进程、2.9 GB 可读内存）：

    第 1 级逐进程 12.5 / 9.6 / 2.0 / 9.1 / 5.8 / 1.9 / 2.5 秒 → 合计 43 秒
    第 2 级单候选 9.4 µs，一个 8.4 MB 的锚点区域 = 840 万候选 × 2 套参数 ≈ 157 秒

而界面承诺的是「预演 12 秒、采集 60 秒」。结果是按钮一直转、用户不知道
该等还是该放弃 —— 真正的问题不是慢，是**承诺了一个时间却做不到**。

这个文件钉住的都是「不查就失效」的位置，每一条都对应上面某一个循环。

跑法：

    cd backend
    python -m pytest tests/test_collect_budget.py -q
    python tests/test_collect_budget.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


# ---------------------------------------------------------------- 假内存

class _FakeReader:
    """一个可控的「进程内存」。

    真机上的内存不可控也不可复现，所以这里把「有多少区域、每块多大、读一块多慢」
    全部变成参量。要验的是**循环有没有查预算**，不是内存读得快不快。
    """

    #: 每读一块停多久（模拟真实机器上 2.9 GB 读 43 秒）
    read_s = 0.0
    #: 区域列表 [(基址, 大小)]
    regions_list: list[tuple[int, int]] = []
    #: `read()` 返回的内容；默认全 0（正则扫得最快）
    blob_fill = b"\x00"
    #: `find()` 的返回值（第 2 级靠它挑区域）
    anchors: list[int] = []

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.closed = False

    def close(self) -> None:
        self.closed = True

    # 与 winapi.MemoryReader 同签名
    def regions(self, *, max_region: int = 0x4000000, private_only: bool = False):
        for base, size in self.regions_list:
            yield base, size

    def read(self, addr: int, size: int) -> bytes:
        if self.read_s:
            time.sleep(self.read_s)
        return self.blob_fill * size

    def iterate(self, callback, *, max_region: int = 0x4000000,
                private_only: bool = False, should_stop=None):
        count = total = 0
        for base, size in self.regions_list:
            if should_stop is not None and should_stop():
                break
            blob = self.read(base, size)
            if not blob:
                continue
            count += 1
            total += len(blob)
            if callback(base, blob) is False:
                break
        return count, total

    def find(self, needle: bytes, *, max_hits: int = 40,
             max_region: int = 0x4000000, should_stop=None) -> list[int]:
        return list(self.anchors)


def _patch_memory(reader_cls) -> tuple:
    """把 winapi 里的进程枚举与内存读取换成假实现，返回还原用的东西。

    `IS_WINDOWS` 也要一起改成 True：CI 跑在 Ubuntu 上，而采集层的探测在非
    Windows 上会直接返回「没有进程」—— 那样这些用例会静默变成空跑。
    """
    from app.collect import winapi

    saved = (winapi.find_processes, winapi.MemoryReader, winapi.IS_WINDOWS)
    winapi.find_processes = lambda names: [types.SimpleNamespace(pid=4242,
                                                                name="QQ.exe")]
    winapi.MemoryReader = reader_cls
    winapi.IS_WINDOWS = True
    return saved


def _restore_memory(saved: tuple) -> None:
    from app.collect import winapi

    (winapi.find_processes, winapi.MemoryReader,
     winapi.IS_WINDOWS) = saved


def _fake_db(tmp: Path) -> Path:
    """一个够长的假库：`read_page1` 只需要能读出 4096 字节。"""
    db = tmp / "nt_msg.db"
    db.write_bytes(b"\x00" * 8192)
    return db


# ---------------------------------------------------------------- 用例


def test_budget_expires_is_about_remaining_time() -> None:
    """`Budget` 的语义：到点就是到点，剩余时间不能是负数。"""
    from app.collect.keys import Budget

    b = Budget(0.2)
    assert not b.expired(), "刚创建就说过期了，预算等于白给"
    time.sleep(0.25)
    assert b.expired(), "过了 0.25 秒、预算 0.2 秒，还没说过期"
    assert b.left <= 0, b.left


def test_hex_scan_stops_when_the_budget_is_gone() -> None:
    """第 1 级（逐进程读内存）必须每个区域回头看一眼预算。

    没有这一条，43 秒的读取会把 12 秒的预演直接拖成三分钟。
    """
    from app.collect import keys as keys_mod
    from app.collect.sqlcipher import GENERIC_PROFILES

    class Slow(_FakeReader):
        read_s = 0.03
        regions_list = [(0x1000 * i, 256 * 1024) for i in range(1, 61)]

    tmp = Path(tempfile.mkdtemp(prefix="wingman_budget1_"))
    saved = _patch_memory(Slow)
    try:
        db = _fake_db(tmp)
        t0 = time.perf_counter()
        res = keys_mod.scan_memory_for_key(
            db, GENERIC_PROFILES, exe_names=("QQ.exe",), budget_s=0.4,
            allow_bruteforce=False)
        elapsed = time.perf_counter() - t0
    finally:
        _restore_memory(saved)
        shutil.rmtree(tmp, ignore_errors=True)

    # 60 个区域 × 30 ms ≈ 1.8 秒；带预算检查应该在 0.4 秒出头收工
    assert elapsed < 1.2, (
        f"预算 0.4s 却跑了 {elapsed:.2f}s —— 第 1 级没查预算（60 块全读完了）")
    assert res.budget_hit, "到点收工了却没有标记，调用方就没法决定要不要再搜一遍"
    assert not res.ok
    joined = " ".join(res.tried)
    assert "到点收工" in joined, f"没告诉用户「没扫完」，只说扫了多少：{joined}"
    assert "到点收工" in res.detail, (
        f"结论一样是「没找到」，但依据不一样（没扫完 vs 扫完了）：{res.detail}")


def test_bruteforce_inner_loop_checks_the_budget() -> None:
    """第 2 级的**内层**也要查预算 —— 一个区域就是几百个候选。

    这是实测里最贵的一处：8.4 MB 的锚点区域 × 2 套参数 ≈ 157 秒。
    只在区域之间查（外层）根本拦不住它。
    """
    from app.collect import keys as keys_mod
    from app.collect.sqlcipher import GENERIC_PROFILES

    BIG = 2 * 1024 * 1024

    class Anchored(_FakeReader):
        read_s = 0.0
        regions_list = [(0x1000, 256 * 1024), (0x200000, BIG)]
        anchors = [0x200000]

    tmp = Path(tempfile.mkdtemp(prefix="wingman_budget2_"))
    saved = _patch_memory(Anchored)
    try:
        db = _fake_db(tmp)
        t0 = time.perf_counter()
        res = keys_mod.scan_memory_for_key(
            db, GENERIC_PROFILES, exe_names=("QQ.exe",), budget_s=0.8)
        elapsed = time.perf_counter() - t0
    finally:
        _restore_memory(saved)
        shutil.rmtree(tmp, ignore_errors=True)

    assert elapsed < 3.0, (
        f"预算 0.8s 却跑了 {elapsed:.1f}s —— 第 2 级内层没查预算"
        f"（2 MB × 2 套参数 = 400 万候选）")
    assert res.budget_hit, "到点收工了却没有标记"
    assert res.candidates < BIG, (
        f"试了 {res.candidates} 个候选，说明把整个区域跑完了 —— "
        "预期是到点就停")


def test_bruteforce_does_not_start_when_level1_ate_the_budget() -> None:
    """第 1 级把预算用光后，不许再进第 2 级。"""
    from app.collect import keys as keys_mod
    from app.collect.sqlcipher import GENERIC_PROFILES

    class Slow(_FakeReader):
        read_s = 0.05
        regions_list = [(0x1000 * i, 128 * 1024) for i in range(1, 41)]
        anchors = [0x1000]

    tmp = Path(tempfile.mkdtemp(prefix="wingman_budget3_"))
    saved = _patch_memory(Slow)
    try:
        db = _fake_db(tmp)
        res = keys_mod.scan_memory_for_key(
            db, GENERIC_PROFILES, exe_names=("QQ.exe",), budget_s=0.3)
    finally:
        _restore_memory(saved)
        shutil.rmtree(tmp, ignore_errors=True)

    assert res.budget_hit
    assert res.candidates == 0, "预算已经被第 1 级吃光，不该再试任何候选"
    assert "没有余额做穷举" in res.detail, res.detail


def test_skipping_the_scan_says_why() -> None:
    """「这次不搜内存」必须给出原因，不能和「你把它关了」共用一句话。"""
    from app.collect.keys import obtain_key
    from app.collect.sqlcipher import GENERIC_PROFILES

    tmp = Path(tempfile.mkdtemp(prefix="wingman_budget4_"))
    try:
        db = _fake_db(tmp)
        note = "同一个客户端的密钥刚刚已经按预算搜过一遍、没找到"
        res = obtain_key(db, GENERIC_PROFILES, exe_names=("QQ.exe",),
                         allow_memory=False, memory_skip_note=note)
        assert res.method == "memory-skipped", res.method
        assert note in res.detail, res.detail
        assert not res.ok

        # 没给原因时才退回默认那句
        res2 = obtain_key(db, GENERIC_PROFILES, exe_names=("QQ.exe",),
                          allow_memory=False)
        assert res2.method == "memory-skipped"
        assert res2.detail == "已关闭自动取密钥。", res2.detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fulltext_index_dbs_are_not_collected() -> None:
    """全文索引库不采：里面没有消息行，但每个都要白跑一遍取密钥。

    实测本机 QQ 会被名字过滤挑出 7 个库，其中 4 个是全文索引 ——
    不排除掉，用户等的时间就是原来的 7 倍。
    """
    from app.collect.pipeline import _dbs_to_collect, _is_message_db

    assert _is_message_db(Path("nt_msg.db"))
    assert _is_message_db(Path("message_0.db"))
    assert _is_message_db(Path("biz_message_0.db"))
    assert not _is_message_db(Path("msg_fts.db")), "全文索引不是消息库"
    assert not _is_message_db(Path("buddy_msg_fts.db"))
    assert not _is_message_db(Path("media_0.db")), "媒体库没有正文"
    assert not _is_message_db(Path("emoji.db"))

    from app.collect.detect import AccountData

    det = types.SimpleNamespace(accounts=[AccountData(
        account="2213914174", root="D:/文档/Tencent Files/2213914174",
        message_dbs=["nt_msg.db", "guild_msg.db", "msg_fts.db",
                     "buddy_msg_fts.db", "media_0.db"])])
    targets, skipped = _dbs_to_collect(det, "")
    assert [p.name for _, p in targets] == ["nt_msg.db", "guild_msg.db"], targets
    assert sorted(skipped) == ["buddy_msg_fts.db", "msg_fts.db"], skipped


def test_one_memory_scan_per_client_not_per_db() -> None:
    """一个客户端只搜一遍内存：后面的库不再重复等一份同样的预算。

    内存内容几秒内不会变。第一个库已经按预算搜过、没找到，第二个库再搜一遍
    只是让用户再等一次 —— 而结论不会变。
    """
    from app.collect import pipeline as pipe
    from app.collect.detect import AccountData
    from app.collect.keys import KeyAttempt

    calls: list[dict] = []

    def fake_obtain(db, profiles, **kw):
        calls.append({"db": Path(db).name, **kw})
        # 第一个库：搜了一遍没找到，而且是用完预算才收工的
        return KeyAttempt(ok=False, method="memory-hex", budget_hit=True,
                          detail="预算内没找到")

    det = types.SimpleNamespace(
        installed=True, version="9.9.20.37051", running=True, support=None,
        accounts=[AccountData(
            account="2213914174", root="D:/文档/Tencent Files/2213914174",
            message_dbs=["nt_msg.db", "guild_msg.db", "msg_fts.db"])])

    saved = (pipe.obtain_key, pipe.detect_mod.detect_client, pipe._cached_key)
    pipe.obtain_key = fake_obtain
    pipe.detect_mod.detect_client = lambda key, deep=True: det
    pipe._cached_key = lambda db, spec: ""
    try:
        rep = pipe.run(None, pipe.CollectRequest(client="qq", memory_budget_s=1.0))
    finally:
        (pipe.obtain_key, pipe.detect_mod.detect_client, pipe._cached_key) = saved

    assert len(calls) == 2, f"只该采 2 个库（索引库要跳过），实际 {[c['db'] for c in calls]}"
    assert calls[0]["allow_memory"] is True, "第一个库应该照常搜内存"
    assert calls[1]["allow_memory"] is False, (
        "同一个客户端的密钥刚搜过一遍没找到，第二个库不该再花一份预算")
    assert "不再重复搜" in calls[1]["memory_skip_note"], calls[1]
    assert any("全文索引库" in n for n in rep.notes), (
        f"跳过索引库必须说出来，否则用户会以为少采了几个库：{rep.notes}")
    assert len(rep.dbs) == 2


def main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    skipped = failed = 0
    print("=" * 64)
    print(f"取密钥的时间预算：{len(tests)} 项")
    print("=" * 64)
    for fn in tests:
        try:
            fn()
        except _SkipTest as exc:
            skipped += 1
            print(f"  - 跳过 {fn.__name__}：{exc}")
        except Exception:                       # noqa: BLE001
            failed += 1
            print(f"  x 失败 {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"  + 通过 {fn.__name__}")
    print("-" * 64)
    print(f"通过 {len(tests) - failed - skipped} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
