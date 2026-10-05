# WingMan · 聊天僚机

[![CI](https://github.com/whyao56/wingman/actions/workflows/ci.yml/badge.svg)](https://github.com/whyao56/wingman/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue.svg)](https://www.python.org/)

> 把聊天记录交给大模型记忆，通话时实时听懂对方，然后告诉你**该怎么回**。

WingMan 是一个本地优先的「对话参谋」系统。它做三件事：

1. **记忆** —— 导入你与某个人的聊天记录（QQ / 微信 / 通用文件），构建长期记忆与人物画像；
2. **听懂** —— 通话时双通道采集（系统回环听对方 + 麦克风听自己），实时转写成文字；
3. **参谋** —— 收到新消息后，分析对方情绪与意图，给出多个可选回复，并**推演每条回复会把聊天带向哪里**。

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

## 快速开始

### 方式一：直接用（Windows，不需要装 Python）

下载对应的文件夹，解压到一个**可写**的位置（桌面、D 盘都行），双击 `WingMan.exe`。
程序会自己起服务并弹出窗口，关掉窗口即退出。

| 版本 | 体积 | 语音怎么办 |
|---|---|---|
| **标准版** | 78 MB | 走云 ASR：填个接口地址就能用 |
| **完整版** | 246 MB | 云端/本地都行；本地转写另需下载模型权重（音频一帧都不出本机） |

**你的数据在** `%LOCALAPPDATA%\WingMan\` —— 聊天记录、画像、设置、日志都在那。

第一次打开会提示「还有 N 项没配好」，这是正常的：默认用的是演示引擎、
还没导入聊天记录、语音还没接。照着**「自检」页**逐条点，每配好一项就会变绿。

> 语音不工作时，点「自检 → 语音链路实测」录一段看看。
> 它会把设备、电平、引擎、识别文本逐步摊开 ——「没反应」会被拆成「第几步不行」。
> 命令行等价物：`WingMan.exe --asr-test`（`--source loopback` 可测「听对方」那条路）。

### 方式二：从源码跑（任意平台，Python 3.11+）

```bash
cd wingman/backend
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

python -m uvicorn app.main:app --reload --port 8787
# 打开 http://127.0.0.1:8787
```

要用语音再加：`pip install -r requirements-asr.txt`。

**不需要任何 API Key 也能跑通全流程** —— 未配置模型时会自动使用内置的 Mock Provider，
走完「导入 → 画像 → 分析 → 建议 → 推演」整条链路，方便你先看懂它怎么工作。

想接真模型时，在控制台「设置」里填 `base_url` / `api_key` / `model` 即可，
支持一切 **OpenAI 兼容协议**的云端服务（DeepSeek、通义、Kimi、硅基流动、OpenAI…），
也支持 **Ollama** 本地模型（数据不出本机）。

### 打包成 exe（开发者）

```bash
python scripts/build_exe.py              # 标准版
python scripts/build_exe.py --with-asr   # 完整版（含本地语音识别）
python scripts/build_exe.py --zip        # 顺手压成 zip
```

产物在 `dist/WingMan/`，**分发时要给整个文件夹**（`_internal/` 是运行时，缺了跑不起来）。
细节、两个版本的差别、以及打包踩过的 10 个坑见 **[docs/DESKTOP.md](docs/DESKTOP.md)**。

---

## 选哪个语音模型？跑一遍就有数

```bash
python scripts/asr_bench.py                      # 对比 tiny / small
python scripts/asr_bench.py --models tiny,base,small
```

实测（同一句中文，5.3 秒音频，CPU）：

| 规格 | 置信度 | 稳态耗时 | 实时倍率 | 识别结果 |
|---|---|---|---|---|
| tiny | 0.62 | 0.25 s | 21x | 今天加班到10点好累呀你周末有空吗 |
| **small** | **0.78** | 1.36 s | 3.9x | 今天加班到10点,好累呀,你周末有空吗? |

`small` 比 `tiny` 准一档、带标点，代价是慢约 5 倍 —— 但仍是 3.9 倍实时，
CPU 完全够用。所以**推荐起步选 `small`**，只想先试通流程再选 `tiny`。

这个脚本同时也是一份**量纲回归测试**：同一个音频用三种形式（已归一化 float32 /
未归一化 float32 / 原始 int16）喂进去，结果必须逐字一致。不一致就说明
`to_float_mono()` 的兜底失效了 —— 而那种情况的症状是「识别出乱码但不报错」，
见 [docs/DESKTOP.md 第 9 节](docs/DESKTOP.md)。

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

## 文档

| 文档 | 内容 |
|---|---|
| [docs/DESKTOP.md](docs/DESKTOP.md) | **打包成桌面程序**：怎么用、怎么构建、两个版本的区别、踩过的 10 个坑 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、模块职责、数据模型、接口契约 |
| [docs/ENGINE_DESIGN.md](docs/ENGINE_DESIGN.md) | 参谋引擎的算法与 Prompt 设计（这是项目的灵魂） |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 迭代路线：从脚手架到能用、好用 |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | 数据合规、隐私边界、使用红线 |

---

## 目录结构

```
wingman/
├── backend/
│   ├── app/
│   │   ├── adapters/     # 聊天记录接入插件（QQ / 微信 / 通用）
│   │   ├── memory/       # 存储、向量化、检索、人物画像
│   │   ├── llm/          # 大模型接入（云 / 本地 / Mock）
│   │   ├── asr/          # 语音识别 + 双通道音频采集 + 模型管理
│   │   ├── engine/       # 分析 → 建议 → 推演 核心引擎
│   │   ├── api/          # HTTP 接口
│   │   ├── desktop.py    # 桌面启动器（exe 入口，含 --check / --asr-test）
│   │   └── selfcheck.py  # 启动自检：缺什么、怎么补
│   ├── run_wingman.py    # PyInstaller 打包入口
│   ├── tests/
│   └── data/             # 运行时数据（已 gitignore）
├── frontend/index.html   # 单文件控制台，零构建
├── build/wingman.spec    # 打包配置
├── samples/              # 示例聊天记录，可直接导入试跑
├── scripts/
│   ├── build_exe.py      # 一键打包
│   ├── asr_bench.py      # 语音模型对比 + 量纲回归
│   └── e2e_check.py      # HTTP 端到端自检
└── docs/
```

---

## 常见问题

**跑 `scripts/e2e_check.py` 时报 404**
环境里设了 `HTTP_PROXY` / `http_proxy` 时，httpx 默认把请求发给代理，
代理用「绝对地址」形式转发，本地 uvicorn 收到 `http%3A//127.0.0.1%3A8787/api/...`
这种畸形路径就 404 了。脚本里已经用 `trust_env=False` 绕开，
如果你自己写调用脚本，记得同样处理。

**中文名的示例文件上传失败**
某些 HTTP 客户端在 multipart 的 filename 里处理非 ASCII 有问题。
把文件复制一份改成英文名再上传即可，内容不受影响。

**「通话」页说没有检测到音频设备**
`pip install -r requirements-asr.txt` 装 `soundcard`。
如果装了还是不行，检查系统默认播放设备是否为当前实际在用的那个 ——
回环设备是跟着「默认扬声器」走的。

> 打包版已经内置 `soundcard`，不用再装。

**双击 exe 没反应 / 提示「Windows 已保护你的电脑」**

后者不是 bug，是 Windows 对未签名程序的默认行为：点「更多信息」→「仍要运行」。

前者请按顺序做两件事：
1. 打开 `%LOCALAPPDATA%\WingMan\logs\wingman.log`，最后一段就是出错原因
2. 命令行执行 `WingMan.exe --check`，它会生成一份人话报告并写到
   `logs\selfcheck.txt`，可以直接发给别人看

**语音识别出来是乱码 / 什么都不出来**

先跑 `WingMan.exe --asr-test`（或界面「自检 → 语音链路实测」）。
它会指出是哪一步的问题。最常见的三种：

- **电平 RMS < 0.005** → 麦克风没收到声音。检查是否选错设备或者系统里被静音了。
  Windows 的「隐私和安全性 → 麦克风」也要允许桌面应用访问。
- **引擎显示 mock** → 你还没真的接上语音，这时输出是占位文本，不是识别不准。
- **没下模型** → 到「设置 → 语音」下载，国内网络记得勾「使用国内镜像」。

**下载模型报 401 Unauthorized / CAS Client Error**

这是 HuggingFace 的 Xet 通道问题：主站域名走了国内镜像，但 Xet 的 CAS 服务器
（`cas-server.xethub.hf.co`）**镜像并不代理**，于是报 401，看起来像鉴权问题。

程序里已经强制关掉了 Xet（`HF_HUB_DISABLE_XET=1`）。如果还遇到，
勾上「使用国内镜像」重试一次，或者换小一号的模型。

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
