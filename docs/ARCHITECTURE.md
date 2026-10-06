# 架构设计

本文档描述 WingMan 的整体结构、模块职责、数据模型与接口契约。
读完之后你应该能清楚地知道：**每个功能该改哪个文件**。

---

## 1. 设计原则

| 原则 | 含义 | 落点 |
|---|---|---|
| **本地优先** | 所有原始数据默认只存在本机 `backend/data/wingman.db`，不上传 | 存储层用 SQLite 单文件 |
| **插件式接入** | 新增聊天平台不改核心代码，只加一个 Adapter | `adapters/` |
| **模型可切换** | 云 / 本地 / Mock 三档自由切换，接口完全一致 | `llm/`、`memory/embedder` |
| **降级可跑** | 任何外部依赖缺失时退化为 Mock，而不是崩溃 | 每个工厂函数都返回可用实例 |
| **Prompt 集中** | 所有提示词放一个文件，方便调优和对照 | `engine/prompts.py` |
| **可解释** | 每条建议都附理由、证据和风险，不输出黑箱结论 | 引擎输出结构里强制带 `rationale` / `evidence` |
| **采集要诚实** | 认不出的东西宁可挂起，也不猜一个看起来合理的值填进去 | 说话人认不出就跳过并计数；时间是顶替来的就标 `ts_source=assumed`；解密认证失败返回 `None` 而不是「大概能读」 |

---

## 2. 分层结构

```
┌─────────────────────────────────────────────────────────────────┐
│  表现层   frontend/index.html （单文件控制台，零构建）             │
│           导入区 · 记忆区 · 指挥台 · 采集区 · 通话区（规划中）     │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────────┐
│  API 层   app/api/                                               │
│  routes_data.py    导入/列表/消息/画像                            │
│  routes_engine.py  分析/建议/推演/策略                            │
│  routes_admin.py   设置读写/健康检查/Provider 探测                │
│  routes_collect.py 采集：探测/版本矩阵/取密钥/自动/半自动/游标     │
│  routes_persons.py 以人为中心的会话归并与清理                      │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  引擎层   app/engine/        ← 项目的灵魂                         │
│  analyzer  → 对方情绪 / 意图 / 潜台词 / 兴趣度 / 风险              │
│  planner   → 关系阶段判定与目标策略                                │
│  suggestor → 候选回复生成 + 多维度打分排序                         │
│  simulator → 逐条回复的多轮走向推演，输出概率分支                  │
└──────────┬──────────────────────────────┬───────────────────────┘
           │                              │
┌──────────▼──────────────┐   ┌───────────▼───────────────────────┐
│ 记忆层 app/memory/       │   │ 模型层 app/llm/                   │
│ store(在 app/store.py)   │   │ llm: openai_compat/ollama/mock    │
│ embedder  向量化          │   │ complete_json 容错解析            │
│ retriever 混合检索        │   │ 依赖缺失自动降级为 mock            │
│ profiler  画像与事实抽取  │   └───────────┬───────────────────────┘
└──────────┬──────────────┘               │
           │                              │
┌──────────▼──────────────────────────────▼───────────────────────┐
│  数据层   SQLite  backend/data/wingman.db                       │
│  persons · chats · messages · embeddings · facts · summaries     │
│  · personas · collect_cursors                                    │
└──────────────────▲──────────────────────────▲───────────────────┘
                   │                          │
    ┌──────────────┴───────────┐  ┌───────────┴──────────────────┐
    │ 接入层 app/adapters/     │  │ 采集层 app/collect/          │
    │ base(抽象) · qq          │  │ detect 探测 · matrix 版本矩阵 │
    │ wechat · generic         │  │ keys 取密钥 · sqlcipher 解密  │
    │ registry(自动选适配器)   │  │ reader 认列 · pipeline 自动   │
    │                          │  │ semi 半自动(剪贴板) · winapi  │
    └──────────────────────────┘  └──────────────────────────────┘

  两条入口都归一成同一套 `Msg` 再进数据层：
  接入层吃「用户导出的文件」，采集层吃「客户端自己的数据库 / 剪贴板」。
```

---

## 3. 各层职责与文件映射

### 3.1 接入层 `app/adapters/`

把任意来源的聊天记录，转换成统一的 `Msg` 序列。

