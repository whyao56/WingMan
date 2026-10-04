# 接入真实模型

WingMan **不配任何模型也能跑通全流程**，但那时用的是内置的 Mock（规则引擎），
它给出的分析、建议、推演**只是流程演示，不是真实智能**。这份文档讲的是：怎么换成真模型，以及**怎么确认自己真的换成功了**。

---

## 1. 三种模式，先选一个

| 模式 | 需要什么 | 能力 | 数据去哪 |
|---|---|---|---|
| **Mock**（默认） | 什么都不用 | 规则模板，能自圆其说，但不懂你说的话 | 完全在本机 |
| **OpenAI 兼容云 API** | 一个服务商的 `base_url` + `API Key` + 模型名 | 最好，推荐先用这个试 | 你发的消息与检索出的历史上下文会**上传到服务商** |
| **Ollama 本地模型** | 本机装 Ollama + 拉一个模型（几个 GB） | 比云端弱一些，够用 | 推理在本机；但**向量化默认会跟随主模型走云端**（见第 6 节） |

支持一切 OpenAI 兼容协议的服务：DeepSeek、通义、Kimi、智谱、硅基流动、OpenAI、各类中转站、vLLM、LM Studio……
只要它提供 `/v1/chat/completions`。切换是**控制台里改一下、点保存**的事，不用改代码、不用重启。

---

## 2. 怎么知道「现在跑的到底是不是真模型」

四个观察点，任选，建议至少看前三个。

**① 控制台「设置」页顶部的「当前生效的组件」**
会显示 `llm · <provider 名> · <note>`。名字是 `mock` 就说明还在用规则引擎；
是 `openai_compat` / `ollama` 才是真模型。绿色=可用，黄色=不可用（note 里写着原因）。

**② 侧边栏最下方的健康区**
启动后显示 `v<版本> · <消息数>` 和 `llm: <provider 名>` 等标签，一眼就能看出用的是哪个。

**③ 「设置」页的「测试连通」按钮**
点一下会真的发一次最小请求，结果显示成一行：

- 成功：`✓ openai_compat — 正常`（后半段是模型回的内容）
- 失败：`✗ openai_compat — <失败原因>`

⚠️ **默认的 Mock 下点「测试连通」会显示 ✗ —— 这不是故障。**
Mock 是规则引擎，只认引擎内部的任务标记，不认这种自由提问，于是它老实报错：

```
✗ mock — Mock Provider 不认识任务标记 [TASK:UNKNOWN]。请检查 prompts.py 是否正确设置了标记。
```

这恰好说明：**判断「现在用的是不是真模型」，依据是 provider 名字，不是有没有 ✓。**

**④ 直接问后端**（服务已在运行时）

```bat
curl -s http://127.0.0.1:8787/api/health
```

```powershell
curl.exe -s http://127.0.0.1:8787/api/health
```

> PowerShell 里要写 `curl.exe`：`curl` 是 `Invoke-WebRequest` 的别名，参数不通用。

响应里的 `providers` 数组就是答案：

```json
{"version":"0.1.0","db":"...\\backend\\data\\wingman.db","counts":{"messages":62,"facts":6,"chats":1},
 "providers":[{"kind":"llm","name":"openai_compat","available":true,"note":"https://api.deepseek.com/v1 · deepseek-chat"},
              {"kind":"embedder","name":"hash","available":true,"note":"..."},
              {"kind":"asr","name":"mock","available":true,"note":"..."}]}
```

`llm` 的 `name` 是 `openai_compat`、`available` 为 `true` —— 这就证明**运行的不是 Mock，而且配置是完整的**。
（`available` 说明配置齐了；要证明「真的连得上」，还要看上面第 ③ 条那个 ✓。）

**⑤ 启动窗口的日志**（每次启动都会打印）
每个组件一行，前面的 `OK ` / `!! ` 就是可用性；如果还是 Mock，这里会有一句明确的警告：
「当前使用 Mock Provider，输出仅用于演示流程」。

**⑥ 想一屏看全**：`wingman.cmd --doctor` 会逐项自检，并打印一行结论式的配置快照，
形如「配置来源：.env 0 个、运行时覆盖 2 项 · provider：llm=openai_compat；embedder=hash；asr=mock」——
这是判断「我到底配成了什么」最省事的办法（自检脚本 `scripts/preflight.py` 也能单独跑，细节见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 第 10 节）。

---

## 3. 路径 A：OpenAI 兼容云 API

### 3.1 先在服务商那边拿到三样东西

1. **API Key**（形如 `sk-...`）
2. **Base URL**：填到 **`/v1` 为止**，例如
   - DeepSeek：`https://api.deepseek.com/v1`
   - 硅基流动：`https://api.siliconflow.cn/v1`
   - OpenAI：`https://api.openai.com/v1`
   - 自建/中转：`http://127.0.0.1:3000/v1` 这类
   ⚠️ **不要**带 `/chat/completions` —— 程序会自己在后面拼它。
