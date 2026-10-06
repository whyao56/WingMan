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
    ActivityEntry,
    ChatInfo,
    CollectCursor,
    EngineRun,
    Fact,
    Msg,
    Person,
    PersonChannel,
    PersonDetail,
    PersonPersona,
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
#
# `chat_id` 允许为空：**对象级事实**没有具体的渠道（「她怕黑」这件事
# 不属于某一段微信聊天），存成 chat_id 为空、person_id 非空。
# 渠道级事实照旧带 chat_id。这两种归属都要能被唯一键保护 ——
# 但 SQLite 里 NULL 互不相等，chat_id 为空的行靠 UNIQUE(chat_id,…) 等于没有约束，
# 所以另外补一条**部分唯一索引**（见 `_ensure_facts_person_index`）。
FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT REFERENCES chats(id) ON DELETE CASCADE,
    person_id   TEXT,
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

-- ---------------------------------------------------------------- 输出留存
--
-- 「有动作就有痕迹」：指挥台的输出、导入、采集、批量编辑都要能回看。
-- 这些表里存的是模型生成的**回复原文**，属于隐私数据 ——
-- 和消息一样落在 DATA_DIR，不外传；导出/删除要能覆盖到。
CREATE TABLE IF NOT EXISTS engine_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     TEXT DEFAULT '',
    chat_ids      TEXT DEFAULT '',   -- JSON 数组
    peer_message  TEXT DEFAULT '',
    analysis      TEXT DEFAULT '',   -- JSON
    strategy      TEXT DEFAULT '',   -- JSON
    options       TEXT DEFAULT '',    -- JSON
    trace         TEXT DEFAULT '',   -- JSON
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engine_runs_person  ON engine_runs (person_id);
CREATE INDEX IF NOT EXISTS idx_engine_runs_created ON engine_runs (created_at);

CREATE TABLE IF NOT EXISTS sim_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER DEFAULT 0,   -- 关联 engine_runs.id（0 = 没带上一次运行）
    option_id    TEXT DEFAULT '',
    option_text  TEXT DEFAULT '',
    branches     TEXT DEFAULT '',     -- JSON
    advice       TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sim_runs_run ON sim_runs (run_id);

CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- import | collect_auto | collect_semi | edit | profile
    person_id  TEXT DEFAULT '',
    chat_id    TEXT DEFAULT '',
    summary    TEXT DEFAULT '',
    detail     TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_activity_person ON activity_log (person_id);
CREATE INDEX IF NOT EXISTS idx_activity_chat   ON activity_log (chat_id);
CREATE INDEX IF NOT EXISTS idx_activity_ts     ON activity_log (ts);