```python
class ChatSourceAdapter(ABC):
    name: str                 # 适配器标识，如 "qq"
    display_name: str         # 中文名，给前端下拉框用
    extensions: tuple[str, ...]

    def sniff(self, path: Path, head: str) -> float: ...
    """返回 0~1 的置信度，registry 用它自动选适配器。"""

    def parse(self, path: Path, options: dict) -> Iterator[Msg]: ...
    """产出统一消息流。抛 NotImplementedError 之外的异常视为解析失败。"""
```

**为什么要 `sniff`**：用户不需要知道自己的导出文件是哪个平台的格式。
上传时 `registry.detect()` 按置信度排序，自动选最匹配的，并把候选列表返回给前端做二次确认。

| 文件 | 说明 |
|---|---|
| `base.py` | 抽象基类 + `Msg` 定义 + 时间/昵称归一化工具 |
| `qq.py` | QQ 消息管理器导出格式（`时间 昵称(QQ号)` 头 + 正文多行） |
| `wechat.py` | 微信导出格式（时间与昵称顺序不固定，用宽松正则） |
| `generic.py` | JSON / JSONL / CSV，字段名可配置映射 |
| `registry.py` | 注册表 + `sniff` 自动选择 + `import_file()` 落库 |

**扩展一个新平台**：在 `adapters/` 新建文件实现 `ChatSourceAdapter`，
在 `registry.py` 的 `ADAPTERS` 列表里加一行，完成。

### 3.2 采集层 `app/collect/`

**两条并列的路**：一条全自动（读客户端自己的本地库），一条半自动（监听剪贴板）。

为什么是「并列」而不是「自动 + 兜底」：自动那条必须在**你这台机器、你这个版本**上
拿到数据库密钥才能走通，而这一步在本机实测的两个版本上**没有走通**（结论见下）。
所以半自动不是备胎 —— 它是当下唯一确定能跑通的通道，两边都是一等公民。

| 通道 | 入口 | 适用 | 「谁说的」怎么分 |
|---|---|---|---|
| **自动** | 读客户端本地加密数据库 | 版本在支持矩阵内、且能拿到密钥 | 库里的说话人字段 |
| **半自动** | 剪贴板（你复制哪条就抓哪条） | 任何版本 / 任何客户端，只要能复制 | 剪贴板块头里的称呼 |

#### 自动：六道关，每道都可能合理地失败

```
① 探测  detect.py     装了吗 / 开着吗 / 版本在支持范围内吗 / 数据目录在哪
② 取密钥 keys.py      缓存 → 用户粘贴 → 自动搜进程内存（有预算、有结论）
③ 解密  sqlcipher.py  剥自定义头、逐页解密、套用 WAL（不套会漏掉最近的消息）
④ 认列  reader.py     这个库里哪一列是时间、哪一列是正文、哪一列是说话人
⑤ 读消息 reader.py    按游标取增量，判「这是我说的还是对方说的」
⑥ 入库  pipeline.py   会话归属到人、消息去重写入、记下游标
```

每道关的失败都返回「为什么 + 下一步做什么」，而不是抛异常或返回空。
这条链路上最贵的事不是慢，是**「看起来成功了」**：用户以为采到了、其实库里一条都没有，
或者更糟 —— 采进一堆错归属的消息。所以 `CollectReport` 的每个字段都带人的解释。

#### 版本指引是数据，不是 README 里的一段话

`matrix.py` 的 `SUPPORT_MATRIX` 是一张**数据表**：每个客户端（`wechat` / `qq`）的
进程名、数据目录模板、库文件名、支持的版本区间，以及**我实测过的版本号**。
`judge()` 拿「装没装 + 探测到的版本」去查这张表，直接产出结论与下一步动作
（`guide_for()`）；界面在**配置环节**就把「你的版本支不支持、不支持该做什么」摆出来。
写在 README 里的版本表，没人会在装软件前先读一遍；写在 `matrix.py` 里，界面自己就会说。

#### 自动取密钥为什么要如实报「取不到」

`scan_memory_for_key()` 在 `QQ.exe` / `Weixin.exe` 的进程内存里按签名找密钥，有**时间预算**
（正式 60 秒、预演 12 秒），超了就是超了。它返回 `KeyAttempt` 结论对象而不是布尔值 ——
因为「没找到」有两种完全不同的原因：**扫完了确实没有** vs **到点收工、没扫完**。
混成一个 `False`，用户就会以为「这功能不行」，而实际只是预算给少了。

