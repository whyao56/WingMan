# WingMan · 聊天僚机

[![CI](https://github.com/whyao56/wingman/actions/workflows/ci.yml/badge.svg)](https://github.com/whyao56/wingman/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue.svg)](https://www.python.org/)

> 把聊天记录交给大模型记忆，通话时实时听懂对方，然后告诉你**该怎么回**。

WingMan 是一个本地优先的「对话参谋」系统。它做三件事：

1. **记忆** —— 导入你与某个人的聊天记录（QQ / 微信 / 通用文件），构建长期记忆与人物画像；
2. **听懂** —— 通话时双通道采集（系统回环听对方 + 麦克风听自己），实时转写成文字；
3. **参谋** —— 收到新消息后，分析对方情绪与意图，给出多个可选回复，并**推演每条回复会把聊天带向哪里**。

> **它只给建议，不代你发消息。** 不做无人值守的自动回复 —— 原因见 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

---

## 快速开始（3 步）

需要 Windows 10/11 与 **Python 3.11+**（安装时勾选 *Add python.exe to PATH*）。不需要 Node，不需要 API Key。

```bat
REM 第 1 步：下载并解压（也可以 git clone）
REM        https://github.com/whyao56/WingMan/archive/refs/heads/main.zip

REM 第 2 步：在仓库根目录启动（或直接双击 wingman.cmd）
wingman.cmd

REM 第 3 步：浏览器打开控制台后 →「导入」上传 samples\qq_sample_小鹿.txt
REM        →「指挥台」点「取会话里最后一条对方消息」→「分析并给建议」
```

PowerShell 里第 2 步要写成 `.\wingman.cmd`（否则提示找不到命令）。
首次运行会自动创建 Python 环境、安装依赖（几分钟，看网速），然后启动服务并自动打开 <http://127.0.0.1:8787>。

**不配模型也能跑通全流程** —— 未配置时会用内置的 Mock（规则引擎）。
但请注意：**Mock 的输出只是「流程演示」，不是真实智能**。接入真实模型见 **[docs/MODELS.md](docs/MODELS.md)**。

不想一步步来？完整版见 **[docs/QUICKSTART.md](docs/QUICKSTART.md)**；卡住了见 **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**。

### 启动器参数

| 命令 | 作用 |
|---|---|
| `wingman.cmd` | 准备环境（首次）+ 启动 + 打开浏览器 |
| `wingman.cmd --port 8899` | 换端口（默认 8787） |
| `wingman.cmd --no-browser` | 不自动打开浏览器 |
| `wingman.cmd --setup-only` | 只准备环境，不启动服务 |
| `wingman.cmd --doctor` | 只做启动前自检，逐项打印结果与修复建议 |
| `wingman.cmd --with-asr` | 额外安装语音依赖（可选，失败不影响主服务） |
| `wingman.cmd --help` | 用法 |

---

## 版本与状态

- **当前版本：v0.1.0**（版本号定义在 `backend/app/__init__.py`，变更记录见 [CHANGELOG.md](CHANGELOG.md)）
- **状态：脚手架可用（阶段 0「能跑通」已完成）**；阶段 1「能用」与阶段 2「好用」尚未实现，详见 [docs/ROADMAP.md](docs/ROADMAP.md)。

| | 说明 |
|---|---|
| ✅ 现在就能用 | 导入（QQ / 微信 / 通用 JSON、CSV）→ 向量索引 → 事实与画像 → 分析 → 建议 → 推演 的完整链路；零 Key 用 Mock 跑通；OpenAI 兼容云服务与 Ollama 可切换；单文件控制台；实时字幕的 SSE 接口 |
| ⚠️ 未做端到端验证 | 真实通话采集、云端 / 本地 ASR 的实际效果（需要你在自己设备上试；采集必须手动点「开始采集」） |
| ⬜ 尚未实现 | 流式 ASR、AEC 回声消除、事实人工校对 UI、前端工程化、自动回复（明确不做，见 COMPLIANCE） |
| 验证过的环境 | 中文 Windows + Python 3.11；CI 覆盖 Python 3.11 / 3.13（仅冒烟测试） |

---

## 它长什么样

你选中和某个人的全部聊天记录 → 告诉它你的目标（比如"想约她周末看展"）→
之后对方每发一条消息，你点一下「分析」，它就给你：

```
┌─ 对方状态分析 ────────────────────────────────┐
│ 情绪：轻松愉悦 (7/10)   兴趣度变化：+1          │
│ 意图：延续话题，等你接话                         │
│ 潜台词：「今天心情不错，希望你也是」              │
│ 观测信号：① 用了 2 个表情 ② 主动提问 ③ 回复变快   │
│ 风险点：她提了"周末"，但没说有没有空 —— 别硬推     │
└───────────────────────────────────────────────┘

┌─ 建议回复（4 选 1）─────────── 综合分 ── 走向预测 ─┐
│ A 调侃式接梗   "那我是不是该收门票了"   8.6  升温 ↗ │
│ B 真诚共情     "听着就好累，抱抱"       7.9  持平 → │
│ C 顺势推进邀约 "那周日带你去放松下？"    7.2  升温 ↗ │
│ D 保守回应     "嗯嗯"                  4.1  降温 ↘ │
└───────────────────────────────────────────────┘
        ↓ 点任意一条，展开 3 轮走向推演树
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 3 步上手：下载 → 启动 → 看到第一条建议 |
| [docs/MODELS.md](docs/MODELS.md) | 接真实模型：OpenAI 兼容云 API / Ollama、怎么确认接上了、密钥与隐私边界 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 排错手册：退出码含义、端口占用、Python 版本、依赖装不上、中文乱码、无音频设备…… |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、模块职责、数据模型、接口契约 |
| [docs/ENGINE_DESIGN.md](docs/ENGINE_DESIGN.md) | 参谋引擎的算法与 Prompt 设计（这是项目的灵魂） |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 迭代路线：从脚手架到能用、好用 |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | 数据合规、隐私边界、使用红线 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

---

## 架构总览

```
                    ┌──────────────────────────────────────────┐
   聊天记录文件  ──▶ │  Adapter 层（插件式）                     │
   QQ / 微信 / JSON  │  qq.py · wechat.py · generic.py          │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │  Memory 层                                │
                    │  SQLite 存储 · Embedder · 混合检索 · 画像  │
                    └────────────────┬─────────────────────────┘
                                     ▼
   通话音频 ──▶ ASR 层 ──▶ ┌──────────────────────────────────────┐
   回环/麦克风  转写文本    │  Engine 层（核心）                    │
                          │  Analyzer → Suggestor → Simulator    │
                          └────────────────┬─────────────────────┘
                                           ▼
                    ┌──────────────────────────────────────────┐
                    │  LLM 层（可切换）                          │
                    │  OpenAI 兼容 · Ollama · Mock             │
                    └──────────────────────────────────────────┘
```

详细的模块划分、数据流、接口契约见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

---

## 目录结构

```
WingMan/
├── wingman.cmd                  # 唯一用户入口：一键启动 / 装环境 / 自检
├── backend/
│   ├── app/
│   │   ├── adapters/            # 聊天记录接入插件（QQ / 微信 / 通用）
│   │   ├── memory/              # 存储、向量化、检索、人物画像
│   │   ├── llm/                 # 大模型接入（云 / 本地 / Mock）
│   │   ├── asr/                 # 语音识别 + 双通道音频采集
│   │   ├── engine/              # 分析 → 建议 → 推演 核心引擎
│   │   └── api/                 # HTTP 接口
│   ├── tests/                   # 冒烟测试（Mock，无需 Key）
│   ├── requirements.txt         # 依赖下限声明（人类可读）
│   ├── requirements.lock.txt    # 精确版本锁定（启动器安装的就是它）
│   └── data/                    # 运行时数据（已 gitignore）
├── frontend/index.html          # 单文件控制台，零构建
├── samples/                     # 虚构的示例聊天记录，可直接导入试跑
├── scripts/                     # 启动 / 自检 / 端到端验证脚本
└── docs/
```

### 开发者：手动跑测试

```bat
REM 准备环境（不启动服务；环境建在 backend\.venv）
wingman.cmd --setup-only

REM 内核链路冒烟测试（Mock，无需任何 Key）
backend\.venv\Scripts\python.exe backend\tests\test_smoke.py

REM 端到端 HTTP 检查（需要服务已经在运行）
backend\.venv\Scripts\python.exe scripts\e2e_check.py
```

> `e2e_check.py` 会**往当前数据库里写数据**（导入示例会话、写入设定、注入一段语音）。
> 想保持数据干净，就另解压一份仓库、或者先把 `backend\data\wingman.db` 备份出来再跑。

---

## 常见问题

> 这几条是最常遇到的；完整的排错手册在 **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**。

**端口 8787 被占用**
换端口启动：`wingman.cmd --port 8899`。查是谁占着：`netstat -ano | findstr :8787`。

**跑 `scripts/e2e_check.py` 时报 404**
环境里设了 `HTTP_PROXY` / `http_proxy` 时，httpx 默认把请求发给代理，
代理用「绝对地址」形式转发，本地 uvicorn 收到 `http%3A//127.0.0.1%3A8787/api/...`
这种畸形路径就 404 了。脚本里已经用 `trust_env=False` 绕开，
如果你自己写调用脚本，记得同样处理。

**中文名的示例文件上传失败**
某些 HTTP 客户端在 multipart 的 filename 里处理非 ASCII 有问题。
把文件复制一份改成英文名再上传即可，内容不受影响。

**「通话」页说没有检测到音频设备**
`wingman.cmd --with-asr` 装采集依赖（`soundcard`）。
如果装了还是不行，检查系统默认播放设备是否为当前实际在用的那个 ——
回环设备是跟着「默认扬声器」走的。没有麦克风也可以用「通话」页的手动注入演示链路。

**Mock 模式下分析结果是废话**
这是预期的。Mock 只是一套规则，用来验证流程能不能跑通。
接上真实模型（设置页填 base_url / api_key / model）之后才会有真正的理解能力。
判断标准见 [ROADMAP](docs/ROADMAP.md) 最后一节。

---

## 踩坑记录

这些是开发中真实踩到、且值得写下来的坑。共同点是**它们都不报错** ——
只是安静地给你错误的结果，所以很难自己发现。

### 1. 把一对多关系建模成了字典

`facts` 表最初的唯一键是 `(chat_id, subject, key)`，意思是「同一个人同一个 key 只能有一个值」。
配合 `INSERT OR REPLACE`：

```sql
-- 抽到「我超爱吃火锅的」→ 写入
INSERT OR REPLACE INTO facts (chat_id, subject, key, value)
  VALUES ('c1', 'peer', '喜欢', '吃火锅');
-- 之后抽到「我超喜欢猫的」→ 把上面那行**替换掉**
INSERT OR REPLACE INTO facts (chat_id, subject, key, value)
  VALUES ('c1', 'peer', '喜欢', '猫');
```

结果：「她喜欢猫」永久消失，画像里只剩「喜欢火锅」，**全程没有任何报错**。

「喜欢」在现实里是一对多关系（猫 / 火锅 / 陶艺），不是字典键值。
唯一键必须带上 `value`：

```sql
UNIQUE (chat_id, subject, key, value)
```

改约束需要重建表（SQLite 不支持 `ALTER` 约束）。
`Store._migrate_facts_unique()` 读 `sqlite_master` 里的原始 DDL 判断版本并自动迁移，
老库数据不丢、重复执行幂等。

### 2. 「本地全绿」不等于「CI 绿」，但原因常常不是平台

这个 bug 是 CI 第一次运行抓出来的，本地一直是绿的。第一反应是「Linux 和 Windows 有差异」，
实际排查下来**与平台无关**：改完 `mock.py` 后我只跑了 HTTP 端到端脚本 `e2e_check.py`
（它不检查事实抽取），**没有重跑 `tests/test_smoke.py`**，
于是本地测试结果还停留在改坏之前的状态。

有效的定位手段是让本地条件对齐：用不同的 `PYTHONHASHSEED` 连跑三次。
三次结果完全一致，就可以排除哈希随机化，问题必然出在确定的代码路径上。

> 教训：改完被多处复用的代码，要把**所有**测试跑一遍。
> CI 最大的价值不是「更严格」，而是**它不会忘记跑**。

### 3. 单层测试通过 ≠ 集成正确

`store` 的唯一约束、`mock` 的事实压缩、`profiler` 的分批抽取，
**每一层单独看行为都是「合理」的**，叠在一起却会丢数据。
上面这个 bug 最终在三个文件里分别修：schema 约束、分组保留多条、长度过滤改白名单。

---

## 免责声明

本项目仅供**个人**在**本人设备**上、对**本人参与过**的对话做备忘与复盘之用。
请勿用于未经同意的他人数据采集、监控或任何违法用途。
使用前请阅读 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

## License

MIT
