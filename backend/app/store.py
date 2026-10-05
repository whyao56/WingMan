"""SQLite 存储层。

设计取舍：
- 每次操作开一条新连接（SQLite 打开很便宜，省掉线程安全问题），启用 WAL。
- 向量以 float32 的原始字节存 BLOB，读取时用 np.frombuffer 零拷贝还原。
  几万条消息在内存里做余弦相似度只要毫秒级；数据量再大再换 sqlite-vec/faiss，
  接口藏在 retriever.VectorIndex 后面，不影响上层。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import numpy as np

from .schemas import (
    ChatInfo,
    CollectCursor,
    Fact,
    Msg,
    Person,
    PersonChannel,
    PersonDetail,
    Persona,
    Summary,
)

log = logging.getLogger("wingman.store")

# ---------------------------------------------------------------- Schema

# facts 表的唯一键必须带上 value。
#
# 「喜欢」是一对多关系：喜欢猫、喜欢火锅、喜欢陶艺是三条并列的事实。
# 早期版本唯一键写成 (chat_id, subject, key)，配合 INSERT OR REPLACE，
# 会让后抽到的事实**静默覆盖**先抽到的 —— 「她喜欢猫」就这么消失了，
# 而且不报任何错。单独抽成常量是因为升级老库时要复用这段 DDL 重建表。
FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL DEFAULT 'peer',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0.6,
    evidence    TEXT DEFAULT '',
    updated_at  TEXT DEFAULT '',
    UNIQUE (chat_id, subject, key, value)
);
"""

SCHEMA = f"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chats (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    name        TEXT NOT NULL,
    peer_name   TEXT DEFAULT '',
    me_name     TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    sender      TEXT NOT NULL,
    role        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    msg_type    TEXT DEFAULT 'text',
    text        TEXT NOT NULL,
    ext_id      TEXT,
    -- 这条消息的时间是哪儿来的：exact（平台记录里带的原始时间）|
    -- assumed（剪贴板里没带时间，用抓取时刻顶替）| manual（用户手填）。
    -- 单独存一列而不是靠猜：剪贴板采集经常拿不到真实时间，
    -- 混进真时间轴会让「上周聊了什么」这类结论建立在假时间上。
    ts_source   TEXT DEFAULT 'exact',
    UNIQUE (chat_id, sender, ts, text)
);

CREATE INDEX IF NOT EXISTS idx_msg_chat_ts   ON messages (chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_chat_role ON messages (chat_id, role);

CREATE TABLE IF NOT EXISTS embeddings (
    message_id  INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL
);

{FACTS_DDL}

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'weekly',
    period      TEXT DEFAULT '',
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT '',
    UNIQUE (chat_id, kind, period)
);