**预算必须贴在每一层循环上。** 这条不是风格问题：预算是参数、注释也写了为什么要有它，
但它最初只在外层查了一次，于是第 1 级（逐进程读内存 + 正则扫十六进制）一次都没查、
第 2 级的内层 `for off in range(len(blob) - 32)` 也查不到。实测本机（7 个 QQ 进程、
2.9 GB 可读内存）：第 1 级逐进程 12.5 / 9.6 / 2.0 / 9.1 / 5.8 / 1.9 / 2.5 秒，
**合计 43 秒**；第 2 级单候选约 9.4 µs，一个 8.4 MB 的锚点区域 = 840 万候选 × 2 套参数
≈ **157 秒**。于是「12 秒的预演」实际要跑三分钟以上 —— 承诺了一个时间却做不到，
比慢更坏。现在内层每 4096 个候选查一次（约 38 ms，密度和开销都合适）。

同一个客户端的内存只搜一遍：内存内容几秒内不会变，前面几个库已经按预算搜过、
没找到，后面的库不再重复等一份同样的预算（结论由 `KeyAttempt.budget_hit` 传递）。
全文索引库（`*_fts.db`）根本不采 —— 里面只有分词表、没有消息行，采不到东西，
而每个都要过一次取密钥 + 解密。

本机实测结论（83.78M 候选 × 2 套参数各扫 419 秒未命中；hex 候选 QQ 8 个全灭、微信 0 处）
写在 `keys.py` 的模块文档里，界面照实转述，不粉饰。

#### 半自动：剪贴板是唯一通道

不是偷懒选的 —— UIA **读不到**消息文本（微信整个主窗口只有 2 个 UIA 节点，
QQ 内容区只有一个 `Chrome_RenderWidgetHostHWND`）。所以「监听你复制了什么」
是唯一不依赖版本、不依赖密钥的通用做法。

```
剪贴板变化  winapi.py     GetClipboardSequenceNumber() 只在内容真变时 +1
    │                     （轮询到同一个号就什么都不做，不会重复入库）
    ▼
切块解析    clipboard.py  称呼: 正文 ／ 称呼 + 时间戳（QQ NT 多选复制的主形态）
    │                     ／ 称呼 + 时间戳 + 同行正文
    ▼
归属判定    semi.py       称呼对得上 → 直接落库；对不上 → 挂起等你在界面里选
```

两条底线，不做成可关闭的选项：

1. **认不出说话人就挂起，不猜。** 挂起项在界面上等你选「这是谁」或「丢弃」；
   `assume_peer`（认不出一律当对方）默认**关**，要显式打开才生效。
2. **没带时间就如实标。** 剪贴板里经常没有时间戳，用抓取时刻顶替可以，
   但必须写成 `ts_source='assumed'` 单独存一列 —— 混进真时间轴，会让
   「上周聊了什么」这类结论建立在假时间上。也可以选 `ask` 模式：先挂起等你填。

半自动的状态（待确认项、计数）会落盘，进程重启能捞回来；已落库的不会重复捞。

#### 几个关键实现取舍

| 取舍 | 为什么 |
|---|---|
| `Scored.tags`（机器判断）与 `Scored.why`（给人看的理由）**分成两个字段** | 「命中登录账号」是「未命中登录账号」的子串，人看的文案不能拿去当判据 |
| 解密认证失败**返回 `None`**，不返回半个结果 | fail-closed：宁可报「解不开」，也不能吐出一堆看似正常、实际是垃圾的数据 |
| 认列**按数据认**，不写死版本映射 | 升级会改列名（QQ 的列名就是数字 `40011`/`40033`），认不出就如实报候选列 |
| 解密结果与认列结果都**缓存** | 几十上百 MB 的库逐页 HMAC 要几百毫秒~几秒；按 `(主库大小, mtime, WAL 大小, WAL mtime, salt)` 做指纹 |
| `Name2Id` 要区分「单列 + rowid 当 id」（`rowid_map`）与「两列 alias」 | 微信 4.x 是前者；当成后者会静默少解一级，alias 全空却不报错 |
| Windows 专有 API 一律**延迟到调用时**才碰，不在模块级导入 | `ctypes.wintypes` 在 Linux 上导不进来（`'v'` 类型码只有 Windows 版 `_ctypes` 认）。写成模块级 import 的话，**非 Windows 上连 import 都失败** —— 功能不可用是对的，「导入就炸」不是 |

