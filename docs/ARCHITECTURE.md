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

---

## 2. 分层结构

```
┌─────────────────────────────────────────────────────────────────┐
│  表现层   frontend/index.html （单文件控制台，零构建）             │
│           导入区 · 记忆区 · 指挥台 · 通话区（规划中）              │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────────┐
│  API 层   app/api/                                               │
│  routes_data.py  导入/列表/消息/画像                              │
│  routes_engine.py 分析/建议/推演/策略                             │
│  routes_admin.py  设置读写/健康检查/Provider 探测                 │
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
│  chats · messages · embeddings · facts · summaries · personas    │
└─────────────────────────────────────────────────────────────────┘
           ▲
┌──────────┴──────────────────────────────────────────────────────┐
│  接入层   app/adapters/                                          │
│  base(抽象) · qq · wechat · generic · registry(自动选择适配器)     │
└─────────────────────────────────────────────────────────────────┘
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

### 3.2 记忆层 `app/memory/` + `app/store.py`

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

### 3.3 模型层 `app/llm/`

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

### 3.4 通话转写（规划中）

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

### 3.5 引擎层 `app/engine/`

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
chats(id PK, platform, name, peer_name, me_name, created_at)

messages(id PK AUTOINCREMENT,
         chat_id FK, platform, sender, role,   -- role: me | peer | system
         ts, msg_type, text, ext_id,
         UNIQUE(chat_id, sender, ts, text))     -- 幂等去重

embeddings(message_id PK FK, model, dim, vec BLOB)   -- float32 紧凑存储

facts(id PK, chat_id FK, subject, key, value, confidence, evidence, updated_at)
         -- subject: peer | me | relationship

summaries(id PK, chat_id FK, kind, period, content, created_at)
         -- kind: daily | weekly | milestone

personas(chat_id PK FK, goal, my_style, peer_profile, taboos, stage, updated_at)

kv(key PK, value)      -- 设置项等零散配置
```

**为什么向量存 BLOB 而不是用 sqlite-vec**：脚手架阶段追求零编译依赖。
几万条消息在内存里做 numpy 余弦相似度只要几毫秒，完全够用。
上万条以上再换 `sqlite-vec` 或 `faiss`，接口不变（`retriever.py` 里预留了 `VectorIndex` 抽象）。

---

## 5. 接口契约（HTTP）

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
| 微信 PC 端数据库加密 | 只做文件导入，不碰本地加密库 | 见 ROADMAP「可选进阶」；合规风险自负 |
| 模型编造反事实 | 画像里混入模型幻觉 | 事实带 `evidence` 指向 message_id，前端可点开核对 |
| 上下文超长 | 几万条消息塞不进窗口 | 分层记忆：摘要层 + 事实层 + 检索层 |

---

## 9. 从脚手架继续往下走

读 [ROADMAP.md](ROADMAP.md)。里面按「能跑通 → 能用 → 好用」三阶段列了每一件要做的事，
并标注了对应要改的文件。