3. **模型名**：例如 `deepseek-chat`（各家的名字不同，以服务商文档为准）

### 3.2 在控制台里填

打开 <http://127.0.0.1:8787> → 左侧「**设置**」→「**大模型**」卡片：

| 设置页字段 | 填什么 |
|---|---|
| Provider | 选 `OpenAI 兼容云服务（DeepSeek / 通义 / Kimi / 硅基流动…）` |
| Base URL | 上面的 `/v1` 地址 |
| API Key | 你的 Key |
| 模型名 | 服务商的模型 ID |

点「**保存设置**」（会提示「已保存并生效」）。
不确定模型名？点旁边的「**拉取模型列表**」，它会请求 `<Base URL>/models` 并把可用的模型列出来，点一个即可。

### 3.3 判定「接上了」

1. 点「**测试连通**」→ 出现 `✓ openai_compat — …`（不是 `✗`）。
2. 第 2 节的 ① / ② / ④ 任一确认 `name` 是 `openai_compat`、`available` 为 `true`。
3. 回「**指挥台**」，贴一句对方的话，点「**分析并给建议**」——真模型的输出会明显贴合你贴的内容，而不是套话。

### 3.4 报错怎么读

程序会把服务商的错误翻译成中文，直接显示在「测试连通」的结果里：

| 你看到的 | 含义与处置 |
|---|---|
| 鉴权失败（401） | Key 错了或过期。重新复制 Key，注意别带空格 |
| 无权访问（403） | Key 权限不足，或该模型没开通 |
| 地址不存在（404） | Base URL 填错。**通常应以 `/v1` 结尾，且不要带 `/chat/completions`** |
| 请求过于频繁或额度用尽（429） | 限速或余额不足 |
| 服务端错误（500）/ 网关错误（502）/ 服务不可用（503） | 服务商那边的问题，稍后重试；中转服务不稳定很常见 |
| 请求超时（120s） | 网络慢或服务商卡住；可换更小的模型试试 |
| 网络错误：… | 本机网络/代理问题，见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 的依赖与代理一节 |
| 响应不是 JSON / 响应里没有 choices | 该地址不是 OpenAI 兼容接口，或返回了网页（常见于把网页地址当 API 填） |

---

## 4. 路径 B：Ollama 本地模型

适合「聊天内容不想给任何云服务」的场景。代价是要本机显存/内存，能力比云端弱。

### 4.1 装 Ollama 并拉模型

1. 去 <https://ollama.com/download> 安装 Windows 版（装完它会在后台跑 `ollama serve`，默认监听 `127.0.0.1:11434`）。
2. 拉一个中文尚可、显存要求不高的模型：

```bat
ollama pull qwen2.5:7b
```

> `qwen2.5:7b` 是默认值，8G 显存左右能跑。机器更好的话可以试 `qwen2.5:14b`。

### 4.2 在控制台里填

「**设置**」→「**大模型**」：

| 设置页字段 | 填什么 |
|---|---|
| Provider | 选 `Ollama 本地模型` |
| Ollama 地址 | 默认 `http://127.0.0.1:11434`，没改过就不用动 |
| Ollama 模型 | `qwen2.5:7b`（你 `ollama pull` 过的那个） |

点「**保存设置**」→ 点「**测试连通**」。

### 4.3 报错怎么读

| 你看到的 | 含义与处置 |
|---|---|
| 连不上 Ollama（http://127.0.0.1:11434）。请确认 ollama serve 已在运行。 | Ollama 没启动，或地址填错。先开 Ollama，再在浏览器访问 `http://127.0.0.1:11434` 看有没有响应 |
| 模型 xxx 未找到。先执行：ollama pull xxx | 模型名写错或还没拉。执行提示里的那条命令 |
| 本地推理超时（180s），模型可能过大。 | 显存不够或模型太大，换更小的模型（如 `qwen2.5:7b` 甚至 `qwen2.5:3b`） |

---

## 5. 离线验证：模型配置到底通没通

不想为了试一下就去注册云服务账号？仓库里带了一个**本地 OpenAI 兼容 stub**：`scripts/openai_stub.py`。
它不是真模型，只负责扮演一个「OpenAI 兼容服务端」，用来证明**你按文档填的 base_url / api_key / model 真的被用上了**。

```bat
REM 1) 先把环境准备好（没跑过启动器时才需要）
wingman.cmd --setup-only

REM 2) 另开一个窗口，看它支持哪些参数：启动参数以 --help 为准
backend\.venv\Scripts\python.exe scripts\openai_stub.py --help
```

```powershell
.\wingman.cmd --setup-only
backend\.venv\Scripts\python.exe scripts\openai_stub.py --help
```

按 `--help` 给出的参数启动 stub，它会打印自己监听的地址和模型名。然后：