> 最后一条是踩出来的：它在 `winapi.py` 和 `clipboard.py` 里各出现了一次。
> 现在 `winapi.py` 统一决定 `wintypes` 是什么（Windows 用真身，真身不存在才退到最小替身，
> 且 Windows 上导不进来会 re-raise 而不用替身盖过去），`clipboard.py` 从它取。
> 守卫测试 `test_collect_offwindows.py` 模拟 Linux 环境、逐个模块重导一遍钉住它。

守卫测试：`test_collect_cipher.py`（自造库往返解密到字节级）、
`test_collect_reader.py`（合成库认列与增量）、`test_collect_clipboard.py`（三种复制形态）、
`test_collect_semi.py`（挂起/确认/去重/复用已有的人）、`test_collect_api.py`（HTTP 契约）、
`test_collect_offwindows.py`（非 Windows 上整层必须能被导入）。

### 3.3 记忆层 `app/memory/` + `app/store.py`

记忆分三种粒度，这是本项目的关键设计：

| 粒度 | 存哪 | 用途 |
|---|---|---|
| **原始消息** | `messages` 表 | 精确回溯、展示聊天流 |
| **语义片段** | `embeddings` 表（向量存 BLOB） | 「她以前提过喜欢什么」这类模糊检索 |
| **结构化事实** | `facts` 表 | 「生日=3月14日」「讨厌=被叫宝贝」直接进 Prompt，不必每次检索 |
| **阶段摘要** | `summaries` 表 | 把几十万字压缩成时间线，喂给模型当长期记忆 |
| **人物画像** | `personas` 表 | 目标、风格、雷区、当前关系阶段，人工可编辑 |

**检索器 `retriever.py`** 用混合策略：
- 向量余弦相似度（语义召回）
- 关键词命中加权（精确名次/专有名词）
- 时间衰减（越近的聊天权重越高）
- 最终 `score = w1*cos + w2*keyword + w3*recency`

**画像 `profiler.py`** 两段式：
1. **事实抽取** —— 分批把消息喂给模型，要求输出 `{key, value, confidence, evidence}` 三元组，写进 `facts`；
2. **画像综合** —— 读取全部 facts + 抽样对话，生成 `peer_profile` / `taboos` / `stage`，写进 `personas`。

### 3.4 模型层 `app/llm/`

```python
class ChatProvider(ABC):
    name: str
    async def chat_raw(self, messages: list[dict], **kw) -> str: ...
    async def complete(self, system: str, user: str, **kw) -> str: ...
    async def complete_json(self, system, user, schema_hint="") -> dict: ...
```

