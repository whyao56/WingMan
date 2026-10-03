# ChatWing · 聊天僚机

[![CI](https://github.com/whyao56/chatwing/actions/workflows/ci.yml/badge.svg)](https://github.com/whyao56/chatwing/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue.svg)](https://www.python.org/)

> 把聊天记录交给大模型记忆，通话时实时听懂对方，然后告诉你**该怎么回**。

ChatWing 是一个本地优先的「对话参谋」系统。它做三件事：

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

```bash
# 1. 装依赖（Python 3.11+）
cd chatwing/backend
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

# 2. 起服务
python -m uvicorn app.main:app --reload --port 8787

# 3. 打开控制台
#    http://127.0.0.1:8787
```

**不需要任何 API Key 也能跑通全流程** —— 未配置模型时会自动使用内置的 Mock Provider，
走完「导入 → 画像 → 分析 → 建议 → 推演」整条链路，方便你先看懂它怎么工作。

想接真模型时，在控制台「设置」里填 `base_url` / `api_key` / `model` 即可，
支持一切 **OpenAI 兼容协议**的云端服务（DeepSeek、通义、Kimi、硅基流动、OpenAI…），
也支持 **Ollama** 本地模型（数据不出本机）。

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
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、模块职责、数据模型、接口契约 |
| [docs/ENGINE_DESIGN.md](docs/ENGINE_DESIGN.md) | 参谋引擎的算法与 Prompt 设计（这是项目的灵魂） |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 迭代路线：从脚手架到能用、好用 |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | 数据合规、隐私边界、使用红线 |

---

## 目录结构

```
chatwing/
├── backend/
│   ├── app/
│   │   ├── adapters/     # 聊天记录接入插件（QQ / 微信 / 通用）
│   │   ├── memory/       # 存储、向量化、检索、人物画像
│   │   ├── llm/          # 大模型接入（云 / 本地 / Mock）
│   │   ├── asr/          # 语音识别 + 双通道音频采集
│   │   ├── engine/       # 分析 → 建议 → 推演 核心引擎
│   │   └── api/          # HTTP 接口
│   ├── tests/
│   └── data/             # 运行时数据（已 gitignore）
├── frontend/index.html   # 单文件控制台，零构建
├── samples/              # 示例聊天记录，可直接导入试跑
├── scripts/              # 启动脚本 / 演示数据生成
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

**Mock 模式下分析结果是废话**
这是预期的。Mock 只是一套规则，用来验证流程能不能跑通。
接上真实模型（设置页填 base_url / api_key / model）之后才会有真正的理解能力。
判断标准见 [ROADMAP](docs/ROADMAP.md) 最后一节。

---

## 免责声明

本项目仅供**个人**在**本人设备**上、对**本人参与过**的对话做备忘与复盘之用。
请勿用于未经同意的他人数据采集、监控或任何违法用途。
使用前请阅读 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

## License

MIT
