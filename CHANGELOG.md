# 更新日志

本文件格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

下一个版本的计划见 [docs/ROADMAP.md](docs/ROADMAP.md)（阶段 1「能用」/ 阶段 2「好用」）。
目前最大的缺口是 **Prompt 调优**：默认的 Mock 引擎让整条链路能跑，但建议内容还是空的。

### 新增

- **采集聊天记录**（新页面）：不用先导出文件，直接从客户端拿记录。两条并列的通道 ——
  **自动采集**（读客户端本地库）与**半自动采集**（选中消息复制，程序读剪贴板）。
  - `backend/app/collect/` 新增一整层：`detect`（探测进程 / 版本 / 数据目录）、
    `matrix`（**版本支持矩阵，写进软件而不是 README**）、`keys`（取密钥）、
    `sqlcipher`（解密）、`reader`（认列 + 读消息）、`pipeline`（六道关编排）、
    `clipboard` + `semi`（半自动通道）、`winapi`（进程 / 版本信息 / 剪贴板）。
  - `backend/app/api/routes_collect.py`：17 条接口。`/preview` 走完整链路但**不写库**；
    `/semi/*` 是半自动采集的生命周期、轮询与待确认队列。
  - 前端新增「采集」页，四张卡片对应四个问题：① 我的版本支不支持
    ② 自动采集能不能跑 ③ 采不动怎么办 ④ 采到哪了。配图 `docs/images/ui-collect.png`。
  - 数据模型：消息与导入完全同构，只多两个标记 —— `chats.source`
    （`import` / `collect`）与 `messages.ts_source`（`exact` / `assumed` / `manual`）。
    「时间推定」必须能被查出来、筛掉、在界面上标出来，否则真时间轴上混进假时间，
    「她平时几点找我说话」这类结论就是错的。
- `backend/tests/test_collect_cipher.py`：**按 SQLCipher 规格自己造库再解回来**。
  真库密钥拿不到的时候，这是唯一能证明解密器写对的验证方式；顺带钉住
  「错密钥一个都不能放过」和「认证失败返回 `None` 而不是垃圾」。
- `backend/tests/test_collect_clipboard.py`：剪贴板解析。覆盖 QQ 多选复制主形态
  （每条消息前面一个「称呼 + 空格 + 时间」）、`称呼:` 形态、认不出说话人时必须拒绝、
  正文里的日期不许被当成消息时间、长名字不能被前缀短名字吃掉。
- `backend/tests/test_collect_semi.py`：半自动采集。归属有依据就直接落库、
  没依据就挂起、`apply_to_rest` 批量、`assumed` 时间要标出来、防重只在时间不可信时启用、
  **不复用已有的人就会多出一个同名的人**。
- `backend/tests/test_collect_api.py`：接口契约。含一项真的写系统剪贴板的端到端
  （默认跳过，要显式开 `WINGMAN_E2E_CLIPBOARD=1`）。
- `backend/tests/test_collect_offwindows.py`：**采集层在非 Windows 上必须能被导入**。
  这条守卫是踩出来的（见「修复」），做法是模拟那个环境：改掉 `sys.platform`、
  给 `builtins.__import__` 打补丁让取 `ctypes.wintypes` 一律失败，
  然后把采集层每个模块**逐个**重新导入 —— 逐个而不是只导入口，是为了出问题时
  能直接点名是哪个文件，而不是给一个「反正整层都导不进来」的结论。
- `backend/app/api/routes_persons.py`：人物实体、渠道归属、跨渠道时间线
  （「以人为中心」的数据层与接口）。
- `scripts/verify_migration.py`：在**真实数据库的副本**上验证结构迁移，全程不碰真库。
- `backend/tests/test_config_contract.py`：盯住 `.env.example` 与 `Settings` 字段的一致性。
  这次撤下语音时 `config.py` 删干净了、测试也全绿，**但 `.env.example` 把 14 个失效的键
  继续留在那里** —— 照着示例配环境的人会以为功能还在，填了不报错也不生效。
  纯文本文件没人 import，删字段时最容易被忘，所以用测试把「代码里的字段」当基准比对。

### 修复

- `/api/collect/matrix` 返回列表但响应注解写成了 `dict`，FastAPI 会按注解校验返回值，
  接口实际是 500 —— 在界面上表现成「点了没反应」。这类错只有真发一次请求才发现得了。
- 重置采集游标时，`account` 对不上就静默删 0 条，接口照样返回成功：
  用户点「重置」以为好了，下次采集还是从老位置继续。改为允许不传 `account`
  （表示「这个平台上这个人的游标全重置」），并**如实返回删了几条**。
- `/api/collect/cursors` 一度同时注册在 `routes_persons.py` 和 `routes_collect.py`。
  运行时先注册的那个生效、后一个完全收不到请求 —— 改哪一份都不一定有用，
  而读代码看不出来（两处都在）。现在只留 `routes_collect.py` 一处。
- `/preview` 会跑到「在进程内存里搜密钥」那一步，默认预算 60 秒，用户点一下要等一分钟
  才看到结论。预演压缩到 12 秒，并把这件事写进报告的 `notes` 里 ——
  不然用户会把「预演取不到密钥」当成「这条路彻底走不通」。
- 半自动采集整块内容都是重复时，`skipped_duplicate` 计数走不到（提前返回了），
  而那一刻恰恰是用户最需要看到「刚才那次复制没进库」的时候。
- 半自动采集只填了称呼、库里已有同名的人时，会按「对方称呼」再新建一个人 ——
  采一次多一个「小鹿」，而这两个小鹿的消息永远检索不到一起。
  改为先按名字/别名**精确**复用已有的人（不做模糊匹配，合并永远由用户显式发起）。
