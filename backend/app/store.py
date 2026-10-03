"""SQLite 存储层。

设计取舍：
- 每次操作开一条新连接（SQLite 打开很便宜，省掉线程安全问题），启用 WAL。
- 向量以 float32 的原始字节存 BLOB，读取时用 np.frombuffer 零拷贝还原。
  几万条消息在内存里做余弦相似度只要毫秒级；数据量再大再换 sqlite-vec/faiss，
  接口藏在 retriever.VectorIndex 后面，不影响上层。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import numpy as np

from .schemas import ChatInfo, Fact, Msg, Persona, Summary

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

CREATE TABLE IF NOT EXISTS voice_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   TEXT,
    channel   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    text      TEXT NOT NULL,
    start_ms  INTEGER DEFAULT 0,
    end_ms    INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO chats (id, platform, name, peer_name, me_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name      = excluded.name,
                    peer_name = CASE WHEN excluded.peer_name <> '' THEN excluded.peer_name ELSE chats.peer_name END,
                    me_name   = CASE WHEN excluded.me_name   <> '' THEN excluded.me_name   ELSE chats.me_name   END
                """,
                (chat_id, platform, name, peer_name, me_name, _now()),
            )

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
                    (chat_id, platform, sender, role, ts, msg_type, text, ext_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

    # ------------------------------------------------------------ voice log

    def add_voice_segment(self, chat_id: str | None, channel: str, ts: str, text: str,
                          start_ms: int, end_ms: int, duration_ms: int) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO voice_log (chat_id, channel, ts, text, start_ms, end_ms, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, channel, ts, text, start_ms, end_ms, duration_ms),
            )

    def list_voice_segments(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM voice_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        out = [dict(r) for r in rows]
        out.reverse()
        return out

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
                "voice_segments": one("SELECT COUNT(*) FROM voice_log"),
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
