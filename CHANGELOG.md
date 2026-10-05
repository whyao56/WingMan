# 更新日志

本文件格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

下一个版本的计划见 [docs/ROADMAP.md](docs/ROADMAP.md)（阶段 1「能用」/ 阶段 2「好用」）。
目前最大的缺口是 **Prompt 调优**：默认的 Mock 引擎让整条链路能跑，但建议内容还是空的。

## [0.2.1] - 2026-10-05

这一版是把用户反馈的四个问题逐个查到底的结果。其中两个**不是体验问题，是真 bug**：
一个是预设里填了已被官方下线的模型名（必失败），一个是同名 CSS 类互相覆盖
（把每个页面的副标题都压成 66px 宽的一列）。所以它同时是一次可用性修复和一次视觉重做。

### 修复

- **「云模型填了也连不上」的真正原因**：界面预设里的 DeepSeek 模型名是 `deepseek-chat`，
  而它在 2026-07-24 已被官方下线，照着填必然报错。预设改为当前可用的
  `deepseek-flash` / `deepseek-v4-pro`（点一下可在两者间切换），并新增**退役模型名检测**：
  读到旧名字当场提示、一键替换。其他服务商也补了「模型名时有更新，用拉取列表更准」的说明。
- **每个页面的副标题都被压成一列**：`.sub` 这个类名同时被用作「副标题段落」和
  「通话字幕行」，后者又定义了 `display:grid; grid-template-columns:66px 1fr`，
  静默覆盖前者 —— 结果每个页面标题下的说明文字只占 66px 宽，看起来就是「字全挤在一起」。
  字幕行已改名 `.cap`。新增 `backend/tests/test_frontend_assets.py` 两条守卫测试钉住它
  （一条查 CSS 顶层类名重复，一条查预设里不出现已下线的模型名）。