CREATE TABLE IF NOT EXISTS personas (
    chat_id       TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    goal          TEXT DEFAULT '',
    my_style      TEXT DEFAULT '',
    peer_profile  TEXT DEFAULT '',
    taboos        TEXT DEFAULT '',
    stage         TEXT DEFAULT '',
    updated_at    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- 以人为中心
--
-- 「人」是记忆的真正主键，chat 只是这个人在某个渠道上的一段记录。
-- 为什么要这么分：同一个人在 QQ 和微信上是两个 chat，但记忆必须是同一份 ——
-- 「她怕黑」这件事不该因为换了平台就查不到。所以关系定位、阶段目标、
-- 永久记忆都挂在 persons 上，chat 只负责「这段对话来自哪里」。
CREATE TABLE IF NOT EXISTS persons (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    -- 别名/昵称，JSON 数组。跨平台把同一个人认出来的依据。
    aliases          TEXT NOT NULL DEFAULT '[]',
    -- 我和 Ta 的关系（现状），以及我希望走到哪一步、这一阶段的目标。
    -- 这三项是「人」级别的，不是「会话」级别的。
    relation         TEXT DEFAULT '',
    desired_relation TEXT DEFAULT '',
    stage_goal       TEXT DEFAULT '',
    notes            TEXT DEFAULT '',
    created_at       TEXT DEFAULT '',
    updated_at       TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_person_name ON persons (name);

-- 采集游标：增量采集的唯一依据。
--
-- 之所以要「内容指纹」而不是只看时间：同一个时间点可能有多条同秒消息，
-- 也可能平台改了历史消息。只比时间会漏、只比条数会错位；
-- 「最后一条的 (ts, 文本哈希) + 已采条数」两项一起比，才不会重复也不会漏。
CREATE TABLE IF NOT EXISTS collect_cursors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT NOT NULL,          -- wechat | qq
    account          TEXT NOT NULL DEFAULT '',-- 平台账号（多账号时才区分）
    peer_key         TEXT NOT NULL,          -- 平台内部的会话标识
    person_id        TEXT,                   -- 归到哪个人名下（可空，未归并时）
    chat_id          TEXT,                   -- 落库后的会话 id
    -- 上次成功采集到的时间点与内容指纹
    last_ts          TEXT DEFAULT '',
    last_ext_id      TEXT DEFAULT '',
    fingerprint      TEXT DEFAULT '',
    -- 已合并的条数，用来校验「这次比上次多了几条」
    merged_count     INTEGER NOT NULL DEFAULT 0,
    -- 采集窗口起点（用户选的那个「从哪天开始」），用于首次全量
    collected_from   TEXT DEFAULT '',
    last_run_at      TEXT DEFAULT '',
    status           TEXT DEFAULT 'idle',    -- idle | ok | skipped | error
    message          TEXT DEFAULT '',
    UNIQUE (platform, account, peer_key)
);

CREATE INDEX IF NOT EXISTS idx_cursor_person ON collect_cursors (person_id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 渠道：这个人的记录是从哪儿来的。渠道决定界面上的分组，也决定
# 「按时间合并看」时怎么标注来源。platform 是数据格式，channel 是人的体感 ——
# 用户想的是「我们的微信聊天」，不是「platform=wechat 的数据集」。
_CHANNEL_BY_PLATFORM = {
    "qq": "qq",
    "wechat": "wechat",
    "call": "call",
    "voice": "call",
    "offline": "offline",
    "face": "offline",
}


def _channel_from_platform(platform: str) -> str:
    return _CHANNEL_BY_PLATFORM.get((platform or "").strip().lower(), "generic")


def _fingerprint_of(rows: Sequence[dict[str, Any]]) -> str:
    """一段消息的内容指纹：条数 + 最后一条的 (id, 时间, 文本哈希)。

    为什么不只比时间：同一秒可能有多条消息，只比时间会漏掉末尾几条；
    为什么不只比条数：平台侧删过历史消息时条数会减少，此时应该重新采而不是跳过。
    两者一起比，才能既「无变动就不动」又「变了一定发现」。
    """
    if not rows:
        return "empty"
    tail = rows[-1]
    body = f"{len(rows)}|{tail.get('ext_id') or tail.get('id') or ''}|{tail.get('ts', '')}|{tail.get('text', '')}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _to_np(blob: bytes, dim: int) -> np.ndarray:
    if not blob:
        return np.zeros(dim, dtype=np.float32)
    return np.frombuffer(blob, dtype=np.float32, count=dim).copy()


# 项目原名 ChatWing，数据库文件曾叫 chatwing.db。改名后要让老数据跟过来，
# 否则用户会看到「数据全没了」——其实只是换了个文件名没找着。
_LEGACY_DB_NAMES: tuple[str, ...] = ("chatwing.db",)
_DB_SIDECAR_SUFFIXES: tuple[str, ...] = ("-journal", "-wal", "-shm")


def _migrate_legacy_db_file(db_path: Path) -> bool:
    """把老名字的库文件改名为新名字。返回是否真的搬过。

    只在「新文件不存在」时才搬，避免覆盖用户已经写好的新库。
    同目录下 rename 是原子的；万一跨设备失败，退化为复制并保留原件。
    """
    if db_path.exists():
        return False
    for legacy_name in _LEGACY_DB_NAMES:
        legacy = db_path.with_name(legacy_name)
        if not legacy.exists():
            continue
        try:
            legacy.rename(db_path)
        except OSError:
            shutil.copy2(legacy, db_path)
            legacy.unlink(missing_ok=True)
        for suffix in _DB_SIDECAR_SUFFIXES:
            src = legacy.with_name(legacy.name + suffix)
            if src.exists():
                src.rename(db_path.with_name(db_path.name + suffix))
        log.warning(
            "检测到旧版数据库文件 %s，已自动更名为 %s（数据保留）",
            legacy_name, db_path.name,
        )
        return True
    return False


class Store:
    """同步的 SQLite 封装。API 层是异步的，用时用 asyncio.to_thread 包一下即可。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _migrate_legacy_db_file(self.db_path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 连接

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = sqlite3.connect(self.db_path, timeout=15.0)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)
            # executescript 会隐式提交，且 PRAGMA foreign_keys 在每个新连接上都要重设
        with self.conn() as c:
            c.execute("PRAGMA foreign_keys = ON")
        self._migrate_facts_unique()
        self._migrate_person_columns()
        self._migrate_message_columns()
        self._backfill_persons()

    # ------------------------------------------------------------ 结构迁移

    def _columns(self, table: str) -> set[str]:
        with self.conn() as c:
            rows = c.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _migrate_person_columns(self) -> None:
        """给 chats 补上「属于哪个人」「来自哪个渠道」两列。

        用 `ALTER TABLE ADD COLUMN` 而不是重建表：这两列都能为 NULL，
        老库的行补上空值即可，不会碰到既有数据。SQLite 支持加列，
        但不支持加约束，所以把约束放在代码里而不是 DDL 里。
        """
        have = self._columns("chats")
        with self.conn() as c:
            if "person_id" not in have:
                c.execute("ALTER TABLE chats ADD COLUMN person_id TEXT")
                log.info("chats 已加列 person_id（老数据待回填）")
            if "channel" not in have:
                c.execute("ALTER TABLE chats ADD COLUMN channel TEXT")
                log.info("chats 已加列 channel")
            if "source" not in have:
                # import（手工导入）| collect（采集器写入），用于区分数据来路
                c.execute("ALTER TABLE chats ADD COLUMN source TEXT DEFAULT 'import'")
        with self.conn() as c:
            c.execute("CREATE INDEX IF NOT EXISTS idx_chat_person ON chats (person_id)")
        # 已有的行要补上渠道。不补的话 `chats.channel` 是 NULL，
        # 而读 API 时有一层「空则按 platform 推」的兜底 —— 于是列表看着正常、
        # 直接读列的地方（比如按人取消息）拿到 None。同一个字段两处口径不同，
        # 就是下一颗雷，所以在迁移里一次补齐。
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, platform FROM chats WHERE channel IS NULL OR channel = ''"
            ).fetchall()
            for row in rows:
                c.execute(
                    "UPDATE chats SET channel = ? WHERE id = ?",
                    (_channel_from_platform(row["platform"]), row["id"]),
                )
        if rows:
            log.info("已为 %d 个会话补上渠道标记", len(rows))

    def _migrate_message_columns(self) -> None:
        """给 messages 补上 `ts_source`。

        采集（尤其是从剪贴板采集）经常拿不到消息的原始时间，只能用抓取时刻顶替。
        顶替不是问题，**顶替了不说才是问题** —— 所以这一列必须单独存，
        而不是靠「时间看起来对不对」去猜。老库补成 'exact'：
        那些行都是导入来的、时间来自原始记录。
        """
        have = self._columns("messages")
        if "ts_source" in have:
            return
        with self.conn() as c:
            c.execute("ALTER TABLE messages ADD COLUMN ts_source TEXT DEFAULT 'exact'")
            c.execute("UPDATE messages SET ts_source = 'exact' WHERE ts_source IS NULL")
        log.info("messages 已加列 ts_source（老数据一律标为 exact）")

    def _backfill_persons(self) -> None:
        """给还没归属的 chat 各建一个「人」并挂上去。

        老库（以及 `upsert_chat` 之前写入的会话）原本只有 chat 的概念。如果不回填，
        界面上「人物」列表会是空的，用户看到的就是「我的数据哪去了」。
        回填规则：一个 chat 先对应一个人，之后由用户手动合并 —— 自动猜
        「QQ 上的张三和微信上的张三是不是同一个人」没有可靠依据，不该猜。

        具体动作交给 `ensure_person_for_chat`，这样「导入时顺手建的人」和
        「启动时补建的人」用的是同一条规则，不会出现两套行为。
        """
        with self.conn() as c:
            rows = c.execute(
                "SELECT id FROM chats WHERE person_id IS NULL OR person_id = ''"
            ).fetchall()
        for row in rows:
            self.ensure_person_for_chat(row["id"])
        if rows:
            log.info("已为 %d 个会话回填「人」实体", len(rows))

    def _migrate_facts_unique(self) -> None:
        """把老库 facts 表的唯一约束从 (chat_id, subject, key) 升到四列。

        SQLite 不支持改动约束，只能重建表。新约束比旧约束更宽松
        （老库每个 key 只存得下一条），所以数据不会丢。
        直接读 sqlite_master 里的原始 DDL 来判断版本，比试探索引更明确。
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
            ).fetchone()
            if row is None:
                return
            ddl = " ".join((row["sql"] or "").split())
            if "chat_id, subject, key, value" in ddl:
                return  # 已经是新约束

            c.execute("ALTER TABLE facts RENAME TO facts_legacy")
            c.execute(FACTS_DDL)
            c.execute(
                """
                INSERT OR IGNORE INTO facts
                    (id, chat_id, subject, key, value, confidence, evidence, updated_at)
                SELECT id, chat_id, subject, key, value, confidence, evidence, updated_at
                FROM facts_legacy
                """
            )
            c.execute("DROP TABLE facts_legacy")
            log.info("facts 表已升级：同 key 现在可以并存多个不同的 value")

    # ------------------------------------------------------------ chats

    def upsert_chat(
        self,
        chat_id: str,
        platform: str,
        name: str,
        peer_name: str = "",
        me_name: str = "",
        channel: str = "",
        source: str = "",
        person_id: str = "",
    ) -> None:
        """写入/更新一个会话。

        收尾时必须保证这个会话有归属的「人」。为什么放在这里而不是放在调用方：
        会话没有归属，在「以人为中心」的界面上等于不存在 —— 用户导入了一份记录，
        人物列表里却什么都没有，只有重启（`init()` 回填）才出现。
        把不变量放在最低层，后续任何写入路径（导入、采集器、脚本）都不会漏。

        `person_id` 是给采集器用的**显式归属**：采集时先按对方的昵称/备注
        找到已有的人，再传进来，就不会每次采集都多出一个同名的人。
        不传则由本方法按「一个会话一个人」新建，绝不按名字去猜 ——
        见 `_backfill_persons` 的说明。
        """
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO chats (id, platform, name, peer_name, me_name, created_at, channel, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name      = excluded.name,
                    peer_name = CASE WHEN excluded.peer_name <> '' THEN excluded.peer_name ELSE chats.peer_name END,
                    me_name   = CASE WHEN excluded.me_name   <> '' THEN excluded.me_name   ELSE chats.me_name   END,
                    channel   = CASE WHEN chats.channel IS NULL OR chats.channel = ''
                                     THEN excluded.channel ELSE chats.channel END,
                    source    = CASE WHEN chats.source IS NULL OR chats.source = ''
                                     THEN excluded.source ELSE chats.source END
                """,
                (
                    chat_id, platform, name, peer_name, me_name, _now(),
                    channel or _channel_from_platform(platform),
                    source or "import",
                ),
            )
        self.ensure_person_for_chat(chat_id, person_id)

    def ensure_person_for_chat(self, chat_id: str, person_id: str = "") -> str:
        """保证某个会话有归属的人，返回它的 person_id。

        - 传了 `person_id`：用它（覆盖旧的归属），不会新建人。
        - 没传且已有归属：原样返回，动都不动（幂等）。
        - 没传且没有归属：按「对方称呼 > 会话名」新建一个人再挂上。

        只做「有没有」的判断，不做「像不像同一个人」的猜测：把 QQ 上的张三
        和微信上的张三合成一个人没有可靠依据，猜错是静默的 —— 用户不会发现
        两个人的记忆被混在了一起。合并永远由用户显式发起。
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT id, name, peer_name, person_id FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return ""
        if person_id:
            if row["person_id"] != person_id:
                self.bind_chat(chat_id, person_id)
            return person_id
        if row["person_id"]:
            return row["person_id"]
        name = (row["peer_name"] or row["name"] or "未命名").strip()
        new_id = self.create_person(
            name=name, aliases=[row["peer_name"]] if row["peer_name"] else []
        )
        self.bind_chat(chat_id, new_id)
        return new_id

    def get_chat(self, chat_id: str) -> ChatInfo | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
            if not row:
                return None
            stat = c.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(role = 'peer') AS peer_cnt,
                       SUM(role = 'me')   AS me_cnt,
                       MIN(ts) AS first_ts,
                       MAX(ts) AS last_ts
                FROM messages WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            indexed = c.execute(
                "SELECT COUNT(*) AS n FROM embeddings e "
                "JOIN messages m ON m.id = e.message_id WHERE m.chat_id = ?",
                (chat_id,),
            ).fetchone()["n"]
            return ChatInfo(
                id=row["id"],
                platform=row["platform"],
                name=row["name"],
                peer_name=row["peer_name"] or "",
                me_name=row["me_name"] or "",
                created_at=row["created_at"] or "",
                message_count=stat["total"] or 0,
                peer_count=stat["peer_cnt"] or 0,
                me_count=stat["me_cnt"] or 0,
                first_ts=stat["first_ts"],
                last_ts=stat["last_ts"],
                indexed=indexed,
                person_id=row["person_id"] or "",
                channel=(row["channel"] or _channel_from_platform(row["platform"])),
                source=(row["source"] or "import"),
            )

    def list_chats(self) -> list[ChatInfo]:
        with self.conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM chats ORDER BY id").fetchall()]
        out: list[ChatInfo] = []
        for cid in ids:
            info = self.get_chat(cid)
            if info:
                out.append(info)
        return out

    def delete_chat(self, chat_id: str) -> None:
        with self.conn() as c:
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def rename_chat(self, chat_id: str, name: str, peer_name: str = "", me_name: str = "") -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE chats SET name = ?, peer_name = COALESCE(NULLIF(?, ''), peer_name), "
                "me_name = COALESCE(NULLIF(?, ''), me_name) WHERE id = ?",
                (name, peer_name, me_name, chat_id),
            )

    def set_roles(self, chat_id: str, me_names: Sequence[str]) -> int:
        """导入时角色猜错了？用它一次性纠正整条会话的 me / peer 归属。"""
        names = [n.strip() for n in me_names if n and n.strip()]
        if not names:
            return 0
        q = ",".join("?" * len(names))
        with self.conn() as c:
            c.execute(
                f"UPDATE messages SET role = CASE WHEN sender IN ({q}) THEN 'me' ELSE 'peer' END "
                f"WHERE chat_id = ?",
                names + [chat_id],
            )
            changed = c.total_changes
            me_name = max(names, key=len)
            c.execute("UPDATE chats SET me_name = ? WHERE id = ?", (me_name, chat_id))
        return changed

    # ------------------------------------------------------------ 人（persons）
    #
    # 「人」是记忆的中心。这一组方法的存在意义是：让「同一个人在 QQ 和微信上
    # 的对话」能被当成同一个人来分析和回忆，而不是两个互不相干的数据集。

    def create_person(self, name: str, aliases: Sequence[str] | None = None) -> str:
        """新建一个人，返回它的 id。"""
        pid = f"p_{uuid.uuid4().hex[:12]}"
        now = _now()
        clean = [a.strip() for a in (aliases or []) if a and a.strip()]
        with self.conn() as c:
            c.execute(
                "INSERT INTO persons (id, name, aliases, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, name.strip() or "未命名", json.dumps(clean, ensure_ascii=False), now, now),
            )
        return pid

    def get_person(self, person_id: str) -> Person | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        if not row:
            return None
        return self._person_with_stats(row)

    def find_person_by_alias(self, name: str) -> Person | None:
        """按名字或别名找人，用于「采集到的会话该挂到谁名下」。

        只做精确匹配（忽略大小写与首尾空白）：模糊匹配会把「小明」和「小明明」
        合成一个人，而这种错误是静默的 —— 用户不会发现两个人被混在一起了。
        """
        key = (name or "").strip().lower()
        if not key:
            return None
        for p in self.list_persons():
            if p.name.strip().lower() == key:
                return p
            if any(a.strip().lower() == key for a in p.aliases):
                return p
        return None

    def list_persons(self) -> list[Person]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM persons ORDER BY updated_at DESC, name").fetchall()
        return [self._person_with_stats(r) for r in rows]

    def _person_with_stats(self, row: sqlite3.Row) -> Person:
        """把某个人名下所有渠道的消息汇总成一个总览。

        这个汇总就是「以人为中心」的落点：界面上看到的条数、时间跨度、
        索引量都是跨渠道合并后的数字，而不是某一个会话的。
        """
        pid = row["id"]
        with self.conn() as c:
            agg = c.execute(
                """
                SELECT COUNT(m.id) AS total,
                       SUM(m.role = 'peer') AS peer_cnt,
                       SUM(m.role = 'me')   AS me_cnt,
                       MIN(m.ts) AS first_ts,
                       MAX(m.ts) AS last_ts
                FROM messages m JOIN chats ch ON ch.id = m.chat_id
                WHERE ch.person_id = ?
                """,
                (pid,),
            ).fetchone()
            # 渠道数要直接数 chats 的行，**不能**数 messages 里出现过的 chat_id：
            # 那样「刚挂上、还没导入记录的会话」不算数 —— 于是列表页写着 1 个渠道，
            # 点进去却列出 2 段记录的入口，两个数字自相矛盾。
            # 口径与 `person_detail().channels`（`len(channels)`）保持一致：
            # 这里的「一个渠道」= 一段记录（`PersonChannel`）。
            channel_count = c.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE person_id = ?", (pid,)
            ).fetchone()["n"]
            indexed = c.execute(
                "SELECT COUNT(*) AS n FROM embeddings e "
                "JOIN messages m ON m.id = e.message_id "
                "JOIN chats ch ON ch.id = m.chat_id WHERE ch.person_id = ?",
                (pid,),
            ).fetchone()["n"]
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except (ValueError, TypeError):
            aliases = []
        return Person(
            id=pid,
            name=row["name"],
            aliases=[str(a) for a in aliases if str(a).strip()],
            relation=row["relation"] or "",
            desired_relation=row["desired_relation"] or "",
            stage_goal=row["stage_goal"] or "",
            notes=row["notes"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            channel_count=channel_count or 0,
            message_count=agg["total"] or 0,
            peer_count=agg["peer_cnt"] or 0,
            me_count=agg["me_cnt"] or 0,
            first_ts=agg["first_ts"],
            last_ts=agg["last_ts"],
            indexed=indexed,
        )

    def update_person(self, person_id: str, patch: dict[str, Any]) -> Person:
        """只更新传进来的字段，没传的保持原值。

        关系 / 期望 / 阶段目标这三项是用户手写的核心内容，绝不能被一次
        局部保存冲掉 —— 所以这里永远不用「整行覆盖」，只做逐字段合并。
        """
        fields: list[str] = []
        values: list[Any] = []
        for key in ("name", "relation", "desired_relation", "stage_goal", "notes"):
            if key in patch and patch[key] is not None:
                text = str(patch[key])
                if key == "name":
                    text = text.strip()
                    if not text:
                        continue
                fields.append(f"{key} = ?")
                values.append(text)
        if "aliases" in patch and patch["aliases"] is not None:
            raw = patch["aliases"]
            if isinstance(raw, str):
                raw = [x.strip() for x in raw.replace("，", ",").split(",")]
            clean = [str(x).strip() for x in raw if str(x).strip()]
            fields.append("aliases = ?")
            values.append(json.dumps(clean, ensure_ascii=False))
        if fields:
            fields.append("updated_at = ?")
            values.append(_now())
            values.append(person_id)
            with self.conn() as c:
                c.execute(f"UPDATE persons SET {', '.join(fields)} WHERE id = ?", values)
        got = self.get_person(person_id)
        if got is None:
            raise KeyError(f"没有这个人：{person_id}")
        return got

    def bind_chat(self, chat_id: str, person_id: str, channel: str = "") -> None:
        """把一个会话挂到某个人名下（可顺带改渠道）。"""
        with self.conn() as c:
            if channel:
                c.execute(
                    "UPDATE chats SET person_id = ?, channel = ? WHERE id = ?",
                    (person_id, channel, chat_id),
                )
            else:
                c.execute("UPDATE chats SET person_id = ? WHERE id = ?", (person_id, chat_id))

    def unbind_chat(self, chat_id: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE chats SET person_id = NULL WHERE id = ?", (chat_id,))

    def merge_persons(self, keep_id: str, merge_ids: Sequence[str]) -> Person:
        """把几个人合并成一个人（跨平台归并）。

        别名取并集、手写字段以 keep 为准但不为空者补齐 —— 合并是不可逆的，
        所以宁可保守：不确定的字段留空让用户自己填，也不去猜。
        """
        keep = self.get_person(keep_id)
        if keep is None:
            raise KeyError(f"没有这个人：{keep_id}")
        merged_aliases = list(keep.aliases)
        patch: dict[str, Any] = {}
        for mid in merge_ids:
            if mid == keep_id:
                continue
            other = self.get_person(mid)
            if other is None:
                continue
            merged_aliases.append(other.name)
            merged_aliases.extend(other.aliases)
            for field in ("relation", "desired_relation", "stage_goal", "notes"):
                if not getattr(keep, field) and getattr(other, field):
                    patch[field] = getattr(other, field)
            with self.conn() as c:
                c.execute("UPDATE chats SET person_id = ? WHERE person_id = ?", (keep_id, mid))
                c.execute("UPDATE collect_cursors SET person_id = ? WHERE person_id = ?", (keep_id, mid))
                c.execute("DELETE FROM persons WHERE id = ?", (mid,))
        # 去重（保持顺序）
        seen: set[str] = set()
        deduped: list[str] = []
        for a in merged_aliases:
            key = a.strip().lower()
            if not key or key == keep.name.strip().lower() or key in seen:
                continue
            seen.add(key)
            deduped.append(a.strip())
        patch["aliases"] = deduped
        return self.update_person(keep_id, patch)

    def delete_person(self, person_id: str) -> None:
        """删除一个人。

        下属会话**不删**，只解除绑定 —— 消息是不可再生资产，
        删一个人不该顺手毁掉几万条聊天记录。真要清数据就逐个会话删。
        """
        with self.conn() as c:
            c.execute("UPDATE chats SET person_id = NULL WHERE person_id = ?", (person_id,))
            c.execute("DELETE FROM collect_cursors WHERE person_id = ?", (person_id,))
            c.execute("DELETE FROM persons WHERE id = ?", (person_id,))

    def person_detail(self, person_id: str) -> PersonDetail | None:
        person = self.get_person(person_id)
        if person is None:
            return None
        with self.conn() as c:
            ids = [
                r["id"] for r in c.execute(
                    "SELECT id FROM chats WHERE person_id = ? ORDER BY id", (person_id,)
                ).fetchall()
            ]
        channels: list[PersonChannel] = []
        for cid in ids:
            info = self.get_chat(cid)
            if not info:
                continue
            channels.append(PersonChannel(
                chat_id=info.id, channel=info.channel, platform=info.platform,
                name=info.name, peer_name=info.peer_name, me_name=info.me_name,
                source=info.source,
                message_count=info.message_count, peer_count=info.peer_count,
                me_count=info.me_count, first_ts=info.first_ts, last_ts=info.last_ts,
                indexed=info.indexed,
            ))
        return PersonDetail(person=person, channels=channels)

    def person_messages(
        self, person_id: str, limit: int = 400, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        """按人取消息 —— 跨渠道合并，按时间排。

        这是「不要以聊天记录为中心」最直接的一处体现：调用方不需要知道
        这条消息来自 QQ 还是微信，只看时间线。每行带上渠道标记，
        让界面能标出「这条是通话里说的」。
        """
        sql = (
            "SELECT m.*, ch.channel, ch.name AS chat_name FROM messages m "
            "JOIN chats ch ON ch.id = m.chat_id WHERE ch.person_id = ?"
        )
        params: list[Any] = [person_id]
        if before_id is not None:
            sql += " AND m.id < ?"
            params.append(before_id)
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        out = [dict(r) for r in rows]
        out.reverse()
        return out

    # ------------------------------------------------------------ 采集游标

    def get_cursor(self, platform: str, account: str, peer_key: str) -> CollectCursor | None:
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM collect_cursors WHERE platform = ? AND account = ? AND peer_key = ?",
                (platform, account or "", peer_key),
            ).fetchone()
        if not row:
            return None
        return CollectCursor(
            platform=row["platform"], account=row["account"] or "", peer_key=row["peer_key"],
            person_id=row["person_id"] or "", chat_id=row["chat_id"] or "",
            last_ts=row["last_ts"] or "", last_ext_id=row["last_ext_id"] or "",
            fingerprint=row["fingerprint"] or "", merged_count=row["merged_count"] or 0,
            collected_from=row["collected_from"] or "", last_run_at=row["last_run_at"] or "",
            status=row["status"] or "idle", message=row["message"] or "",
        )

    def list_cursors(self, platform: str = "", person_id: str = "") -> list[CollectCursor]:
        sql = "SELECT * FROM collect_cursors WHERE 1 = 1"
        params: list[Any] = []
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if person_id:
            sql += " AND person_id = ?"
            params.append(person_id)
        sql += " ORDER BY last_run_at DESC, peer_key"
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            CollectCursor(
                platform=r["platform"], account=r["account"] or "", peer_key=r["peer_key"],
                person_id=r["person_id"] or "", chat_id=r["chat_id"] or "",
                last_ts=r["last_ts"] or "", last_ext_id=r["last_ext_id"] or "",
                fingerprint=r["fingerprint"] or "", merged_count=r["merged_count"] or 0,
                collected_from=r["collected_from"] or "", last_run_at=r["last_run_at"] or "",
                status=r["status"] or "idle", message=r["message"] or "",
            )
            for r in rows
        ]

    def save_cursor(
        self,
        platform: str,
        account: str,
        peer_key: str,
        *,
        person_id: str = "",
        chat_id: str = "",
        last_ts: str = "",
        last_ext_id: str = "",
        fingerprint: str = "",
        merged_count: int = 0,
        collected_from: str = "",
        status: str = "ok",
        message: str = "",
    ) -> None:
        """写入/更新采集游标。下一次采集就是靠它决定「从哪儿继续、要不要跳过」。"""
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO collect_cursors
                    (platform, account, peer_key, person_id, chat_id, last_ts, last_ext_id,
                     fingerprint, merged_count, collected_from, last_run_at, status, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, account, peer_key) DO UPDATE SET
                    person_id      = CASE WHEN excluded.person_id <> '' THEN excluded.person_id ELSE collect_cursors.person_id END,
                    chat_id        = CASE WHEN excluded.chat_id   <> '' THEN excluded.chat_id   ELSE collect_cursors.chat_id END,
                    last_ts        = CASE WHEN excluded.last_ts   <> '' THEN excluded.last_ts   ELSE collect_cursors.last_ts END,
                    last_ext_id    = CASE WHEN excluded.last_ext_id <> '' THEN excluded.last_ext_id ELSE collect_cursors.last_ext_id END,
                    fingerprint    = CASE WHEN excluded.fingerprint <> '' THEN excluded.fingerprint ELSE collect_cursors.fingerprint END,
                    merged_count   = CASE WHEN excluded.merged_count > 0 THEN excluded.merged_count ELSE collect_cursors.merged_count END,
                    collected_from = CASE WHEN excluded.collected_from <> '' THEN excluded.collected_from ELSE collect_cursors.collected_from END,
                    last_run_at    = excluded.last_run_at,
                    status         = excluded.status,
                    message        = excluded.message
                """,
                (platform, account or "", peer_key, person_id, chat_id, last_ts, last_ext_id,
                 fingerprint, merged_count, collected_from, _now(), status, message),
            )

    def reset_cursor(self, platform: str, account: str, peer_key: str) -> int:
        """重置采集游标 = 下次重新全量采。返回真删掉的条数。

        `account` 传空字符串表示**该平台下这个人的所有账号**都重置。
        为什么要有这个语义：界面上用户看到的是「小鹿 · QQ」这一行，
        他不知道（也不该知道）游标是按 `(平台, 账号, 对方)` 三段存的主键。
        如果空账号被当成「账号必须是空字符串」，用户点「重置」时
        什么都删不掉，而接口照样返回成功 —— 这是最坏的一种失败：
        没有任何报错，用户以为重置了，下次采集却还是从老位置继续。
        """
        sql = "DELETE FROM collect_cursors WHERE platform = ? AND peer_key = ?"
        params: list[Any] = [platform, peer_key]
        if account:
            sql += " AND account = ?"
            params.append(account)
        with self.conn() as c:
            before = c.total_changes
            c.execute(sql, params)
            return c.total_changes - before

    def merged_message_count(self, chat_id: str, since: str = "") -> int:
        """某会话已落库的条数（可限定起点），用于和游标里的记录对账。"""
        sql = "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?"
        params: list[Any] = [chat_id]
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        with self.conn() as c:
            return int(c.execute(sql, params).fetchone()["n"] or 0)

    def messages_fingerprint(self, chat_id: str, since: str = "") -> str:
        """**库里实际存的**那段消息的内容指纹。

        为什么由 store 来算，而不是让采集器拿「这一次读到的消息」去算：
        采集器手上的那批消息里，有一部分会因为 UNIQUE 约束被忽略（早就有了）。
        用「读到的」算指纹，等于把「我以为采到了什么」当成「库里有什么」——
        两者一旦不一致（比如同一条消息在两次采集里文本略有差别），
        下一次采集就会误判成「内容变了」，反复重采同一段。
        指纹必须从事实（数据库）里读，不能从事物（本次读到的）里推。
        """
        sql = "SELECT ext_id, ts, text FROM messages WHERE chat_id = ?"
        params: list[Any] = [chat_id]
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts, id"
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, params).fetchall()]
        return _fingerprint_of(rows)

    # ------------------------------------------------------------ messages

    def insert_messages(self, msgs: Sequence[Msg]) -> tuple[int, int]:
        """返回 (inserted, skipped)。靠 UNIQUE 约束实现幂等。"""
        if not msgs:
            return 0, 0
        rows = [m.to_row() for m in msgs]
        with self.conn() as c:
            before = c.total_changes
            c.executemany(
                """
                INSERT OR IGNORE INTO messages
                    (chat_id, platform, sender, role, ts, msg_type, text, ext_id, ts_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted = c.total_changes - before
        return inserted, len(rows) - inserted

    def list_messages(
        self, chat_id: str, limit: int = 200, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        """按时间倒序取，返回时已翻正序，方便前端直接渲染。"""
        sql = "SELECT * FROM messages WHERE chat_id = ?"
        args: list[Any] = [chat_id]
        if before_id is not None:
            sql += " AND id < ?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        rows.reverse()
        return rows

    def recent_messages(self, chat_id: str, n: int = 20) -> list[dict[str, Any]]:
        return self.list_messages(chat_id, limit=n)

    def messages_by_ids(self, ids: Sequence[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(
                f"SELECT * FROM messages WHERE id IN ({q}) ORDER BY id", list(ids)
            ).fetchall()]
        return rows

    def last_peer_message(self, chat_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND role = 'peer' "
                "ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def search_text(self, chat_id: str, terms: Sequence[str], limit: int = 200) -> list[dict[str, Any]]:
        """朴素的关键词召回，配合向量召回做混合检索。"""
        terms = [t for t in terms if t and len(t) >= 1][:8]
        if not terms:
            return []
        where = " OR ".join(["text LIKE ?"] * len(terms))
        args: list[Any] = [chat_id] + [f"%{t}%" for t in terms]
        with self.conn() as c:
            rows = c.execute(
                f"SELECT * FROM messages WHERE chat_id = ? AND ({where}) "
                f"ORDER BY id DESC LIMIT ?",
                args + [limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def all_messages(self, chat_id: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ embeddings

    def upsert_embeddings(self, model: str, items: Iterable[tuple[int, np.ndarray]]) -> int:
        payload = [
            (mid, model, int(vec.shape[0]), vec.astype(np.float32).tobytes())
            for mid, vec in items
        ]
        if not payload:
            return 0
        with self.conn() as c:
            c.executemany(
                """
                INSERT INTO embeddings (message_id, model, dim, vec) VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    model = excluded.model, dim = excluded.dim, vec = excluded.vec
                """,
                payload,
            )
        return len(payload)

    def unembedded(self, chat_id: str, model: str, limit: int = 4000) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT m.* FROM messages m
                LEFT JOIN embeddings e ON e.message_id = m.id AND e.model = ?
                WHERE m.chat_id = ? AND e.message_id IS NULL
                ORDER BY m.id LIMIT ?
                """,
                (model, chat_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_embeddings(self, chat_id: str, model: str) -> tuple[list[int], np.ndarray, int]:
        """返回 (message_ids, 矩阵 N×D, dim)。矩阵已 L2 归一化，检索时直接点积即余弦。"""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT e.message_id, e.dim, e.vec FROM embeddings e
                JOIN messages m ON m.id = e.message_id
                WHERE m.chat_id = ? AND e.model = ?
                ORDER BY e.message_id
                """,
                (chat_id, model),
            ).fetchall()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32), 0
        dim = rows[0]["dim"]
        ids = [r["message_id"] for r in rows]
        mat = np.vstack([_to_np(r["vec"], r["dim"]) for r in rows]).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return ids, mat / norms, dim

    def count_embeddings(self, model: str | None = None) -> int:
        with self.conn() as c:
            if model:
                row = c.execute("SELECT COUNT(*) n FROM embeddings WHERE model = ?", (model,)).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) n FROM embeddings").fetchone()
        return row["n"]

    # ------------------------------------------------------------ facts

    def replace_facts(self, chat_id: str, subject: str, facts: Sequence[Fact]) -> int:
        """同 subject 下整体替换 —— 重跑画像时避免残留过时事实。"""
        with self.conn() as c:
            c.execute("DELETE FROM facts WHERE chat_id = ? AND subject = ?", (chat_id, subject))
            c.executemany(
                """
                INSERT OR REPLACE INTO facts
                    (chat_id, subject, key, value, confidence, evidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (chat_id, f.subject or subject, f.key, f.value,
                     float(f.confidence), f.evidence, _now())
                    for f in facts if f.key and f.value
                ],
            )
        return len(facts)

    def upsert_fact(self, chat_id: str, fact: Fact) -> None:
        """单条事实的插入或更新。

        冲突目标必须与表上的唯一键完全一致，否则 SQLite 会直接抛
        "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint"。
        """
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO facts (chat_id, subject, key, value, confidence, evidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, subject, key, value) DO UPDATE SET
                    confidence = excluded.confidence,
                    evidence = excluded.evidence, updated_at = excluded.updated_at
                """,
                (chat_id, fact.subject, fact.key, fact.value,
                 float(fact.confidence), fact.evidence, _now()),
            )

    def list_facts(self, chat_id: str, subject: str | None = None) -> list[Fact]:
        sql = "SELECT * FROM facts WHERE chat_id = ?"
        args: list[Any] = [chat_id]
        if subject:
            sql += " AND subject = ?"
            args.append(subject)
        sql += " ORDER BY subject, key"
        with self.conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [
            Fact(
                id=r["id"], chat_id=r["chat_id"], subject=r["subject"], key=r["key"],
                value=r["value"], confidence=r["confidence"],
                evidence=r["evidence"] or "", updated_at=r["updated_at"] or "",
            )
            for r in rows
        ]

    def delete_fact(self, fact_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM facts WHERE id = ?", (fact_id,))

    # ------------------------------------------------------------ summaries

    def add_summary(self, s: Summary) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO summaries (chat_id, kind, period, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, kind, period) DO UPDATE SET
                    content = excluded.content, created_at = excluded.created_at
                """,
                (s.chat_id, s.kind, s.period, s.content, _now()),
            )

    def list_summaries(self, chat_id: str, limit: int = 5) -> list[Summary]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM summaries WHERE chat_id = ? ORDER BY period DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [
            Summary(id=r["id"], chat_id=r["chat_id"], kind=r["kind"],
                    period=r["period"] or "", content=r["content"],
                    created_at=r["created_at"] or "")
            for r in rows
        ]

    # ------------------------------------------------------------ persona

    def get_persona(self, chat_id: str) -> Persona:
        with self.conn() as c:
            row = c.execute("SELECT * FROM personas WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return Persona(chat_id=chat_id)
        return Persona(
            chat_id=row["chat_id"], goal=row["goal"] or "", my_style=row["my_style"] or "",
            peer_profile=row["peer_profile"] or "", taboos=row["taboos"] or "",
            stage=row["stage"] or "", updated_at=row["updated_at"] or "",
        )

    def save_persona(self, chat_id: str, patch: dict[str, str]) -> Persona:
        cur = self.get_persona(chat_id)
        merged = cur.model_copy(update={k: v for k, v in patch.items() if k in Persona.model_fields})
        merged.chat_id = chat_id
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO personas (chat_id, goal, my_style, peer_profile, taboos, stage, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    goal = excluded.goal, my_style = excluded.my_style,
                    peer_profile = excluded.peer_profile, taboos = excluded.taboos,
                    stage = excluded.stage, updated_at = excluded.updated_at
                """,
                (chat_id, merged.goal, merged.my_style, merged.peer_profile,
                 merged.taboos, merged.stage, _now()),
            )
        merged.updated_at = _now()
        return merged

    # ------------------------------------------------------------ kv

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def kv_delete(self, key: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM kv WHERE key = ?", (key,))

    def kv_all(self) -> dict[str, str]:
        with self.conn() as c:
            rows = c.execute("SELECT key, value FROM kv").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------ stats

    def counts(self) -> dict[str, int]:
        with self.conn() as c:
            def one(sql: str) -> int:
                return int(c.execute(sql).fetchone()[0])
            return {
                "chats": one("SELECT COUNT(*) FROM chats"),
                "messages": one("SELECT COUNT(*) FROM messages"),
                "embeddings": one("SELECT COUNT(*) FROM embeddings"),
                "facts": one("SELECT COUNT(*) FROM facts"),
                "summaries": one("SELECT COUNT(*) FROM summaries"),
                "personas": one("SELECT COUNT(*) FROM personas"),
            }

    def export_chat_json(self, chat_id: str) -> dict[str, Any]:
        """导出为自包含 JSON，方便备份 / 迁移 / 在 Obsidian 里归档。"""
        info = self.get_chat(chat_id)
        if not info:
            raise KeyError(chat_id)
        return {
            "chat": info.model_dump(),
            "persona": self.get_persona(chat_id).model_dump(),
            "facts": [f.model_dump() for f in self.list_facts(chat_id)],
            "summaries": [s.model_dump() for s in self.list_summaries(chat_id, limit=100)],
            "messages": self.all_messages(chat_id),
        }
