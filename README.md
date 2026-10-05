# WingMan · 聊天僚机

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue.svg)](https://www.python.org/)
[![CI](https://github.com/whyao56/WingMan/actions/workflows/ci.yml/badge.svg)](https://github.com/whyao56/WingMan/actions/workflows/ci.yml)

> 把聊天记录交给大模型记忆，读懂 Ta 是个什么样的人，然后告诉你**该怎么回**。

WingMan 是一个本地优先的「对话参谋」系统。它做两件事：

1. **记忆** —— 导入你与某个人的聊天记录（QQ / 微信 / 通用文件），构建长期记忆与人物画像；
2. **参谋** —— 收到新消息后，分析对方情绪与意图，给出多个可选回复，并**推演每条回复会把聊天带向哪里**。

> **它只给建议，不代你发消息。** 不做无人值守的自动回复 —— 原因见 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。
>
> **通话语音转写暂时撤下了**（原「听懂」那一环）。做得不够好，不拿半成品占位置；
> 思路完整保留在「通话」页与 [docs/ROADMAP.md](docs/ROADMAP.md)，标为「规划中」。

---

## 直接下载（不想装 Python 就走这条）

**[→ 前往下载页](https://github.com/whyao56/WingMan/releases/latest)** · 绿色免安装，解压后双击即可

| 下载 | 大小 | 说明 |
|---|---|---|
| **[`WingMan-0.2.1-win64.zip`](https://github.com/whyao56/WingMan/releases/download/v0.2.1/WingMan-0.2.1-win64.zip)** | 31 MB | 唯一的包。聊天记录分析、人物画像、回复建议都在里面 |

早期版本曾分成「标准版 / 完整版」两个包，区别只在**是否内置本地语音识别**。
语音撤下后这个区别消失了，现在只有一个包。

> ⚠️ **解压后双击 `WingMan.exe`，不要只把 exe 单独拖出来。** 旁边的 `_internal`
> 文件夹是程序本体的一部分，少一个文件都起不来。要挪位置就整个文件夹一起挪。
>
> 双击没反应时，命令行跑 `WingMan.exe --check`，它会直接告诉你缺什么。
> 数据存在 `%LOCALAPPDATA%\WingMan\`，**换位置解压程序数据不会丢**。

想改代码、或者用 macOS / Linux，走下面的**源码路线**。

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

不想一步步来？详细步骤见 **[docs/QUICKSTART.md](docs/QUICKSTART.md)**；卡住了见 **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**。

> 上面这条是**源码路线**。如果你不想在这台机器上装 Python，往下看
> [「三种跑法：怎么选」](#三种跑法怎么选) —— 打包好的 `WingMan.exe` 连 Python 都不用装。

### 启动器参数

| 命令 | 作用 |
|---|---|
| `wingman.cmd` | 准备环境（首次）+ 启动 + 打开浏览器 |
| `wingman.cmd --port 8899` | 换端口（默认 8787） |
| `wingman.cmd --no-browser` | 不自动打开浏览器 |
| `wingman.cmd --setup-only` | 只准备环境，不启动服务 |
| `wingman.cmd --doctor` | 只做启动前自检，逐项打印结果与修复建议 |
| `wingman.cmd --help` | 用法 |

---

## 三种跑法：怎么选

有三种跑法。**分水岭只有一个：这台机器愿不愿意装 Python。**

| | A. 一键启动器 | B. 打包版 | C. 手动源码 |
|---|---|---|---|
| 入口 | `wingman.cmd` | `WingMan.exe` | `uvicorn` |
| 需要 Python | ✅ 3.11+ | ❌ 不用装 | ✅ 3.11+ |
| 首次启动 | 几分钟（自动建环境装依赖） | 秒级 | 几分钟 |
| 体积 | 仓库本身（几 MB） | 下载 31 MB | 仓库本身 |
| 改了代码 | 直接生效 | 要重新打包 | 直接生效 |
| 适合 | 想改代码 / 已装 Python | 只想用，或给不懂技术的朋友 | 非 Windows、要做开发 |

### A. 一键启动（Windows，推荐）

```bat
wingman.cmd
```

首次运行会自动：创建 Python 环境 → 按锁定版本装依赖 → 启动前自检 → 起服务 → 打开浏览器
（`http://127.0.0.1:8787`）。

详细步骤见 **[docs/QUICKSTART.md](docs/QUICKSTART.md)**，参数速查：

| 命令 | 作用 |
|---|---|
| `wingman.cmd` | 准备环境（首次）+ 启动 + 自动开浏览器 |
| `wingman.cmd --doctor` | 只做启动前自检，逐项打印结果与修复建议 |
| `wingman.cmd --setup-only` | 只准备环境，不启动 |
| `wingman.cmd --port 8899` | 换端口 |

> PowerShell 里要写 `.\wingman.cmd`。用 PowerShell 工具的会话请照此处理。

### B. 打包版（连 Python 都不用装）

从 **[下载页](https://github.com/whyao56/WingMan/releases/latest)** 拿到 zip（内容见上文
[「直接下载」](#直接下载不想装-python-就走这条)），解压到一个**可写**的位置（桌面、D 盘都行），
双击 `WingMan.exe`。程序会自己起服务并弹出原生窗口，关掉窗口即退出。

| 体积 | 解压后 | 里面有什么 |
|---|---|---|
| 31 MB | 约 68 MB | 全部功能：聊天记录分析、人物画像、回复建议与推演 |

> 早期的「完整版」（91 MB / 解压后 237 MB）只比标准版多一套本地语音识别。
> 语音撤下后它就没有存在理由了，两个包合并成一个 —— 下载页不再需要你纠结选哪个。

**你的数据在** `%LOCALAPPDATA%\WingMan\` —— 聊天记录、画像、设置、日志都在那。

第一次打开会提示「还有 N 项没配好」，这是正常的：默认用的是演示引擎、还没导入聊天记录。
照着**「自检」页**逐条点，每配好一项就会变绿。

怎么自己打个包，见 **[docs/DESKTOP.md](docs/DESKTOP.md)**。

### C. 手动从源码跑（任意平台）

```bash
cd wingman/backend
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt   # 或 requirements.lock.txt（精确版本）

python -m uvicorn app.main:app --reload --port 8787
# 打开 http://127.0.0.1:8787
```

**不需要任何 API Key 也能跑通全流程** —— 未配置模型时会自动使用内置的 Mock Provider，
走完「导入 → 画像 → 分析 → 建议 → 推演」整条链路，方便你先看懂它怎么工作。
想让建议真的有意义，去接真模型 —— 见 **[docs/MODELS.md](docs/MODELS.md)**。

想接真模型时，在控制台「设置」里填 `base_url` / `api_key` / `model` 即可，
支持一切 **OpenAI 兼容协议**的云端服务（DeepSeek、通义、Kimi、硅基流动、OpenAI…），
也支持 **Ollama** 本地模型（数据不出本机）。

---

## 排错从哪下手

**两条路的自检入口不一样，先找对那个：**

| 你用的是 | 命令 | 产物 |
|---|---|---|
| `wingman.cmd` | `wingman.cmd --doctor` | 终端逐项输出（退出码 2 = 拒绝启动） |
| `WingMan.exe` | `WingMan.exe --check` | `%LOCALAPPDATA%\WingMan\logs\selfcheck.txt` |
| 界面里 | 侧栏「自检」页 | 三档结论 + 每项一个「点哪里能修好」按钮 |

再往下看 **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**（排错手册）和
[docs/DESKTOP.md](docs/DESKTOP.md)（打包特有的坑）。

---

## 版本与状态

- **当前版本：v0.2.1**（版本号定义在 `backend/app/__init__.py`，变更记录见 [CHANGELOG.md](CHANGELOG.md)）
- **状态：有成品包可用了。** [Releases](https://github.com/whyao56/WingMan/releases) 提供免安装的
  Windows 桌面版（双击即用，不需要 Python）；源码路线同样可用。
  **最大的缺口是建议内容本身** —— 默认的 Mock 引擎让整条链路跑得通，但给的建议还是规则生成的，
  下一步是 Prompt 调优，详见 [docs/ROADMAP.md](docs/ROADMAP.md)。

| | 说明 |
|---|---|
| ✅ 现在就能用 | 导入（QQ / 微信 / 通用 JSON、CSV）→ 向量索引 → 事实与画像 → 分析 → 建议 → 推演 的完整链路；零 Key 用 Mock 跑通；OpenAI 兼容云服务与 Ollama 可切换；单文件控制台 |
| ⏸️ 已撤下（规划中） | **通话实时转写**。上一版试过云端转写，但延迟、双方串音、断句切碎都还不够好，而且要把通话音频送出本机 —— 对一个「本地优先」的工具来说代价不划算。已从界面与代码里整体移除，思路保留在「通话」页与 [docs/ROADMAP.md](docs/ROADMAP.md) |
| ⬜ 尚未实现 | 事实人工校对 UI、前端工程化、自动回复（明确不做，见 COMPLIANCE） |
| 验证过的环境 | 中文 Windows + Python 3.11（实测安装 + 端到端人工验收）；Python 3.13 已由 CI 在真实环境验证：GitHub Actions（Ubuntu）用同一份锁定集真实安装并跑通冒烟测试（[run 37215141304](https://github.com/whyao56/WingMan/actions/runs/37215141304)，`3.11` / `3.13` 两个 job 均 success），但**开发机没有 3.13、未在本机真跑**；macOS / Linux 桌面未做人工验收 |

---

## 它长什么样

界面是这样的（浅色、不刺眼；顶部那三张「上手卡」在配置完成后会自动消失）：

<img src="docs/images/ui-copilot.png" alt="WingMan 指挥台界面" width="880">

选中和某个人的全部聊天记录 → 告诉它你的目标（比如"想约她周末看展"）→
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

## 通话转写：暂停了，但思路留着

「通话时实时听懂对方」是这个项目最早的想法之一，上一版也真的做出来了 ——
双通道采集 + 云端/本地转写 + 实时字幕，跑得通。但效果不够好：

- 转写有可见延迟，对话里等不起；
- 双方声音串音，人和话对不上；
- VAD 按静音切句，一句「我觉得……（停顿）……还是算了吧」被切成两段，语义就断了；
- 而要对齐这些，最省事的办法是把通话音频送到云端 —— 对一个「本地优先」的工具，
  这个代价不划算。

所以这一块**从界面到代码整体撤下来了**，没有留半成品占位置。留下的是想清楚的方案：

| # | 思路 | 为什么 |
|---|---|---|
| 1 | **双通道采集，把「谁说的」分清** | 一路抓系统回环当「对方」，一路抓麦克风当「我」，两条流独立分帧、独立识别。事后靠声纹去切是下策 —— 切错一次整段记录都不可信 |
| 2 | **本机转写，音频不出机器** | 用 `faster-whisper` 本地跑，模型权重存数据目录、可离线。代价是首次要下模型、CPU 比显卡慢，但换来的是一帧音频都不上传 —— 这正是这个工具存在的理由 |
| 3 | **按「话轮」断句，而不是按静音断** | VAD 先粗切，再用小模型判断句尾是否完整，不完整就继续等下一帧。宁可多等几百毫秒，也不切坏一句话 |
| 4 | **落库时和文字记录同构** | 转写结果按 `(时间, 说话人, 文本)` 写成普通消息，只多带一个 `source=call` 标记。这样记忆、向量检索、人物分析都不用为通话写第二套逻辑 |
| 5 | **开录前把话说在前面** | 录音涉及法律问题。开始前明确确认，只用于你本人参与的通话、只存本机，且不提供「采集他人对话」的用法。这条不做成可关闭的选项 |

界面上的「通话」页保留了这份说明和一张静态效果预览 —— 点进去看不到按钮，
因为后端入口也已经一并移除了，不会出现「点了没反应」。

> 为什么连代码一起删，而不是留个开关？因为这个项目的每个可选项最后都要有人维护、
> 要有人测、要在发版清单里占一行。一个「暂时没有出口」的功能留在仓库里，
> 只会让下一个来读代码的人以为它可用。想回来看实现，`git log` 里有。

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
                    ┌──────────────────────────────────────────┐
                    │  Engine 层（核心）                        │
                    │  Analyzer → Suggestor → Simulator        │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │  LLM 层（可切换）                          │
                    │  OpenAI 兼容 · Ollama · Mock             │
                    └──────────────────────────────────────────┘

   （通话音频 ──▶ 转写 ──▶ Engine 这条支线已撤下，见上一节）
```

详细的模块划分、数据流、接口契约见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 3 步上手：下载 → 启动 → 看到第一条建议 |
| [docs/DESKTOP.md](docs/DESKTOP.md) | **打包成桌面程序**：怎么用、怎么构建、踩过的坑 |
| [docs/MODELS.md](docs/MODELS.md) | 接真实模型：OpenAI 兼容云 API / Ollama、怎么确认接上了、密钥与隐私边界 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 排错手册：退出码含义、端口占用、Python 版本、依赖装不上、中文乱码、代理劫持回环请求…… |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、模块职责、数据模型、接口契约 |
| [docs/ENGINE_DESIGN.md](docs/ENGINE_DESIGN.md) | 参谋引擎的算法与 Prompt 设计（这是项目的灵魂） |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 迭代路线：从脚手架到能用、好用 |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | 数据合规、隐私边界、使用红线 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

---

## 目录结构

```
WingMan/
├── wingman.cmd                  # 一键启动器（源码路线入口）：装环境 / 自检 / 启动
├── backend/
│   ├── app/
│   │   ├── adapters/            # 聊天记录接入插件（QQ / 微信 / 通用）
│   │   ├── memory/              # 存储、向量化、检索、人物画像
│   │   ├── llm/                 # 大模型接入（云 / 本地 / Mock）
│   │   ├── engine/              # 分析 → 建议 → 推演 核心引擎
│   │   ├── api/                 # HTTP 接口
│   │   ├── desktop.py           # 桌面启动器（exe 入口，含 --check）
│   │   └── selfcheck.py         # 应用内自检：缺什么、怎么补
│   ├── run_wingman.py           # PyInstaller 打包入口
│   ├── tests/                   # 冒烟测试 + 回环代理 / 版本号一致性 / 前端资源守卫
│   ├── requirements.txt         # 依赖下限声明（人类可读）
│   ├── requirements.lock.txt    # 精确版本锁定（启动器安装的就是它）
│   ├── requirements-desktop.txt # 打包工具链（pyinstaller / pywebview）
│   └── data/                    # 运行时数据（已 gitignore）
├── frontend/index.html          # 单文件控制台，零构建
├── build/wingman.spec           # PyInstaller 打包配置
├── samples/                     # 虚构的示例聊天记录，可直接导入试跑
├── scripts/
│   ├── bootstrap.ps1            # 启动器主体（建环境 / 装依赖 / 调 preflight）
│   ├── preflight.py             # 启动前自检（wingman.cmd --doctor）
│   ├── build_exe.py             # 一键打包成 exe
│   ├── e2e_check.py             # HTTP 端到端检查
│   ├── openai_stub.py           # 本地假 OpenAI 服务（离线联调用）
│   └── run_dev.*                # 开发态启动脚本
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

> `e2e_check.py` 会**往当前数据库里写数据**（导入示例会话、写入设定）。
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

**上传中文名的文件失败（旧版本的问题，现已支持）**
早期版本在 multipart 的 filename 里处理非 ASCII 有问题；**现在已支持中文名上传** ——
按 [QUICKSTART](docs/QUICKSTART.md) 第 3 步直接传 `samples\qq_sample_小鹿.txt` 即可（端到端检查实测全绿）。
如果你的环境里仍然失败（极少见），把文件复制一份改成英文名再上传即可，内容不受影响。

**「通话」页为什么什么都没有？**
因为它现在就是「规划中」。语音转写这一轮撤下来重做了 —— 页面上保留了设计思路和一张
静态效果预览，但没有任何按钮，后端接口也已一并移除，所以不会出现「点了没反应」。
想了解详细原因和方案，见上文[「通话转写：暂停了，但思路留着」](#通话转写暂停了但思路留着)。

**双击 exe 没反应 / 提示「Windows 已保护你的电脑」**

后者不是 bug，是 Windows 对未签名程序的默认行为：点「更多信息」→「仍要运行」。

前者请按顺序做两件事：
1. 打开 `%LOCALAPPDATA%\WingMan\logs\wingman.log`，最后一段就是出错原因
2. 命令行执行 `WingMan.exe --check`，它会生成一份人话报告并写到
   `%LOCALAPPDATA%\WingMan\logs\selfcheck.txt`，可以直接发给别人看

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

### 4. 「删掉一个功能」比「加一个功能」更容易留下垃圾

撤下通话语音时，我按 `grep asr` 干掉了 60 多处引用，跑测试 **当时那 41 项全绿**，
看起来干净了。但复查时发现还有一堆东西活着：

| 漏掉的 | 为什么测试抓不到 |
|---|---|
| `app/bus.py` | 它是「采集线程 → SSE 客户端」的进程内事件总线。SSE 端点删了，就再没人 `subscribe()`，但 `EventBus` 自己仍然被 `main.py` 实例化并 `bind_loop()` —— 语法合法、启动不报错，**只是永远收不到事件** |
| `check_optional_soundcard` / `check_optional_faster_whisper` | 仍注册在 `preflight.py` 的 `CHECKS` 里。它们在 `--doctor` 报告里继续列着「未安装 soundcard（可选）」—— 用户看到的是**一个不存在的功能提示他装依赖** |
| 三处文档字符串、`schemas.py` 的 `# llm \| embedder \| asr` 注释 | 注释和字符串常量不参与运行，改轮廓不会碰到它们 |
| `docs/TROUBLESHOOTING.md` 删掉一章后，`docs/MODELS.md` 里「见第 10 节」的交叉引用全部错位 | 是**跨文件的数字**，两边谁也管不到谁 |

共同点：**它们都不会报错**。测试全绿说明的是「现有断言都还成立」，
不等于「删干净了」—— 因为没人给「这个东西不该再存在」写断言。

> 教训：删除类改动的验收标准不是「测试通过」，而是**「搜不到残留引用 + 逐处确认它的调用方也没了」**。
> 具体做法是三层检查：
> ① `grep` 关键词，逐条看是不是现存功能；
> ② 对每个要删的模块问「**谁在调用它**」，调用方也删了才算删干净（`bus.py` 就是这么揪出来的）；
> ③ 跨文件引用的章节号 / 文件名，改完统一再搜一遍。
>
> 另外一条：**留着「以后可能要用」的空壳，是有成本的**。一个恒为 `false` 的开关、
> 一个永远为空的常量，会让下一个人以为功能只是没开。所以这次 `OPTIONAL_MODULES` 虽然保留，
> 但改成了**数据驱动** —— 注册表为空时预检面板就明确写「当前版本没有需要额外安装的可选依赖」，
> 而不是让两个永远不会通过的检查项继续挂在那里。

---

## 免责声明

本项目仅供**个人**在**本人设备**上、对**本人参与过**的对话做备忘与复盘之用。
请勿用于未经同意的他人数据采集、监控或任何违法用途。
使用前请阅读 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

## License

MIT