- **采集层在非 Windows 上「导入即失败」**：`winapi.py` 和 `clipboard.py` 顶上各有一行
  模块级的 `from ctypes import wintypes`，而 `ctypes.wintypes` 里的 `VARIANT_BOOL`
  用了 `'v'` 类型码 —— 那个码只有 Windows 版的 `_ctypes` 认识，于是 Linux 上直接抛
  `ValueError: _type_ 'v' not supported`。它跟有没有调用无关，属于**模块级语句**。
  CI 跑在 Ubuntu 上、采集的路由又会把整条链拉起来，所以表现是
  **本地 Windows 全绿、一推上去整个测试矩阵全红**，而报错跟采集看起来毫无关系；
  更麻烦的是它有**两处**，只改一处不够。
  现在由 `winapi.py` 统一决定 `wintypes` 是什么：Windows 上用真身，真身不存在才退到
  一份最小替身（**Windows 上导不进来会直接 re-raise，不许用替身把真故障盖过去**），
  `clipboard.py` 从它取 —— 只留一份实现。
  新增 `test_collect_offwindows.py` 钉住它，并附一条反向用例防止守卫自己变成空测。
- `Store.reset_cursor` 增加 `messages.ts_source` 迁移：老库自动补 `exact`，
  保证「原来的记录都是真时间」这个事实不被新字段的默认值说反。
- （0.2.x 遗留）`scripts/preflight.py` 里两个检查项 `check_optional_soundcard` /
  `check_optional_faster_whisper` 在语音撤下后仍注册着，`--doctor` 会继续提示用户
  「未安装 soundcard（可选）」。改为**数据驱动**的单一 `check_optional_modules`：
  注册表 `OPTIONAL_MODULES` 为空时明确输出「当前版本没有需要额外安装的可选依赖」，
  将来加可选依赖只要往注册表加一项。同时去掉 `bootstrap.ps1` 里恒为 `false` 的
  `WithAsr` / `NeedAsr` 空开关 —— 一个永远不会通的开关，只会让下一个人以为功能只是没开。
- （0.2.x 遗留）`docs/TROUBLESHOOTING.md` 删掉一节后章节号顺移，`docs/MODELS.md` 里
  「见第 10 节 / 第 12 节」的跨文件引用随之失准，已同步修正。

### 移除

- **通话语音转写整体撤下**，改为「规划中」。上一版做出来了，但延迟、双方串音、
  断句切碎都还不够好；而要对齐这些，最省事的做法是把通话音频送到云端 ——
  对一个「本地优先」的工具，这个代价不划算。所以不留半成品占位置：
  - 后端删掉整个 `app/asr/`（识别 / 双通道采集 / 模型管理）、`api/routes_voice.py`、
    `routes_asr_models.py`、`routes_asr_probe.py`、`app/bus.py`（只为转写 SSE 存在的
    进程内事件总线）、`voice_log` 表、`AudioDevice` / `TranscriptSegment` / `VoiceStatus`
    三个模型，以及 `config.py` 里全部 `asr_*` / `whisper_*` / `vad_*` / `audio_*` / `hf_endpoint` 配置；
  - 删掉 `requirements-asr.txt`、`scripts/asr_bench.py`、`--with-asr` / `--asr-test`
    两个命令行入口，以及 `WINGMAN_WITH_ASR` 构建开关；
  - 前端撤下设置页的「语音识别」卡片、自检页的「语音链路实测」卡片和通话页的全部操作 UI；
    「通话」页改为一份「规划中」说明（保留 5 条设计思路 + 一张静态效果预览，无任何按钮），
    侧栏该导航项挂上「敬请期待」标签；
  - 打包从两个包合并为一个：原来「完整版」比标准版多出的 91 MB 全是本地语音依赖，
    撤下后它没有存在理由了，下载页不再需要用户纠结选哪个。
- 语音的**设计思路完整保留**在「通话」页、[README](README.md) 与
  [docs/ROADMAP.md](docs/ROADMAP.md)，不是删掉了想法，只是没做完不放出来。

### 说明

- **自动采集的密钥在本机这两个版本上取不到**，而且是有依据的取不到：实测
  QQ NT 9.9.20.37051 / 微信 4.1.13.12，内存里的十六进制候选全部校验失败（微信 0 处），
  二进制滑窗穷举按可读内存推算单线程上千小时；按 salt 收缩到 88 MB 后
  8,784 万候选 × 2 套参数各跑 419 秒（约 21 万/秒）仍未命中。
  所以半自动采集是**并列的一等公民**，不是「失败后的安慰」。
  完整实测记录见 `backend/app/collect/keys.py` 的模块文档。
- **版本指引写进软件、不写进 README**：文档不会知道用户装的是哪个版本。
  支持矩阵是 `collect/matrix.py` 里的数据（含「我实测过的版本」），
  界面把探测结果和矩阵合起来给结论 + 下一步动作，四档结论每一档都带照做的事。
- **工作区清理（不影响仓库内容）**：`build/` 下 33 个一次性侦察脚本与原始日志
  （探测、内存嗅探、密钥穷举、UIA 试探等）已从工作目录删掉、另存备份。
  它们**从来没有进过仓库**（`build/` 下只有 `wingman.spec` 是跟踪的），
  所以这里不计入「移除」—— 但结论必须留下：已写进 `collect/keys.py` 与
  `collect/matrix.py` 的模块文档里。已搬进测试套件的两个：
  `_selftest_cipher.py` → `test_collect_cipher.py`、`_probe_clip.py` → `test_collect_clipboard.py`。

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