1. 「设置」里按第 3.2 节填：Provider 选 `OpenAI 兼容云服务`，Base URL 填 stub 打印的地址，模型名填 stub 打印的模型名，API Key 随便填一个非空字符串（stub 不校验）。
2. 点「**测试连通**」，应显示 `✓ openai_compat — …`。
3. 用第 2 节 ④ 的 `curl` 确认 `providers` 里 `llm` 是 `openai_compat` 且 `available` 为 `true` —— **这就是「没在跑 Mock」的证据**。
4. 回「指挥台」点「分析并给建议」，返回的内容会带着 stub 的标记文案，说明请求确实发到了你配置的那个地址。

> **说清楚**：这是**离线自检演示**，验证的是「配置链路 + 请求形状」正确，**不代表真实模型的智能水平**。
> 想知道效果好不好，必须接真实服务商或本地 Ollama 自己试。

---

## 6. 它到底「懂不懂」：一个可以自己量化的判定口径

接上真模型之后，怎么判断它是真在理解、还是在说场面话？

项目给出的口径（见 [ROADMAP.md](ROADMAP.md) 最后一节）：从你自己的真实记录里挑 **20 个你已经回复过的片段**，
把「对方那句话之前」的状态喂给引擎，看它给的选项里**有没有你当时真选的那条**。
**命中率超过 50%，说明它真的在理解，而不是在说场面话。**

低于这个数，先别折腾界面和语音 —— 去调 Prompt（ROADMAP 阶段 1.4）。

---

## 7. 密钥与隐私边界（重要）

**密钥存哪、怎么显示**

- 在控制台「设置」里保存的配置，存在 `backend\data\wingman.db` 里（运行时覆盖），**优先级最高**，重启仍在；
  完整的优先级是：**控制台保存的运行时覆盖 > 进程环境变量 > `.env` > 代码默认值**。
  想回到 `.env`：点「清除运行时覆盖（回到 .env）」。
- **界面上和 API 响应里的密钥只以掩码形式出现**（形如 `sk-a******xyz`），并用 `llm_api_key_set` 这类标志告诉你「配没配」。
  保存时如果字段里是掩码值，不会覆盖真实密钥。密钥明文不会出现在设置页、`/api/health`、`/api/settings` 或自检输出里。
- `.env` 已被 `.gitignore` 忽略 —— 不要把真实 Key 提交到任何仓库。

**数据边界**（与 [COMPLIANCE.md](COMPLIANCE.md) 一致）

| 你的配置 | 消息与历史上下文会去哪 |
|---|---|
| Mock | 不出本机 |
| 云端 LLM | **会随每次请求上传到该服务商**（这就是用云端的取舍） |
| Ollama + 向量模型设为 `hash` | 推理与向量化都在本机 |
| Ollama，但向量模型保持 `auto` 且填了云端 Key | 推理在本机，**向量化仍会走云端** |

最后一行是最容易踩的坑：想让数据尽量不出本机，要把三处都设好 —— Provider 选 `Ollama`、
「向量模型」选 `hash`、「语音识别」选 `mock` 或本地 whisper。**只要有一处指向云端，那部分数据就会上传。**

另外三条边界（引自 COMPLIANCE，别忽略）：

- 只能处理**你自己参与过**的对话；
- 通话录音要符合你所在地的法律，只用于个人备忘；
- **本项目不做自动回复**：只出建议、一键填进输入框，发送永远由你本人确认。

---

## 8. 语音（ASR）要不要接

语音是**可选**能力，不接不影响前面的全部流程：

- 默认 `ASR = mock`（占位文本），「通话」页也能用「手动注入一句话」的方式演示实时字幕。
- 想真转写：先 `wingman.cmd --with-asr` 装依赖，再在「设置 → 语音识别」里选 `本地 faster-whisper`（隐私最好）或 `云端 ASR`。
- 「没有检测到音频设备」怎么办 → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

> 说明：语音采集与 ASR 的效果**本次发行没有做端到端验证**，需要你自己在设备上试。
> 采集必须是你在「通话」页手动点「开始采集」才会启动。

---

## 9. 配置项速查

| 设置页 | `.env` 里的键 | 默认值 | 备注 |
|---|---|---|---|
| Provider（大模型） | `LLM_PROVIDER` | `mock` | `mock` / `openai_compat` / `ollama` |
| Base URL | `LLM_BASE_URL` | 空 | 以 `/v1` 结尾 |
| API Key | `LLM_API_KEY` | 空 | 云端才需要 |
| 模型名 | `LLM_MODEL` | 空 | 例：`deepseek-chat` |
| 温度 | `LLM_TEMPERATURE` | `0.8` | 越高越发散 |
| Ollama 地址 | `OLLAMA_HOST` | `http://127.0.0.1:11434` | |
| Ollama 模型 | `OLLAMA_MODEL` | `qwen2.5:7b` | |
| 向量模型模式 | `EMBEDDER` | `auto` | `auto` / `cloud` / `hash` |
| 语音引擎 | `ASR_ENGINE` | `mock` | `mock` / `local` / `cloud` |

（`.env` 是可选的：不建也能跑，所有配置都能在控制台里改。）