-- ---------------------------------------------------------------- 对象级人物设定
--
-- 人物的心愿/雷区/阶段目标是「人」的属性，不是某一段聊天的属性：
-- 在微信里想约她看展，不会因为换到 QQ 聊天就变成另一个目标。
-- 现有 `personas`（chat 级）保留作「渠道级覆盖」，界面只暴露对象级这一张。
CREATE TABLE IF NOT EXISTS person_personas (
    person_id     TEXT PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
    goal          TEXT DEFAULT '',
    my_style      TEXT DEFAULT '',
    peer_profile  TEXT DEFAULT '',
    taboos        TEXT DEFAULT '',
    stage         TEXT DEFAULT '',
    updated_at    TEXT DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_or(value: Any, default: Any) -> Any:
    """把库里存的 JSON 文本读回来；空值或坏值都退到 default（不抛）。"""
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _normalise_ts(value: Any) -> str:
    """把消息时间归一到 ISO 字符串。

    datetime 直接序列化；字符串按 ISO 解析后规范化（统一分隔符与本地时区写法）。
    解析不了就原样返回 —— 校验在前面的接口层做，这里不吞掉用户的输入。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


# 一条消息允许被二次编辑的字段。刻意**不含** chat_id / platform / ext_id：
# 前者是幂等键与游标的依据，后两者是「这条从哪来」的事实，不属于「编辑内容」。
_MESSAGE_EDITABLE_FIELDS = ("sender", "role", "text", "ts", "ts_source")


# 渠道：这个人的记录是从哪儿来的。渠道决定界面上的分组，也决定
# 「按时间合并看」时怎么标注来源。platform 是数据格式，channel 是人的体感 ——
# 用户想的是「我们的微信聊天」，不是「platform=wechat 的数据集」。
#
# `other`：除 QQ / 微信以外的聊天（Telegram、钉钉、短信、贴吧、游戏内私聊…）。
# 它们是**同一个位置**，不是「未知」—— 用户明确说过「其他聊天也算一类」。
# `generic` 是导入适配器的名字（「通用 JSON / CSV」），作为渠道它只是历史遗留的
# 写法，统一归到 `other`，免得列表里出现「通用」这种不懂是什么的标签。
_CHANNEL_BY_PLATFORM = {
    "qq": "qq",
    "wechat": "wechat",
    "other": "other",
    "generic": "other",
    "call": "call",
    "voice": "call",
    "offline": "offline",
    "face": "offline",
}

def _channel_from_platform(platform: str) -> str:
    """platform → channel。**兜底是 `other`，不是 `generic`。**

    认不出来的平台，语义上就是「其他聊天」；旧库里存的 `generic` 也是同一个意思。
    如果这里还留着 `generic` 兜底，界面上就会出现「未知」这种既不准确、
    也没告诉用户该做什么的标签。
    """
    return _CHANNEL_BY_PLATFORM.get((platform or "").strip().lower(), "other")


def _norm_channel(channel: str, platform: str = "") -> str:
    """归一一个**显式传入**的渠道名；传空则按平台推。

    为什么显式传也要过一遍映射：`generic` 这个写法是从导入适配器那边漏进来的，
    界面上它是「其他聊天」。放它进库，就等于允许同一个渠道有两个名字 ——
    而半自动采集正是靠 `channel == client` 认领已有渠道的，两个名字会让它
    在同一个对象下重复建出两个「其他聊天」。
    认不出的名字保持原样：那是调用方有意为之，不该被悄悄改成 other。
    """
    key = (channel or "").strip().lower()
    if not key:
        return _channel_from_platform(platform)
    return _CHANNEL_BY_PLATFORM.get(key, key)


# 「对象级事实」的判定条件。它同时是部分唯一索引的 WHERE、也是写入时的冲突目标 ——
# 两处必须**逐字相同**，否则 SQLite 会认为它们指向不同的索引。
# 渠道级事实带 chat_id，天然被排除；只有 chat_id 留空且有 person_id 的才算对象级。
_FACTS_PERSON_SCOPE = (
    "person_id IS NOT NULL AND person_id <> '' AND (chat_id IS NULL OR chat_id = '')"
)


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
        self._migrate_facts_person()
        self._migrate_person_columns()
        self._migrate_message_columns()
        self._backfill_persons()
        # 回填事实归属必须在 `_backfill_persons` 之后：先让每个 chat 都有归属的人，
        # 这里才回填得到值。
        self._backfill_fact_person()
        self._ensure_facts_person_index()

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

        # `generic` → `other`。这两个词指的是同一件事（「其他聊天」），
        # 但 `generic` 是导入适配器的术语，作为渠道名用户看不懂。归一之后
        # 「谁和谁是同一个渠道」的比较才只有一个口径 —— 半自动采集就是靠
        # `channel == client` 认领已有渠道的，两个写法并存会让它重复建渠道。
        with self.conn() as c:
            n = c.execute(
                "UPDATE chats SET channel = 'other' WHERE channel = 'generic'"
            ).rowcount
        if n:
            log.info("已把 %d 个会话的渠道从 generic 归一为 other", n)

    def _migrate_message_columns(self) -> None:
        """给 messages 补上 `ts_source` 与 `captured_at`。

        `ts_source`：采集（尤其是从剪贴板采集）经常拿不到消息的原始时间，
        只能用采集时刻顶替。顶替不是问题，**顶替了不说才是问题** —— 所以这一列
        必须单独存，而不是靠「时间看起来对不对」去猜。老库补成 'exact'：
        那些行都是导入来的、时间来自原始记录。

        `captured_at`：**采集时刻**，与消息自身的 `ts` 分开存。
        它是「我在什么时候把这条抓进库里」，不是「这条消息发生在什么时候」。
        剪贴板没带时间时用采集时刻冒充 `ts` 会让时间轴失真；
        把两者分开，界面才能同时展示「采集于」与「消息时间」。
        老库这一列为空 —— 历史数据没记过采集时刻，不编造。
        """
        have = self._columns("messages")
        added: list[str] = []
        with self.conn() as c:
            if "ts_source" not in have:
                c.execute("ALTER TABLE messages ADD COLUMN ts_source TEXT DEFAULT 'exact'")
                c.execute("UPDATE messages SET ts_source = 'exact' WHERE ts_source IS NULL")
                added.append("ts_source（老数据一律标为 exact）")
            if "captured_at" not in have:
                c.execute("ALTER TABLE messages ADD COLUMN captured_at TEXT")
                added.append("captured_at（历史数据留空，不编造采集时刻）")
        if added:
            log.info("messages 已加列：%s", "、".join(added))

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

    def _migrate_facts_person(self) -> None:
        """给 facts 补上 `person_id`，并把 `chat_id` 放开为可空。

        两件事必须一起做：对象级事实没有具体渠道，`chat_id` 得留空；
        而老库这一列是 `NOT NULL`，不放开就存不进去。SQLite 不支持 ALTER 改约束，
        只能重建表 —— 用新 DDL 重建，`person_id` 先留空，
        稍后由 `_backfill_fact_person` 按 chats 回填。
        直接读 sqlite_master 的原始 DDL 判断版本，比试探索引更明确。
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
            ).fetchone()
            if row is None:
                return
            ddl = " ".join((row["sql"] or "").split())
            if "person_id" in ddl:
                return  # 已是新结构
            c.execute("ALTER TABLE facts RENAME TO facts_legacy")
            c.execute(FACTS_DDL)
            c.execute(
                """
                INSERT OR IGNORE INTO facts
                    (id, chat_id, person_id, subject, key, value, confidence, evidence, updated_at)
                SELECT id, chat_id, NULL, subject, key, value, confidence, evidence, updated_at
                FROM facts_legacy
                """
            )
            c.execute("DROP TABLE facts_legacy")
        log.info("facts 表已升级：加列 person_id，chat_id 放开为可空（支持对象级事实）")

    def _backfill_fact_person(self) -> None:
        """把已有事实的 person_id 按 `chats.person_id` 回填。

        为什么不一上来就在 DDL 里写死：`facts.chat_id` 指向 chats，
        而 person 归属是 chats 上的字段。回填 = 一次 join 更新，
        比在插入时到处传 person_id 可靠（漏一处就漂移）。
        """
        with self.conn() as c:
            n = c.execute(
                """
                UPDATE facts SET person_id = (
                    SELECT c.person_id FROM chats c WHERE c.id = facts.chat_id
                )
                WHERE (person_id IS NULL OR person_id = '')
                  AND chat_id IS NOT NULL AND chat_id <> ''
                """
            ).rowcount
        if n:
            log.info("已为 %d 条事实回填 person_id", n)

    def _ensure_facts_person_index(self) -> None:
        """给「对象级事实」补一条部分唯一索引，并先清掉重复行。

        为什么不靠 `UNIQUE(chat_id, subject, key, value)`：SQLite 里 NULL 互不相等，
        chat_id 为空的行彼此永不冲突 —— 对象级事实会失去唯一性保护，
        同一条被抽两次就存两条，而且是静默的。部分唯一索引把「对象级」这一子集
        单独约束起来。**建索引前必须先删重复**（保留 id 最小的一条），
        否则 CREATE UNIQUE INDEX 直接失败、整个启动流程挂掉。
        """
        with self.conn() as c:
            removed = c.execute(
                f"""
                DELETE FROM facts
                WHERE ({_FACTS_PERSON_SCOPE})
                  AND id NOT IN (
                      SELECT MIN(id) FROM facts
                      WHERE ({_FACTS_PERSON_SCOPE})
                      GROUP BY person_id, subject, key, value
                  )
                """
            ).rowcount
            c.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_facts_person
                  ON facts(person_id, subject, key, value)
                  WHERE {_FACTS_PERSON_SCOPE}
                """
            )
        if removed:
            log.warning(
                "建对象级事实唯一索引前清掉了 %d 条重复行（保留 id 最小的一条）", removed
            )

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
                    _norm_channel(channel, platform),
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

        # 「提前新建的对象」必须接得住后来导入的记录。
        #
        # 用户被明确告知可以先建一个只有名字的「小鹿」（这时记录还没导），
        # 随后导入小鹿的 QQ 记录。如果这里无条件新建，他会看到两个「小鹿」，
        # 还得自己去合并 —— 而「这条记录属于小鹿」本来就是他表达过的意图。
        #
        # 判据是**这个名字下还没有任何渠道**，这一点是刻意的：
        #   · 空对象里没有任何数据，接住它不可能把谁的记忆混在一起，没有猜错的风险；
        #   · 那个名字下**已经有**记录时，就退回原来的保守策略（新建，让用户显式合并）——
        #     因为此时「QQ 上的张三」和「微信上的张三」是不是同一人，程序无从判断。
        placeholder = self.find_person_by_alias(name)
        if placeholder is not None and self._channel_count(placeholder.id) == 0:
            log.info("「%s」是先前手建的空对象，这次的记录挂到它名下（%s）", name, placeholder.id)
            self.bind_chat(chat_id, placeholder.id)
            return placeholder.id

        new_id = self.create_person(
            name=name, aliases=[row["peer_name"]] if row["peer_name"] else []
        )
        self.bind_chat(chat_id, new_id)
        return new_id

    def _channel_count(self, person_id: str) -> int:
        """这个人名下有几段记录。用来区分「空对象」和「已经有料的人」。"""
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE person_id = ?", (person_id,)
            ).fetchone()
        return int(row["n"] or 0)

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

    def set_chat_platform(self, chat_id: str, platform: str, channel: str = "") -> ChatInfo | None:
        """改一条记录的平台 / 渠道。会话不存在返回 None。

        **绝不改 `chat_id`**：它是消息幂等键与采集游标的依据，改了会破坏去重与增量。
        `channel` 默认由 `platform` 按映射表重算（除非显式传入）—— platform 与 channel
        若各说各话，界面上的分组就和实际平台对不上，而这是静默的。
        """
        info = self.get_chat(chat_id)
        if info is None:
            return None
        plat = (platform or "").strip() or info.platform
        ch = _norm_channel(channel, plat)
        with self.conn() as c:
            c.execute(
                "UPDATE chats SET platform = ?, channel = ? WHERE id = ?",
                (plat, ch, chat_id),
            )
        return self.get_chat(chat_id)

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
            # 对象级人物设定随人一起走；对象级事实由调用方决定是否保留（这里不动 facts）。
            # 不依赖 ON DELETE CASCADE：连接默认没开 foreign_keys，指望级联会漏删。
            c.execute("DELETE FROM person_personas WHERE person_id = ?", (person_id,))
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
                    (chat_id, platform, sender, role, ts, msg_type, text, ext_id, ts_source, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def _message_by_id(self, msg_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return dict(row) if row else None

    def update_message(self, msg_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        """改一条消息。撞幂等键时返回**结构化错误**，不抛异常、不让接口 500。

        幂等键是 `UNIQUE(chat_id, sender, ts, text)`。编辑后的行可能和另一条
        完全一样（同会话 + 同发送者 + 同时间 + 同内容）—— 这时 SQLite 会抛
        `IntegrityError`。对用户来说这不是「服务出错」，而是「你改的这条和已有的
        重复了」，所以要能区分「改成功」和「改重复」，界面才给得出人话提示。

        返回：`{"ok": True, "id", "changed", "message"}`；
        或 `{"ok": False, "id", "error": "not_found"|"duplicate", "detail"}`。
        """
        current = self._message_by_id(msg_id)
        if current is None:
            return {"ok": False, "id": msg_id, "error": "not_found",
                    "detail": f"消息不存在：{msg_id}"}
        fields = [k for k in _MESSAGE_EDITABLE_FIELDS
                  if k in patch and patch[k] is not None]
        if not fields:
            return {"ok": True, "id": msg_id, "changed": 0, "message": current}
        values: list[Any] = [
            _normalise_ts(patch[k]) if k == "ts" else patch[k] for k in fields
        ]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        try:
            with self.conn() as c:
                c.execute(
                    f"UPDATE messages SET {assignments} WHERE id = ?",
                    values + [msg_id],
                )
                changed = c.total_changes
        except sqlite3.IntegrityError:
            return {
                "ok": False, "id": msg_id, "error": "duplicate",
                "detail": "改完和另一条完全一样（同会话 + 同发送者 + 同时间 + 同内容）。",
            }
        return {"ok": True, "id": msg_id, "changed": changed,
                "message": self._message_by_id(msg_id)}

    def delete_messages(self, ids: Sequence[int]) -> int:
        """按 id 批量删消息，返回真删掉的条数。向量随 ON DELETE CASCADE 一起走。"""
        clean = [int(i) for i in (ids or []) if i is not None]
        if not clean:
            return 0
        q = ",".join("?" * len(clean))
        with self.conn() as c:
            c.execute("PRAGMA foreign_keys = ON")
            before = c.total_changes
            c.execute(f"DELETE FROM messages WHERE id IN ({q})", clean)
            return c.total_changes - before

    def insert_manual_message(
        self,
        chat_id: str,
        *,
        sender: str,
        role: str,
        ts: Any,
        text: str,
        msg_type: str = "text",
        ts_source: str = "manual",
        captured_at: str = "",
    ) -> dict[str, Any]:
        """手工往某条会话里加一条消息（聊天记录的「增加」）。

        `ext_id` 留空：它不是从平台读来的，没有外部 id；`ts_source` 默认 `manual`，
        因为时间是人给的。撞幂等键时同样返回结构化错误，不 500。
        """
        info = self.get_chat(chat_id)
        if info is None:
            return {"ok": False, "error": "not_found", "detail": f"会话不存在：{chat_id}"}
        msg = Msg(
            chat_id=chat_id, platform=info.platform, sender=sender, role=role,
            ts=ts, text=text, msg_type=msg_type, ext_id=None,
            ts_source=ts_source, captured_at=captured_at,
        )
        inserted, _skipped = self.insert_messages([msg])
        if not inserted:
            return {"ok": False, "error": "duplicate",
                    "detail": "这条和已有的完全一样（同会话 + 同发送者 + 同时间 + 同内容）。"}
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND sender = ? AND ts = ? AND text = ?",
                (chat_id, msg.sender, msg.ts.isoformat(), msg.text),
            ).fetchone()
        return {"ok": True, "id": int(row["id"]) if row else None,
                "message": dict(row) if row else None}

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
                    (chat_id, person_id, subject, key, value, confidence, evidence, updated_at)
                VALUES (?, (SELECT person_id FROM chats WHERE id = ?), ?, ?, ?, ?, ?, ?)
                """,
                [
                    (chat_id, chat_id, f.subject or subject, f.key, f.value,
                     float(f.confidence), f.evidence, _now())
                    for f in facts if f.key and f.value
                ],
            )
        return len(facts)

    def upsert_fact(self, chat_id: str, fact: Fact) -> None:
        """单条事实的插入或更新。

        冲突目标必须与表上的唯一键完全一致，否则 SQLite 会直接抛
        "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint"。
        `person_id` 一并按 chats 带上：人物级视图要能只靠 facts 表就知道归属，
        不必每次都 join（也避免新写入的事实和迁移回填的旧事实口径不一致）。
        """
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO facts (chat_id, person_id, subject, key, value, confidence, evidence, updated_at)
                VALUES (?, (SELECT person_id FROM chats WHERE id = ?), ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, subject, key, value) DO UPDATE SET
                    person_id = excluded.person_id,
                    confidence = excluded.confidence,
                    evidence = excluded.evidence, updated_at = excluded.updated_at
                """,
                (chat_id, chat_id, fact.subject, fact.key, fact.value,
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
                id=r["id"], chat_id=r["chat_id"], person_id=r["person_id"] or "",
                subject=r["subject"], key=r["key"],
                value=r["value"], confidence=r["confidence"],
                evidence=r["evidence"] or "", updated_at=r["updated_at"] or "",
            )
            for r in rows
        ]

    def list_facts_for_person(self, person_id: str, subject: str | None = None) -> list[Fact]:
        """对象级事实 + 该对象名下所有渠道的事实，合并成一个视图。

        合并的是「视野」不是「去重」：同一条事实若在对象级与渠道级都存在，
        两条都会返回，各自标着 `scope`，界面才能分组显示且不重复计数。
        """
        sql = (
            "SELECT * FROM facts WHERE ("
            f"({_FACTS_PERSON_SCOPE} AND person_id = ?) "
            "OR chat_id IN (SELECT id FROM chats WHERE person_id = ?)"
            ")"
        )
        params: list[Any] = [person_id, person_id]
        if subject:
            sql += " AND subject = ?"
            params.append(subject)
        sql += " ORDER BY subject, key, id"
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        out: list[Fact] = []
        for r in rows:
            scope = "person" if not (r["chat_id"] or "").strip() else "chat"
            out.append(Fact(
                id=r["id"], chat_id=r["chat_id"] or "", person_id=r["person_id"] or "",
                scope=scope, subject=r["subject"], key=r["key"], value=r["value"],
                confidence=r["confidence"], evidence=r["evidence"] or "",
                updated_at=r["updated_at"] or "",
            ))
        return out

    def upsert_person_fact(self, person_id: str, fact: Fact) -> Fact | None:
        """写一条**对象级**事实（不绑具体渠道）。人不存在或内容为空返回 None。

        不直接用 `ON CONFLICT`：冲突目标要匹配那条**部分唯一索引**，
        手写匹配条件容易和索引定义漂移；这里先查后写，语义直白且不依赖缝隙。
        """
        if self.get_person(person_id) is None:
            return None
        key = (fact.key or "").strip()
        value = (fact.value or "").strip()
        if not key or not value:
            return None
        subject = fact.subject or "peer"
        with self.conn() as c:
            row = c.execute(
                f"SELECT id FROM facts WHERE ({_FACTS_PERSON_SCOPE}) "
                "AND person_id = ? AND subject = ? AND key = ? AND value = ?",
                (person_id, subject, key, value),
            ).fetchone()
            if row:
                fid = int(row["id"])
                c.execute(
                    "UPDATE facts SET confidence = ?, evidence = ?, updated_at = ? WHERE id = ?",
                    (float(fact.confidence), fact.evidence or "", _now(), fid),
                )
            else:
                cur = c.execute(
                    "INSERT INTO facts "
                    "(chat_id, person_id, subject, key, value, confidence, evidence, updated_at) "
                    "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (person_id, subject, key, value, float(fact.confidence),
                     fact.evidence or "", _now()),
                )
                fid = int(cur.lastrowid or 0)
            got = c.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
        return Fact(
            id=fid, chat_id="", person_id=person_id, scope="person",
            subject=got["subject"], key=got["key"], value=got["value"],
            confidence=got["confidence"], evidence=got["evidence"] or "",
            updated_at=got["updated_at"] or "",
        )

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

    # ------------------------------------------------------------ 对象级人物设定

    def get_person_persona(self, person_id: str) -> PersonPersona:
        """对象级人物设定。没有就返回一份空的（不是 None）——
        界面首次打开就该能编辑，而不是先报「不存在」。"""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM person_personas WHERE person_id = ?", (person_id,)
            ).fetchone()
        if not row:
            return PersonPersona(person_id=person_id)
        return PersonPersona(
            person_id=row["person_id"], goal=row["goal"] or "",
            my_style=row["my_style"] or "", peer_profile=row["peer_profile"] or "",
            taboos=row["taboos"] or "", stage=row["stage"] or "",
            updated_at=row["updated_at"] or "",
        )

    def save_person_persona(self, person_id: str, patch: dict[str, Any]) -> PersonPersona | None:
        """局部更新对象级设定：只写传进来的字段，其余保持。人不存在返回 None。"""
        if self.get_person(person_id) is None:
            return None
        cur = self.get_person_persona(person_id)
        fields = ("goal", "my_style", "peer_profile", "taboos", "stage")
        merged = {
            f: (str(patch[f]) if f in patch and patch[f] is not None else getattr(cur, f))
            for f in fields
        }
        now = _now()
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO person_personas
                    (person_id, goal, my_style, peer_profile, taboos, stage, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    goal = excluded.goal, my_style = excluded.my_style,
                    peer_profile = excluded.peer_profile, taboos = excluded.taboos,
                    stage = excluded.stage, updated_at = excluded.updated_at
                """,
                (person_id, merged["goal"], merged["my_style"], merged["peer_profile"],
                 merged["taboos"], merged["stage"], now),
            )
        return PersonPersona(person_id=person_id, updated_at=now, **merged)

    # ------------------------------------------------------------ 输出留存

    def save_run(
        self,
        *,
        person_id: str = "",
        chat_ids: Sequence[str] = (),
        peer_message: str = "",
        analysis: Any = None,
        strategy: Any = None,
        options: Any = None,
        trace: Any = None,
    ) -> int:
        """留存一次指挥台运行，返回 run id。

        `options` 里是模型生成的回复原文（隐私），只落本机 DATA_DIR。
        JSON 一律 `ensure_ascii=False` 存原文，方便直接肉眼核对。
        """
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT INTO engine_runs
                    (person_id, chat_ids, peer_message, analysis, strategy, options, trace, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (person_id or "",
                 json.dumps(list(chat_ids or []), ensure_ascii=False),
                 peer_message or "",
                 json.dumps(analysis or {}, ensure_ascii=False),
                 json.dumps(strategy or {}, ensure_ascii=False),
                 json.dumps(list(options or []), ensure_ascii=False),
                 json.dumps(trace or {}, ensure_ascii=False),
                 _now()),
            )
            return int(cur.lastrowid or 0)

    @staticmethod
    def _run_model(row: sqlite3.Row) -> EngineRun:
        return EngineRun(
            id=row["id"], person_id=row["person_id"] or "",
            chat_ids=_json_or(row["chat_ids"], []),
            peer_message=row["peer_message"] or "",
            analysis=_json_or(row["analysis"], {}),
            strategy=_json_or(row["strategy"], {}),
            options=_json_or(row["options"], []),
            trace=_json_or(row["trace"], {}),
            created_at=row["created_at"] or "",
        )

    def list_runs(self, person_id: str = "", limit: int = 50) -> list[EngineRun]:
        """按人（可空 = 全部）取历史输出，时间倒序。"""
        sql = "SELECT * FROM engine_runs"
        params: list[Any] = []
        if person_id:
            sql += " WHERE person_id = ?"
            params.append(person_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 50), 500)))
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._run_model(r) for r in rows]

    def get_run(self, run_id: int) -> EngineRun | None:
        """取一次运行的完整内容（含它的推演），供「回看 / 复用」。"""
        with self.conn() as c:
            row = c.execute("SELECT * FROM engine_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = self._run_model(row)
        run.sim_runs = self.list_sim_runs(run_id)
        return run

    def delete_run(self, run_id: int) -> int:
        """删一次运行（连同它的推演），返回真删掉的运行条数。"""
        with self.conn() as c:
            c.execute("DELETE FROM sim_runs WHERE run_id = ?", (run_id,))
            before = c.total_changes
            c.execute("DELETE FROM engine_runs WHERE id = ?", (run_id,))
            return c.total_changes - before

    def save_sim_run(
        self,
        *,
        run_id: int = 0,
        option_id: str = "",
        option_text: str = "",
        branches: Any = None,
        advice: str = "",
    ) -> int:
        """留存一次推演。`run_id=0` 表示这次推演没关联到具体的运行。"""
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT INTO sim_runs (run_id, option_id, option_text, branches, advice, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(run_id or 0), option_id or "", option_text or "",
                 json.dumps(list(branches or []), ensure_ascii=False), advice or "", _now()),
            )
            return int(cur.lastrowid or 0)

    def list_sim_runs(self, run_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM sim_runs WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            {"id": r["id"], "run_id": r["run_id"], "option_id": r["option_id"] or "",
             "option_text": r["option_text"] or "",
             "branches": _json_or(r["branches"], []),
             "advice": r["advice"] or "", "created_at": r["created_at"] or ""}
            for r in rows
        ]

    # ------------------------------------------------------------ 动作痕迹

    def log_activity(
        self,
        kind: str,
        *,
        person_id: str = "",
        chat_id: str = "",
        summary: str = "",
        detail: str = "",
        ts: str = "",
    ) -> int:
        """记一条动作痕迹（导入 / 采集 / 编辑 / 画像）。

        它是「有动作就有痕迹」的落点：对象详情要能回看「这个人的数据是怎么来的」。
        """
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT INTO activity_log (ts, kind, person_id, chat_id, summary, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ts or _now(), str(kind or ""), person_id or "", chat_id or "",
                 summary or "", detail or ""),
            )
            return int(cur.lastrowid or 0)

    def list_activity(
        self,
        *,
        person_id: str = "",
        chat_id: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> list[ActivityEntry]:
        sql = "SELECT * FROM activity_log WHERE 1 = 1"
        params: list[Any] = []
        if person_id:
            sql += " AND person_id = ?"
            params.append(person_id)
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 100), 500)))
        with self.conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            ActivityEntry(
                id=r["id"], ts=r["ts"] or "", kind=r["kind"] or "",
                person_id=r["person_id"] or "", chat_id=r["chat_id"] or "",
                summary=r["summary"] or "", detail=r["detail"] or "",
            )
            for r in rows
        ]

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
