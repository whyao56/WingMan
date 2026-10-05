"""在**真实数据库的副本**上验证结构迁移，全程不碰真库本体。

    cd wingman
    python scripts/verify_migration.py                # 自动定位 backend/data/wingman.db
    python scripts/verify_migration.py --db D:\\path\\to\\wingman.db
    python scripts/verify_migration.py --keep         # 保留副本目录，便于手工翻查

为什么需要它：`Store.init()` 会做三件事 —— 升级 `facts` 的唯一约束、
给 `chats` 加 `person_id / channel / source` 三列、给没有归属的会话回填「人」。
这些都改**结构**，而结构改造最典型的失败不是崩溃，是**静默丢数据**
（消息还在但没人能读到、会话没归属所以界面上看不到）。
单元测试用的是空库，跑不出这类问题，只有真库的形状才试得出来。

它检查四件事：

1. **一条消息都不少**：迁移前后各表行数完全一致。
2. **没有悬空引用**：每个会话都有归属，且归属指向真实存在的人。
3. **重复 init 幂等**：连跑三次，人数 / 归属 / 消息数一个都不能变
   （`init()` 在每次启动时都会跑，不幂等就是「启动一次多一批数据」）。
4. **新会话立刻有归属**：写入一个会话后不重启就能被识别，
   否则运行中导入的记录在界面上等于不存在。

退出码：0 全部通过 | 1 有检查未通过 | 2 环境问题（缺库 / 缺依赖）。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DEFAULT_DB = ROOT / "backend" / "data" / "wingman.db"

PASS = 0
FAIL = 0


def _configure_console() -> None:
    """中文 Windows 控制台默认 cp936，打印中文偶尔会抛 UnicodeEncodeError。

    只调输出编码，不改任何检查行为。与 scripts/e2e_check.py 的处理保持一致。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def ok(label: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  [OK]   {label}" + (f" —— {detail}" if detail else ""), flush=True)


def bad(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {label}" + (f" —— {detail}" if detail else ""), flush=True)


def warn(label: str, detail: str = "") -> None:
    print(f"  [note] {label}" + (f" —— {detail}" if detail else ""), flush=True)


def _snapshot(db: Path) -> dict[str, int]:
    """直接读 sqlite 数一遍行数，作为「有没有丢数据」的基准。"""
    tables = ("chats", "messages", "facts", "summaries", "embeddings", "persons")
    out: dict[str, int] = {}
    con = sqlite3.connect(str(db))
    try:
        for table in tables:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            out[table] = (
                con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if row else -1
            )
    finally:
        con.close()
    return out


def _fmt(counts: dict[str, int]) -> str:
    return "  ".join(
        f"{k}={'-' if v < 0 else v}" for k, v in counts.items()
    )


def main() -> int:
    _configure_console()

    ap = argparse.ArgumentParser(description="在真库副本上验证结构迁移")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="源数据库路径（只读复制，不会被改）")
    ap.add_argument("--keep", action="store_true", help="保留副本目录以便手工翻查")
    args = ap.parse_args()

    source = Path(args.db)
    print("=" * 72)
    print("结构迁移验证（操作对象是副本，真库不会被动）")
    print("=" * 72)

    if not source.is_file():
        print(f"\n源数据库不存在：{source}")
        print("先在应用里导入一份聊天记录，或用 --db 指定别的库。")
        return 2

    try:
        from app.store import Store
    except ImportError as exc:
        print(f"\n无法导入后端模块，请先装依赖（wingman.cmd 会自动装）：{exc}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="wingman_migrate_"))
    copy = tmp / source.name
    try:
        # copy2 保留时间戳；副本与真库完全同名同目录结构，避免任何路径假设出偏差
        shutil.copy2(source, copy)
        print(f"\n源库：{source}")
        print(f"副本：{copy}")

        before = _snapshot(copy)
        print(f"\n迁移前的行数：{_fmt(before)}")

        store = Store(copy)
        store.init()

        after = _snapshot(copy)
        print(f"迁移后的行数：{_fmt(after)}")

        # ---------------------------------------------------- 1 一条都不少
        print("\n[1] 消息与记忆数据一条都不能少")
        for table in ("chats", "messages", "facts", "summaries", "embeddings"):
            b, a = before.get(table, -1), after.get(table, -1)
            if b < 0 and a < 0:
                continue
            if b < 0:
                warn(f"{table}：迁移前不存在，迁移后 {a} 行")
            elif b == a:
                ok(f"{table}：{a} 行，未变")
            else:
                bad(f"{table}：{b} → {a} 行", "行数变了，迁移不该动数据")
        if after.get("persons", -1) > 0:
            ok(f"persons：新建 {after['persons']} 行", "迁移产生的归属记录")

        # ---------------------------------------------------- 2 没有悬空引用
        print("\n[2] 归属完整：每个会话都有「人」，且那个人真实存在")
        chats = store.list_chats()
        persons = store.list_persons()
        print(f"      会话 {len(chats)} 个，人 {len(persons)} 个")
        for p in persons:
            print(f"      · {p.name:<12} 渠道={p.channel_count} 消息={p.message_count} "
                  f"我={p.me_count} 对方={p.peer_count} 索引={p.indexed}"
                  + (f"  {p.first_ts} → {p.last_ts}" if p.first_ts else ""))

        unowned = [c.id for c in chats if not c.person_id]
        if chats and not unowned:
            ok("每个会话都有归属")
        elif unowned:
            bad(f"{len(unowned)} 个会话没有归属", ", ".join(unowned[:5]))
        else:
            warn("库里还没有会话，跳过归属检查")

        known = {p.id for p in persons}
        dangling = [(c.id, c.person_id) for c in chats if c.person_id and c.person_id not in known]
        if dangling:
            bad(f"{len(dangling)} 个会话挂在不存在的人上", str(dangling[:3]))
        else:
            ok("归属都指向真实存在的人")

        # 渠道列必须真的有值：读接口有兜底，直接读列的地方没有
        blank_channel = [c.id for c in chats if not c.channel]
        if blank_channel:
            bad(f"{len(blank_channel)} 个会话的 channel 是空的", ", ".join(blank_channel[:5]))
        elif chats:
            ok("每个会话都有渠道标记", f"取值 {sorted({c.channel for c in chats})}")

        # ---------------------------------------------------- 3 幂等
        print("\n[3] 重复 init 幂等（init 每次启动都会跑）")
        base = (
            len(persons),
            {c.id: c.person_id for c in chats},
            _snapshot(copy).get("messages", 0),
        )
        for _ in range(2):
            store.init()
        again = (
            len(store.list_persons()),
            {c.id: c.person_id for c in store.list_chats()},
            _snapshot(copy).get("messages", 0),
        )
        if base == again:
            ok("连跑三次 init，人数 / 归属 / 消息数都没变")
        else:
            bad("重复 init 改变了状态", f"前 {base[:1]}... 后 {again[:1]}...")

        # ---------------------------------------------------- 4 新会话立刻有归属
        print("\n[4] 运行中写入的新会话立刻有归属（用户不该为了看到它而重启）")
        probe = "__verify_migration_probe__"
        store.upsert_chat(probe, "wechat", "验证用", peer_name="验证用")
        hit = store.find_person_by_alias("验证用")
        if hit is not None and store.get_chat(probe).person_id == hit.id:
            ok("新会话写入即归属到新的人")
        else:
            bad("新会话没有立刻产生归属", "导入的记录在界面上会看不到，直到重启")
        # 清掉验证痕迹（都在副本里，随后整个目录一起删）
        store.delete_chat(probe)
        if hit is not None:
            store.delete_person(hit.id)

        # ---------------------------------------------------- 结果
        print("\n" + "=" * 72)
        if FAIL == 0:
            print(f"全部通过（{PASS} 项检查）。真库未被修改。")
        else:
            print(f"{PASS} 项通过，{FAIL} 项失败。真库未被修改。")
        print("=" * 72)
        return 0 if FAIL == 0 else 1
    finally:
        # 失败时一律保留副本，方便自己翻；成功时按 --keep 决定。
        if FAIL != 0 or not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n（--keep）副本已保留，可以自己翻：\n  {copy}")


if __name__ == "__main__":
    raise SystemExit(main())
