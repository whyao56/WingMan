# 更新日志

本文件格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

下一个版本的计划见 [docs/ROADMAP.md](docs/ROADMAP.md)（阶段 1「能用」/ 阶段 2「好用」）。

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
  核心依赖在 **Python 3.11 上实测安装通过**；**Python 3.13 只做过依赖解析验证**
  （`pip --dry-run --only-binary=:all:`，结论记在 `requirements.lock.txt` 的 "Resolution re-checked" 一段），
  **没有在 3.13 上真实安装过**（开发机没有 3.13）。`backend/requirements.txt` 保留为人类可读的依赖下限声明。
- 控制台输出统一为 UTF-8（65001）：启动器负责切换，并在**所有退出路径**恢复你原来的代码页；
  面向用户的 `.cmd` / `.bat` 是纯 ASCII、PowerShell 引导脚本是 UTF-8 带 BOM；终端渲染不了中文时自动降级为纯 ASCII 输出。
- README 的「快速开始」重写为使用者的 3 步；架构、合规、踩坑记录等既有内容完整保留。

### 修复

- 修复中文 Windows（GBK 控制台）下执行 `python tests/test_smoke.py` 会崩溃的问题
  （`UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`）。
- 调整 Mock 模式下「测试连通」的返回：现在直接说明当前是内置演示引擎、并指引去哪里接真实模型
  （此前会返回一条与故障难以区分的错误信息）。
- 修复控制台中文乱码（包括 `.env.example` 等文本在 GBK 控制台下显示为乱码的问题）。

### 已知限制

- **真实通话采集与 ASR 未做端到端验证**：需要用户在自己的设备上确认；
  录音与数据处理边界以 [docs/COMPLIANCE.md](docs/COMPLIANCE.md) 为准。
- **不做自动回复 / 自动代聊**：系统只产出建议，发送始终由用户本人确认。
- 前端仍是单文件 `frontend/index.html`，零构建，没有工程化重写（见 ROADMAP 阶段 2）。
- 仅在中文 Windows + Python 3.11 上人工验收（真装 + 端到端跑通）；
  Python 3.13 只做到依赖解析验证，未真装；CI 工作流（Ubuntu）虽已配置 Python 3.11 / 3.13 冒烟矩阵，
  但本仓库尚未 push，**CI 还没有在真实环境下执行过** —— 其他平台与 3.13 结论均未验证。