- **自检报告的位置在文档里写错了**：它实际写在数据目录
  `%LOCALAPPDATA%\WingMan\logs\selfcheck.txt`，而 zip 内的使用说明、README 和发行说明
  都把它说成写在「程序目录」—— 程序目录下压根没有 `logs\` 这个文件夹。
  功能是好的，但用户出问题时按说明去找，找不到文件只会以为「自检根本没跑」。
  三处文案已改正，并加守卫测试（`test_version.py`）禁止再把它说成程序目录。

### 变更

- **界面整体重做**，目标是从「AI 生成的后台面板」变成「正常的桌面工具」：
  配色由深色 + 紫渐变改为浅色中性灰阶 + 单一强调色（靛蓝）；
  字号收成 12 / 13 / 14.5 / 15.5 / 21 五档、正文行高 1.7，间距统一走 8 / 12 / 16 / 24 / 32 的节奏；
  侧栏图标从混用的符号字符（◎ ◈ ↧ ◉ ✚ ⚙）换成统一线性 SVG；卡片、表单、按钮全部重排。
- **新手引导**：指挥台顶部新增三步卡「接上大模型 → 导入聊天记录 → 开始分析」，
  按钮直达对应页面，两步都齐之后整块自动收起。
- **连通失败不再只丢一句 HTTP 错误**：按 401 / 403 / 404 / 429 / 模型名 / 网络超时
  分别给出「可能是什么原因 + 下一步点哪」。
- 记忆页的事实归属标记由 `peer` / `me` 显示为「对方」/「我」。
- OpenAI 兼容层的 `User-Agent` 改为读取 `__version__`，不再手写（手写过一次 0.1）。

### 新增

- 支持 `?view=<页面名>` 深链接（如 `?view=settings`）；支持 `?nostream=1` 关闭 SSE 长连接
  —— 后者是给自动化截图 / 视觉回归用的（EventSource 是持久连接，会让「等网络空闲再截图」
  的工具永远等不到空闲）。两者都不影响正常使用。

## [0.2.0] - 2026-10-05

第二个发行版：从「代码能跑通」到「**双击就能用、坏了还能自己看出哪坏了**」。
首次提供免安装的 Windows 成品包（见 [Releases](https://github.com/whyao56/WingMan/releases)），
对方机器不需要装 Python。

### 新增

- **打包成 Windows 桌面程序**：`python scripts/build_exe.py` 一条命令出包，
  产物是绿色免安装的 `dist/WingMan/`（下载 31 MB / 91 MB，解压后 68 MB / 237 MB）。
  两种风味：标准版（云 ASR）与 `--with-asr` 完整版（含本地语音识别）。
  详见 [docs/DESKTOP.md](docs/DESKTOP.md)。
- **发行脚本** `scripts/make_release.py`：打包 → 算校验和 → 建 Release → 传附件一条命令完成。
  刻意做成**可复现**——时间戳钉死、文件顺序排序，同样输入重跑会得到字节相同的 zip，
  因此发行说明里印的 SHA256 是用户能独立验证的事实，而不是一次性的快照。
- **桌面启动器** `WingMan.exe`：端口探测 → 单实例检查 → 起服务 → 开原生窗口（pywebview），
  失败退回浏览器。所有异常走原生弹窗 + 滚动日志，不静默退出。
  命令行支持 `--check`（环境自检）、`--asr-test`（语音链路自检，`--source loopback` 可测「听对方」）、
  `--port` / `--no-window` / `--browser`。
- **应用内自检**（`backend/app/selfcheck.py` + 控制台「自检」页）：
  三档状态（已完成 / 建议补上 / 必须处理），每条非「已完成」都带一句人话说明
  和一个「点哪里能修好」的跳转按钮。同时暴露 `GET /api/selfcheck`（含运行形态与数据路径）。
- **语音两条路线可选**，由「设置 → 语音识别」决定：
  - 云 ASR：填任意 OpenAI 兼容的 `audio/transcriptions` 接口即可用（音频会传到第三方）；
  - 本地转写：`faster-whisper`，音频一帧都不出本机。
  模型按需下载（tiny/base/small/medium/large-v3），带真实进度、剩余时间与国内镜像开关。
- **语音链路实测**（控制台「自检」页 + `POST /api/asr/probe`）：
  录一段并转写，把设备列表、电平、引擎、耗时、识别文本逐步摊开。
  语音故障的现象永远是同一句「没反应」，这一屏用来把它拆成「第几步不行」。
- **模型对比工具** `scripts/asr_bench.py`：同一段音频跑多个规格，输出置信度、稳态耗时、
  实时倍率与识别结果，用来决定该选哪一档；同时也是一份量纲回归测试。
- 数据目录新增 `models/`（本地语音模型权重）与 `exports/`。

### 变更

- **可写数据在打包版下移到 `%LOCALAPPDATA%\WingMan`**（源码运行仍在 `backend/data`）。
  程序目录可能只读，且 onefile 的临时解压目录退出即删 —— 数据放那里用户会以为丢了。
- 已存在的旧库文件（改名前的 `chatwing.db`）会自动迁移到新名字，数据保留。
- **打包时剔除 `hf_xet`**（标准版少 9 MB，约 12%）。它是 `huggingface_hub` 的可选
  加速后端，被自动探测到就会打包进去，而我们无条件禁用了 Xet（见下条「修复」旁的说明），
  因此属于纯死重量。剔除后 `snapshot_download` 等接口照常可用，已实测下载一个
  完整模型跑通。
- `docs/DESKTOP.md` 新增第 7~12 节踩坑记录（`ready` vs `available`、录音采集是两条路线的
  公共依赖、量纲静默陷阱、Windows 上的 `/tmp` 语义、「静态分析看不见」的适用范围、
  手工验证时别连到旧进程）。

### 修复

- **本地转写输出乱码但不报错**：调用方传入「int16 量级的 float32」（值域 ±32768，
  只是 dtype 被转成了 float32），而代码只按 dtype 判断要不要归一化，
  于是把放大三万倍的波形喂给模型。新增 `to_float_mono()` 用**峰值兜底**，
  三种量纲形式的输出现在逐字一致（`backend/tests/test_audio.py` + `scripts/asr_bench.py` 双重回归）。
- **自检把「模型没下载」报成绿色「已完成」**：原先用 `available`（对象构造得出来）判断，
  而本地 whisper 在权重一个字节都没下载时它也是 `True`。拆出 `ready`
  （现在能不能真的出字）与 `not_ready_reason`（差在哪），自检改用 `ready`。
- **标准版的语音提示指错方向**：用户选了「本地模型」但包里没有语音库时，
  此前会笼统说「换完整版安装包」；现在会明确区分「库没内置」与「模型没下载」，
  并同时给出「改用云 ASR」这条替代路。
- **中文转写偶尔吐繁体**：`base` 模型会把「今天加班到10点,好累呀,你周末有空吗?」
  写成「今天加班到10點,好累呀,你周末有空嗎?」—— 内容对、字形错，用户会以为认错了字。
  现在中文转写会带一段简体 `initial_prompt` 把它带回简体（实测同一段音频恢复为
  「今天加班到10点,好累呀。你周末有空吗?」）。只在明确是中文时加，`auto` 语种识别
  模式不加，否则会把语种判断带偏。
- **模型下载失败时的提示会把人带偏**：`_explain_error()` 原先没有「缺组件」这一类，
  于是 `ModuleNotFoundError` 也落到兜底话术，建议用户「重试一次 / 勾上国内镜像」——
  而镜像补不上一个没打进包的模块，这是个永远修不好的方向。现在这类错误会明确
  告知「这是安装包的问题」。

### 已知限制

- 打包版仅在中文 Windows 上验证过；未做代码签名，首次运行会被 SmartScreen 拦
  （点「更多信息」→「仍要运行」即可）。
- 本地转写的准确性与速度都取决于模型规格。同一句中文 5.3 秒音频，
  打包版 `--asr-test` 实测：`tiny` 置信度 0.62 / 0.5s，`base` 0.74 / 0.8s；
  `scripts/asr_bench.py` 另量了稳态吞吐（tiny 约 21x 实时、small 约 3.9x 实时）。
  CPU 上建议 `small` 起步。
- 打包版实际验证过的本地模型只有 `tiny` 与 `base`（都在冻结环境下跑通了
  下载 → 加载 → 转写全链路）；`small` / `medium` / `large-v3` 只验过下载与
  文件结构，没在打包版里跑过转写。

## [0.1.0] - 2026-10-04

首个发行版：从「代码能跑通」到「陌生人下载下来能装起来、知道自己在用什么」。

### 新增

- **一键启动器 `wingman.cmd`**：根目录唯一入口。首次运行自动创建 Python 环境、安装依赖、启动服务并打开控制台；
  支持 `--port N`、`--no-browser`、`--setup-only`、`--doctor`、`--with-asr`、`--help`。
- **启动前自检与可操作报错**：对 Python 版本过低、端口被占用、数据目录不可写、依赖安装失败四类问题给出中文修复建议；
  失败以区分性退出码结束（`0` 成功 / `2` 前置自检拒绝 / `3` 依赖准备失败 / `1` 其他），
  前置自检失败时不产生半成品环境。
- **运行期自检通道**：`wingman.cmd --doctor` 体检仓库完整性、Python 版本与位数、虚拟环境、依赖锁与依赖完整性、数据目录与端口；
  `GET /api/health` 保持最小只读契约（版本 / 数据路径 / 计数 / provider 摘要，`scripts/e2e_check.py` 依赖它）；
  新增 `GET /api/health/details` 结构化自证报告（逐键标注配置来源 `runtime`/`env`/`dotenv`/`default`、命中的 `.env`、
  运行时覆盖的键名、完整路径、DB 统计、可选依赖与 notes）。**密钥在健康/自检输出中一律以掩码出现，不会回显明文。**
- **新手上手文档**：[docs/QUICKSTART.md](docs/QUICKSTART.md)（3 步上手）、
  [docs/MODELS.md](docs/MODELS.md)（接真实模型与「怎么确认接上了」）、
  [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)（排错手册）。
- **离线自检脚本** `scripts/openai_stub.py`：本地 OpenAI 兼容 stub，用于在不注册任何云服务的前提下，
  验证「按文档填的 base_url / api_key / model 真的被用上了」。
- 示例聊天记录 `samples/qq_sample_小鹿.txt`、`samples/wechat_sample_阿哲.txt`（均为虚构数据）。

### 变更

- 用户入口统一为根目录 `wingman.cmd`；`scripts/` 下的开发调试脚本仍然保留。
- 依赖新增**精确版本锁定** `backend/requirements.lock.txt`（含传递依赖，启动器安装的就是它）；
  核心依赖在 **Python 3.11 上实测安装通过**；**Python 3.13 已在 CI（GitHub Actions, Ubuntu）上用同一份锁定集
  真实安装并跑通冒烟测试** —— run [37215141304](https://github.com/whyao56/WingMan/actions/runs/37215141304)，
  `3.11` 与 `3.13` 两个 job 均 success（更早的证据是 `pip --dry-run --only-binary=:all:` 解析，记在
  `requirements.lock.txt` 的 "Resolution re-checked" 一段）；**开发机（中文 Windows）没有 3.13 解释器，
  本机仍未在 3.13 上真跑过**。`backend/requirements.txt` 保留为人类可读的依赖下限声明。
- 控制台输出统一为 UTF-8（65001）：启动器负责切换，并在**所有退出路径**恢复你原来的代码页；
  面向用户的 `.cmd` / `.bat` 是纯 ASCII、PowerShell 引导脚本是 UTF-8 带 BOM；终端渲染不了中文时自动降级为纯 ASCII 输出。
- README 的「快速开始」重写为使用者的 3 步；架构、合规、踩坑记录等既有内容完整保留。

### 修复

- 修复中文 Windows（GBK 控制台）下执行 `python tests/test_smoke.py` 会崩溃的问题
  （`UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`）。
- 调整 Mock 模式下「测试连通」的返回：现在直接说明当前是内置演示引擎、并指引去哪里接真实模型
  （此前会返回一条与故障难以区分的错误信息）。
- 修复控制台中文乱码。

### 已知限制

- **真实通话采集与 ASR 未做端到端验证**：需要用户在自己的设备上确认；
  录音与数据处理边界以 [docs/COMPLIANCE.md](docs/COMPLIANCE.md) 为准。
- **不做自动回复 / 自动代聊**：系统只产出建议，发送始终由用户本人确认。
- 前端仍是单文件 `frontend/index.html`，零构建，没有工程化重写（见 ROADMAP 阶段 2）。
- 仅在中文 Windows + Python 3.11 上做人工验收（真装 + 端到端跑通）；macOS / Linux 桌面未做人工验收。
- Python 3.13 的结论来自 CI、不是本机：`CI` 工作流（Ubuntu）已在真实环境执行并通过 ——
  run [37215141304](https://github.com/whyao56/WingMan/actions/runs/37215141304) 的 `3.11` / `3.13` 两个 job 均 success
  （用 `backend/requirements.lock.txt` 真实安装，并跑通冒烟测试与编码防护步骤）；
  **开发机没有 3.13 解释器，本机未在 3.13 上真跑过**。
