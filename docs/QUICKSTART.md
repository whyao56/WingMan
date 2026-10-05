# 快速上手（Windows）

从零到看见第一条回复建议，只要 **3 步**。不用装 Node，不用申请 API Key，也不用先看懂原理。

> **前置条件**：Windows 10/11 + **Python 3.11 或更高版本**（安装 Python 时记得勾选 *Add python.exe to PATH*）。
> 没装或不确定？先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 里的「启动器说 Python 版本太低 / 找不到 Python」。

---

## 第 1 步：下载并解压

- 直接下压缩包：<https://github.com/whyao56/WingMan/archive/refs/heads/main.zip>
- 或者用 Git：`git clone https://github.com/whyao56/WingMan.git`

解压到**任意目录**都行（路径里有中文、有空格都没问题）。
注意：先完整解压，**不要**在压缩包窗口里直接双击运行。

## 第 2 步：双击 `wingman.cmd`

进入解压出来的 `WingMan` 目录，双击 **`wingman.cmd`**。

也可以自己开一个终端，在仓库根目录执行（两种 shell 的写法不一样，注意前缀）：

```bat
wingman.cmd
```

```powershell
.\wingman.cmd
```

> PowerShell 里必须写成 `.\wingman.cmd`，直接敲 `wingman.cmd` 会提示「无法将该项识别为 cmdlet」。

第一次运行会自动完成：创建 Python 环境 → 安装依赖（**几分钟，取决于网速**）→ 启动服务 → **自动打开浏览器**：

    http://127.0.0.1:8787

**怎么算成功了**：浏览器里出现 WingMan 控制台，左侧是「指挥台 / 记忆 / 导入 / 采集 / 通话（敬请期待）/ 自检 / 设置」，左下方显示类似 `v0.3.0 · 0 条消息`，以及 `llm: mock` 这样的组件状态。

> 浏览器没自动打开 → 手动访问上面的地址（`--no-browser` 就是用来关掉自动打开的）。
> 只想先装好环境、不启动服务 → `wingman.cmd --setup-only`。

## 第 3 步：导入示例，拿到第一条建议

1. 左侧点「**导入**」→ 在「上传文件」里选仓库中的 `samples\qq_sample_小鹿.txt` → 点「**确认导入**」。
   导入成功会显示「新增 62 条」（示例文件里就是 62 条消息），并自动跳到「记忆」页。
2. 左侧点回「**指挥台**」→ 点「**取会话里最后一条对方消息**」（省得手打）→ 点「**分析并给建议**」。
3. 下方出现「**对方状态分析**」和「**建议回复 4 选 1**」，每条都带综合分与走向预测；点任意一条的「推演走向」还能看之后三轮会怎么聊。

到这里，完整链路（导入 → 记忆 → 分析 → 建议 → 推演）已经跑通。

> ⚠️ 这一步用的是内置的 **Mock（规则引擎）**：不需要任何 Key，但它给出的内容**只是流程演示，不是真实智能**。
> 想让建议真的有意义，去接真模型 —— 见 **[MODELS.md](MODELS.md)**。

## 第 4 步（可选）：不用导出文件，直接从客户端拿聊天记录

如果你懒得「在 QQ / 微信里导出记录再上传」，可以走左侧的「**采集**」页：

- **半自动**：点「开启监听」，然后在 QQ / 微信里**选中几条消息复制**。
  程序把复制的文字、说话人和时间抓下来；认不出是谁说的会**挂起等你确认**，不会瞎猜。
  任何版本都能用，是本项目**实测确定能跑通**的那条路。
- **自动**：直接读客户端自己的加密数据库。能不能用取决于**你的客户端版本 + 能不能拿到密钥**，
  界面在配置环节就会告诉你结论和下一步（支持矩阵内置在程序里，不在这份文档里）。

两条路怎么选、版本对不对、密钥拿不到怎么办，见 **[TROUBLESHOOTING.md](TROUBLESHOOTING.md) 第 12 节**。
想先用示例数据把主链路跑通，就跳过这一步。

---

## 启动器参数速查

| 命令 | 作用 |
|---|---|
| `wingman.cmd` | 准备环境（首次）+ 启动服务 + 自动打开浏览器 |
| `wingman.cmd --port 8899` | 换端口（默认 8787） |
| `wingman.cmd --no-browser` | 不自动打开浏览器 |
| `wingman.cmd --setup-only` | 只准备环境，不启动服务 |
| `wingman.cmd --doctor` | 只做启动前自检，逐项打印检查结果与修复建议 |
| `wingman.cmd --help` | 打印用法 |

- 参数在 **cmd 与 PowerShell 里都能用**（记得 PowerShell 加 `.\` 前缀）。
- 停止服务：在运行中的窗口按 `Ctrl+C`；如果出现 `Terminate batch job (Y/N)?`，按 `Y` 再回车。
  就绪横幅里会打印进程 PID，来不及按的时候可以在另一个窗口执行 `taskkill /PID <pid> /F`，或者直接关掉窗口。
- **启动前**自查用 `wingman.cmd --doctor`；**服务已经在跑**时想看健康报告，用 `GET /api/health`
  （想知道「每个配置从哪来」，再加一个 `GET /api/health/details`；见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 第 9 节）。

也可以用环境变量（都是可选的，只对当前终端窗口有效）：

| 环境变量 | 作用 |
|---|---|
| `PORT` | 默认端口（`--port N` 优先于它） |
| `WINGMAN_ASCII=1` | 强制启动器输出纯英文 ASCII（终端渲染不了中文时用） |
| `WINGMAN_POWERSHELL` | 指定用哪个 powershell 可执行文件来跑引导脚本 |

```bat
set PORT=8899
wingman.cmd
```

```powershell
$env:PORT="8899"
.\wingman.cmd
```

## 出问题了？

1. 先跑一次 `wingman.cmd --doctor`：它会体检仓库完整性、Python 版本与位数、虚拟环境、依赖完整性、数据目录和端口，
   给出结论与修复建议（不修改配置、不建库、不动你的数据、也不启动服务；唯一会留下的是 Python 自动生成的
   `__pycache__` 字节码缓存）。
   > 服务已经在跑时，还可以查 `GET /api/health/details` 看配置来源与各 provider 状态（见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 第 9 节）。
2. 再看 **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**：端口被占、Python 版本、依赖装不上、中文乱码、Mock 输出像废话……常见情况都在里面。

## 下一步看什么

| 你想知道 | 看哪份 |
|---|---|
| 怎么接真实模型（云 API / Ollama）、怎么确认接上了 | [MODELS.md](MODELS.md) |
| 启动失败、报错看不懂 | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| 系统架构、模块职责与接口契约 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 参谋引擎的算法与 Prompt 设计 | [ENGINE_DESIGN.md](ENGINE_DESIGN.md) |
| 哪些做了、哪些没做、接下来做什么 | [ROADMAP.md](ROADMAP.md) |
| 隐私边界、使用红线、自动回复的立场 | [COMPLIANCE.md](COMPLIANCE.md) |
| 版本变化 | [../CHANGELOG.md](../CHANGELOG.md) |