`complete_json()` 是引擎唯一依赖的方法。它做了**容错解析**：
剥掉 ```json 围栏 → 找第一个 `{` 到最后一个 `}` → `json.loads` → 失败则尝试补全括号 → 仍失败抛 `LLMFormatError`，由引擎决定重试还是降级。

| 实现 | 适用 |
|---|---|
| `openai_compat.py` | 任何 OpenAI 兼容端点：DeepSeek / 通义 / Kimi / 硅基流动 / OpenAI / 各类中转 |
| `ollama.py` | 本地 Ollama，`format: json` 模式 |
| `mock.py` | **无 Key 也能跑通全流程**，基于关键词的规则化输出 |

### 3.5 通话转写（规划中）

**这个能力目前没有提供。** 语音识别 / 通话实时转写已从产品中整体撤下，改为「规划中」：
`app/asr/` 模块、`/api/voice/*` 与 `/api/asr/*` 接口、`voice_log` 表、
`requirements-asr.txt`、`scripts/asr_bench.py` 都已删除；前端「通话」页现在只是一个
不含任何按钮的说明页。下面记录的是撤下时已经想清楚的思路，将来重做按这个方向走。

1. **双通道采集，谁说的要分清。** 一路抓系统回环（WASAPI loopback）当「对方」，
   一路抓默认麦克风当「我」；两条流独立分帧、独立送识别。说话人在源头就分开，
   而不是事后靠声纹去切 —— 切错一次，整段记录都不可信。
2. **本机转写，音频不出机器。** 用 faster-whisper 在本地推理，模型权重存数据目录，可离线。
   代价是首次要下 80–500MB 的模型、CPU 比显卡慢，但换来的是「通话音频一帧都不上传」。
3. **按「话轮」断句，而不是按静音断。** VAD 先粗切，再用小模型判断句尾是否完整，
   不完整就继续等下一帧；宁可多等几百毫秒，也不把一句话切坏。
4. **落库时和文字记录同构。** 转写结果按 `(时间, 说话人, 文本)` 写成普通消息，
   只多带一个 `source=call` 标记；记忆、检索、分析都不用为通话写第二套逻辑。
5. **开录前把话说在前面。** 录音涉及法律边界，开始采集前必须明确确认，
   只用于你本人参与的通话、只存本机；这条不做成可关闭的选项。

撤下它的直接原因是这几个问题没解决：端到端延迟、双方串音、以及纯能量 VAD 在噪音下断句太碎。
重做时要一并解决，才值得放出来。

### 3.6 引擎层 `app/engine/`

四步流水线，详见 **[ENGINE_DESIGN.md](ENGINE_DESIGN.md)**：

```
新消息 ──▶ analyzer ──▶ planner ──▶ suggestor ──▶ simulator ──▶ 前端
           理解对方     定策略      出选项        推演走向
```

| 文件 | 输入 | 输出 |
|---|---|---|
| `prompts.py` | — | 所有中文提示词模板（集中管理） |
| `analyzer.py` | 新消息 + 检索到的上下文 + 画像 | `PeerAnalysis` |
| `planner.py` | 画像 + 近期对话 | 关系阶段 + 本轮策略与目标 |
| `suggestor.py` | 分析 + 策略 + 上下文 | `list[ReplyOption]`（带打分） |
| `simulator.py` | 单个选项 + 画像 | `SimBranch`（多轮推演树） |

---

## 4. 数据模型

```sql
-- 「人」是记忆的真正主键，chat 只是这个人在某个渠道上的一段记录。
persons(id PK, name, aliases,           -- aliases: JSON 数组，跨平台认人的依据
        relation, desired_relation, stage_goal, notes,
        created_at, updated_at)

chats(id PK, platform, name, peer_name, me_name, created_at,
      person_id,                        -- 归到哪个人名下（可空，未归并时）
      channel,                          -- 渠道标记：qq / wechat / ...
      source)                           -- import（手工导入）| collect（采集写入）

messages(id PK AUTOINCREMENT,
         chat_id FK, platform, sender, role,   -- role: me | peer | system
         ts, msg_type, text, ext_id,
         ts_source,                     -- exact | assumed | manual（时间从哪来）
         UNIQUE(chat_id, sender, ts, text))     -- 幂等去重

embeddings(message_id PK FK, model, dim, vec BLOB)   -- float32 紧凑存储

facts(id PK, chat_id FK, subject, key, value, confidence, evidence, updated_at)
         -- subject: peer | me | relationship

summaries(id PK, chat_id FK, kind, period, content, created_at)
         -- kind: daily | weekly | milestone

personas(chat_id PK FK, goal, my_style, peer_profile, taboos, stage, updated_at)

kv(key PK, value)      -- 设置项等零散配置

-- 采集游标：增量采集的唯一依据
collect_cursors(id PK, platform, account, peer_key, person_id, chat_id,
                last_ts, last_ext_id, fingerprint, merged_count,
                collected_from, last_run_at, status, message,
                UNIQUE(platform, account, peer_key))
```

**为什么 `ts_source` 要单独存一列**：采集（尤其是从剪贴板采集）经常拿不到消息的原始时间，
只能用抓取时刻顶替。顶替不是问题，**顶替了不说才是问题** —— 混进真时间轴，
「上周聊了什么」这类结论就建立在假时间上了。所以它是一列数据，不是靠「时间看起来对不对」去猜。

**为什么游标不只看时间**：同一个时间点可能有多条同秒消息，平台也可能改历史消息。
只比时间会漏、只比条数会错位；所以 `(最后一条的 ts, 文本哈希) + 已采条数` 两项一起比。

**为什么向量存 BLOB 而不是用 sqlite-vec**：脚手架阶段追求零编译依赖。
几万条消息在内存里做 numpy 余弦相似度只要几毫秒，完全够用。
上万条以上再换 `sqlite-vec` 或 `faiss`，接口不变（`retriever.py` 里预留了 `VectorIndex` 抽象）。

**表结构变更是迁移，不是重建**：`chats.person_id/channel/source` 与 `messages.ts_source`
都是后加的列，走 `ALTER TABLE ADD COLUMN` + 回填（老库的行补成 `exact`）。
`scripts/verify_migration.py` 在**真实库的副本**上验证「消息不丢 / 无悬空引用 /
init 幂等 / 新会话立刻有归属」，避免升级一次丢一批数据。

---

## 5. 接口契约（HTTP）

**连接的生命周期也是契约的一部分。** 服务端允许一条连接空闲多久，必须明显长于
浏览器自己的复用窗口（`config.KEEP_ALIVE_S` = 600 秒）。uvicorn 的默认值是 5 秒，
而浏览器会把空闲连接留几分钟 —— 它不知道服务端已经收掉了，于是把下一个请求写进
一条死连接，拿到零字节响应。这一步的后果不对称：**GET 会被浏览器静默重试，
POST 不会**，后者直接把 `net::ERR_CONNECTION_CLOSED` 抛给 JS。
v0.1.2 里「点采集报英文 `Failed to fetch`」就是这么来的：
采集页的动作全是 POST，而查版本、拉列表全是 GET，所以只有采集看起来是坏的。
（前端还有一次针对「请求没到服务端」的重发作为兜底，见 `frontend/index.html` 的 `api()`。）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查，返回当前 provider/embedder 名称 |
| POST | `/api/import` | 上传文件导入，自动 sniff 适配器 |
| POST | `/api/import/preview` | 只解析不入库，返回前 N 条 + 适配器置信度 |
| GET | `/api/chats` | 会话列表及统计 |
| GET | `/api/chats/{id}/messages` | 消息分页 |
| POST | `/api/chats/{id}/profile/build` | 构建画像（事实抽取 + 综合） |
| GET/PUT | `/api/chats/{id}/persona` | 读写人物设定（目标/风格/雷区） |
| POST | `/api/chats/{id}/analyze` | 分析对方最新一条消息 |
| POST | `/api/chats/{id}/suggest` | 出候选回复（含打分） |
| POST | `/api/chats/{id}/simulate` | 对某条选项做多轮推演 |
| GET/PUT | `/api/settings` | 读写运行时设置（覆盖 .env） |
| POST | `/api/settings/test` | 测试模型连通性 |

以人为中心的归并（`routes_persons.py`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/persons` | 人员列表 / 新建 |
| GET | `/api/persons/{id}` | 人 + 名下各渠道会话明细 |
| DELETE | `/api/persons/{id}` | 删除（会话与消息按外键级联） |
| POST | `/api/persons/{id}/merge` | 把另一个人的会话并到这个人名下 |
| GET | `/api/persons/{id}/messages` | 跨渠道按时间取这个人的消息 |
| POST | `/api/persons/{id}/channels` | 把一个会话挂到这个人名下 |
| DELETE | `/api/persons/{id}/channels/{chat_id}` | 摘掉某个会话 |

采集（`routes_collect.py`）—— 17 条，分四组：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/collect/clients` | 探测本机装了哪些客户端，给**结论 + 下一步动作** |
| GET | `/api/collect/clients/{key}` | 单个客户端的详情（数据目录、库文件、账号） |
| GET | `/api/collect/matrix` | 支持矩阵（含「我实测过的版本」标记） |
| POST | `/api/collect/key/check` | 校验一个用户粘贴的密钥对不对 |
| POST | `/api/collect/key/scan` | 去进程内存里找密钥（有预算、有结论） |
| POST | `/api/collect/preview` | 只走到「能不能拿到密钥」就停，**不写库** |
| POST | `/api/collect/run` | 全自动采集一遍 |
| GET / DELETE | `/api/collect/cursors` | 查看 / 重置增量游标（重置会回报真删了几条） |
| POST | `/api/collect/inspect` | 打开一个已解密的库，报「哪些列像是时间/正文/说话人」 |
| POST | `/api/collect/semi/start` | 开启半自动监听（指定采谁） |
| POST | `/api/collect/semi/stop` | 停止监听 |
| GET | `/api/collect/semi/status` | 状态与计数 |
| GET | `/api/collect/semi/poll` | 轮询新增的抓取块（用 id 定位，不用下标） |
| POST | `/api/collect/semi/commit` | 确认写入（可改文本与时间，标 `manual`） |
| POST | `/api/collect/semi/discard` | 丢弃某个抓取块 |
| POST | `/api/collect/semi/clear` | 清空**待确认**列表（不动已落库的） |

> `/api/collect/*` 全部挂在 `/api/collect` 前缀下，只有这一处注册。
> 曾经 `routes_persons.py` 里也有一份 `/api/collect/cursors`，
> 而 FastAPI 里**先注册的会静默屏蔽后一个** —— 路径相同、注解不同、表现随注册顺序变，
> 是最难查的一类 bug。现在归属明确：采集的接口全在 `routes_collect.py`。

---

## 6. 一次完整请求的数据流

以「对方发来一条消息，我要建议」为例：

```
1. 前端 POST /api/chats/{id}/suggest  { "peer_message": "哈哈哈今天好累啊" }

2. 存入 messages 表（role=peer），并向量化写入 embeddings

3. retriever.search("今天好累 哈哈哈", chat_id, top_k=30)
   └─ 混合打分：语义 0.6 + 关键词 0.25 + 时间衰减 0.15

4. 载入 personas（目标、风格、雷区、关系阶段）

5. analyzer.complete_json(ANALYZE_SYSTEM, 拼装好的上下文)
   └─ 得到 PeerAnalysis

6. planner.complete_json(PLAN_SYSTEM, ...)  → 本轮策略

7. suggestor.complete_json(SUGGEST_SYSTEM, ...) → 4 条候选

8. 本地打分（不依赖模型，防止模型自评失真）：
   naturalness / warmth / progress / risk 四个维度加权

9. 返回 SuggestionBundle 给前端渲染

10. 用户点某条 → POST /simulate → simulator 做 3 轮分支推演
```

**设计要点**：第 8 步是**本地评分**而不是让模型打分。
模型给自己写的回复打分普遍偏高且方差极小，没有区分度。
本地用可解释的启发式（长度合理性、是否包含问句、是否复读对方词、是否踩雷区词表、是否推进目标）
反而更稳定，而且用户能看懂分数是怎么来的。

---

## 7. 关键技术选型与理由

| 选择 | 理由 | 替代方案 |
|---|---|---|
| Python + FastAPI | 异步流式、生态好、后续接 ML 顺畅 | Node / Go（生态差） |
| SQLite 单文件 | 零部署、可整包加密、隐私可控 | Postgres（过重） |
| 向量存 BLOB + numpy | 零编译依赖，脚手架够用 | sqlite-vec / faiss / chroma |
| 单文件 HTML 前端 | 零构建、双击即用、方便你读懂全貌 | Vite+React（见 ROADMAP） |

---

## 8. 已知难点与对策

| 难点 | 现状 | 对策 |
|---|---|---|
| 微信 / QQ 本地库加密 | 已实现 SQLCipher 4 解密，但**密钥拿不到** —— 本机实测自动搜内存未命中 | 密钥手动粘贴可走通；同时把剪贴板半自动做成并列通道，不依赖密钥 |
| 客户端升级改列名 | 认列按数据认，仍可能在全新版本上认不出 | 不写死映射，认不出就报候选列 + 允许 `schema_overrides` 手工指定并记住 |
| 剪贴板拿不到原始时间 | 半自动模式常见 | 用抓取时刻顶替但标 `ts_source='assumed'` 单独存；也可选 `ask` 模式先挂起 |
| 模型编造反事实 | 画像里混入模型幻觉 | 事实带 `evidence` 指向 message_id，前端可点开核对 |
| 上下文超长 | 几万条消息塞不进窗口 | 分层记忆：摘要层 + 事实层 + 检索层 |

---

## 9. 从脚手架继续往下走

读 [ROADMAP.md](ROADMAP.md)。里面按「能跑通 → 能用 → 好用」三阶段列了每一件要做的事，
并标注了对应要改的文件。
