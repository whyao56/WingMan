# 排错手册

先做两件事，能解决大半问题：

1. 在仓库根目录跑一次自检：`wingman.cmd --doctor`（PowerShell 里写 `.\wingman.cmd --doctor`）。
   它逐项检查 11 项（项目结构、Python 版本、后端依赖、数据目录、数据库、端口、前端控制台、示例数据、语音可选依赖、配置与 Provider），
   每项给出 `[通过]` / `[提醒]` / `[阻断]` 和结论行，需要修的直接跟一句「修复建议」。
   > 这是**启动前**检查：如果服务已经在运行，它会如实报告「端口被占用」，并告诉你可以直接打开浏览器用（不必再起一个）。
2. 对照下面的**退出码**找到对应的那一节。

| 退出码 | 含义 | 有副作用吗 |
|---|---|---|
| `0` | 成功 | — |
| `1` | 其他错误（未预期的异常） | 视情况 |
| `2` | **前置自检拒绝**：环境不满足，服务没有启动（端口被占用、Python 低于 3.11、参数写错都算这一类） | 没有（不会建环境、不改数据） |
| `3` | **依赖准备失败**：装依赖这一步没过，服务没有启动 | 可能留下不完整的环境，重跑即可自愈 |

> 怎么看退出码：启动器窗口通常不会一闪而过。如果你是从自己的终端跑的，
> cmd 里执行 `echo %errorlevel%`，PowerShell 里执行 `$LASTEXITCODE`。

---

## 1. 启动器说端口被占用（退出码 2）

**现象**：提示端口 8787（或你指定的端口）已被占用，然后退出；服务没起来。

**原因**：上一次的 WingMan 没关干净，或者别的程序占着这个端口（8787 是常见的中转/调试端口）。

**处置**：三选一。

```bat
REM ① 换个端口启动
wingman.cmd --port 8899

REM ② 看看是谁占着（记下最后一列的 PID）
netstat -ano | findstr :8787

REM ③ 确认那个 PID 不是你要用的程序，再结束它
taskkill /PID 12345 /F
```

> 换成 8899 之后，浏览器要访问 `http://127.0.0.1:8899`，不是 8787。
> 另外：`wingman.cmd --setup-only` **不检查端口**（它只准备环境、自己不占端口），所以服务正跑着的时候，也可以用它单独修复或补装依赖。

## 2. 启动器说 Python 版本太低 / 找不到 Python（退出码 2）

**现象**：提示需要 Python 3.11 或更高版本，或者提示找不到 `python`。

**原因**：没装 Python、装了但没加进 PATH、或版本低于 3.11。

**处置**：

1. 先确认装了什么：

```bat
python -V
```

2. 版本低于 3.11 或提示找不到 → 装一个 Python 3.11+：

```bat
winget install Python.Python.3.11
```

也可以去 <https://www.python.org/downloads/windows/> 下载安装包，安装时**务必勾选 “Add python.exe to PATH”**。

3. 装完**关掉并重开一个终端**（PATH 变了旧终端不认），再跑 `python -V` 确认版本，然后重新运行 `wingman.cmd`。

> **常见坑**：在 cmd 里敲 `python` 却弹出微软应用商店 —— 那是 Windows 自带的「应用执行别名」占位。
> 打开「设置 → 应用 → 高级应用设置 → 应用执行别名」，把 `python.exe` / `python3.exe` 两项关掉。

## 3. 依赖装不上（退出码 3）：网络慢、超时、走代理

**现象**：卡在「安装依赖」很久，然后报错退出；提示里有 `Timeout`、`ConnectionError`、`Read timed out`、`SSL` 等字样。

**原因**：默认从 PyPI 官方源下载，国内网络经常慢或断；公司网络可能还要走代理。

**处置**（都在**运行启动器的同一个终端**里先设好变量，再运行启动器）：

换国内镜像 —— cmd：

```bat
set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
wingman.cmd
```

换国内镜像 —— PowerShell：

```powershell
$env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
.\wingman.cmd
```

走公司代理 —— cmd（把地址换成你自己的）：

```bat
set HTTPS_PROXY=http://proxy.example.com:8080
set HTTP_PROXY=http://proxy.example.com:8080
wingman.cmd
```

走公司代理 —— PowerShell：

```powershell
$env:HTTPS_PROXY="http://proxy.example.com:8080"
$env:HTTP_PROXY="http://proxy.example.com:8080"
.\wingman.cmd
```

其他要点：

- **直接重跑一次**往往就好（临时网络抖动）。依赖装到一半失败不会留下坏环境，启动器会接着装。
- 上面的变量只对当前这个终端窗口有效，关掉就没了 —— 这是故意的，不会污染系统设置。
  想永久改 pip 源，用 `backend\.venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`，撤销用同样的命令把 `set` 换成 `unset`。
- 手动补装（环境已经建好、只是想重装依赖）：

```bat
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock.txt
```

> `backend\requirements.lock.txt` 是**精确版本锁定**（含传递依赖），启动器安装的就是它；
> `backend\requirements.txt` 只是人类可读的依赖下限声明。两个文件都在仓库里，别搞混。

## 4. 控制台中文乱码 / 显示成方块或问号

**现象**：启动器或脚本输出的中文是乱码，或者显示成 `锟斤拷`、`????`、方块。

**原因**：中文 Windows 默认代码页是 GBK（936），而程序的输出可能是 UTF-8。

**处置**：

- 编码策略（实测）：面向用户的 `.cmd` / `.bat` 脚本是**纯 ASCII**（cmd 的 `echo` 按控制台代码页原样输出字节，
  同一份字节在 cp936 与 cp65001 下必有一个乱码，所以中文统一交给 PowerShell 脚本打印）；
  PowerShell 脚本是 **UTF-8 带 BOM**（Windows PowerShell 5.1 在无 BOM 时会按 ANSI(936) 解析源文件，中文字面量直接变乱码）；
  依赖锁定文件也是纯 ASCII（pip 在 Windows 按 locale 解码 requirement 文件）。
- 启动器运行时会**主动把控制台切到 UTF-8（65001）**，并在**所有退出路径**（正常结束、异常、Ctrl+C）恢复你原来的代码页；
  `wingman.cmd` 在 PowerShell 退出后再兜底恢复一次。所以正常情况不会乱码。
- 但如果你的终端太旧、或字体不支持中文，还是可能显示成方块/问号（**这是终端渲染能力的问题，不是编码错误**）：
  - 推荐用 **Windows Terminal**，或直接开 PowerShell / cmd 的新窗口再跑一次；
  - 手动切代码页：`chcp 65001`（想切回去用 `chcp 936`）；
  - 换一个带中文字形的等宽字体（如「更纱黑体」「微软雅黑」）；
  - 启动器在三种情况下会自动降级成**纯 ASCII 输出**（全英文、0 个非 ASCII 字节）：没有控制台句柄、切不到 65001，
    或者你显式设了 `WINGMAN_ASCII=1`（想强制英文界面就用它）。
- **不要**把输出重定向到不支持 UTF-8 的老编辑器里看，那会看不出真实编码。
- 想把输出存成文件，**用 cmd 重定向**：`cmd /c "wingman.cmd --doctor > doctor.txt"` 得到的是干净的 UTF-8；
  PowerShell 5.1 的 `>` 会用切换前的编码解码子进程输出，文件反而会乱码（这是 PS 的已知行为，不是启动器的问题）。

> 如果你在 Windows 11 上仍然看到乱码，请把「退出码 + 你用的终端 + `chcp` 的输出」一起报 issue，这是我们需要修的场景。

## 5. 启动器说数据目录不可写（退出码 2）

**现象**：提示数据目录不可写 / 无法创建 `backend\data`。

**原因**：目录权限被安全软件或系统策略拦住；也可能 `backend\data` 被误建成了**同名文件**。

**处置**：

1. 看清楚提示里的路径（一般是 `<仓库目录>\backend\data`）。
2. 确认它是**目录**而不是文件：

```bat
dir backend
```

如果 `data` 是个文件，删掉它，再重新运行启动器：

```bat
del backend\data
wingman.cmd
```

3. 如果是权限问题，看看 ACL：

```bat
icacls backend\data
```

把目录（或整个解压目录）从「只读/受限」位置移出来，比如放到 `D:\WingMan`、`%USERPROFILE%\WingMan` 这类你自己的目录下，比放在 `C:\Program Files` 下省事得多。

## 6. 浏览器没自动打开 / 页面显示「后端未连接」

**现象**：启动器窗口里已经在滚日志，但浏览器没弹出来；或者页面打开了，左下角却是「后端未连接」。

**处置**：

- 手动访问启动器打印的地址（默认 `http://127.0.0.1:8787`）。用 `--no-browser` 时本来就不会自动打开。
- 用了 `--port 8899` 就要访问 8899。
- 页面「后端未连接」通常是服务已经退出：回到启动器窗口看最后几行报错，按上面几节处理。
- 浏览器缓存问题：`Ctrl+F5` 强刷一次。
- 端口被防火墙拦（少见）：确认你访问的是 `127.0.0.1` 而不是本机对外 IP。

## 7. 「通话」页说没有检测到音频设备

**现象**：进「通话」页，提示「没有检测到音频设备 —— 可能没装 soundcard，或系统没有可用设备」。

**原因**：语音是**可选**能力，默认没有安装采集依赖。

**处置**：

1. 装上语音依赖：

```bat
wingman.cmd --with-asr
```

（等价的手动方式：`backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-asr.txt`）

2. 「系统回环」通道是跟着**默认播放设备**走的：确认系统默认扬声器就是你现在实际在用的那个，再点「刷新设备」。
3. **没有麦克风也能用**：在「通话」页的实时字幕区域，用最上面的输入框手动输入一句话，选「对方说」或「我说」，点「注入」——用于演示实时字幕链路。
4. 录音涉及法律问题：采集必须是你在页面上手动点「开始采集」才会开始，请只用于个人备忘。

## 8. Mock 的输出像废话 / 建议不痛不痒

**现象**：能用，但分析、建议看起来像模板。

**原因**：这是**预期行为**。默认的 Mock 是规则引擎，它只用来验证流程跑通，不懂语义。

> 顺带一提：默认 Mock 下点「设置 → 测试连通」会显示 `✗ mock — Mock Provider 不认识任务标记 [TASK:UNKNOWN]…`。
> 那是 Mock 的正常反应（它只认引擎内部的任务标记），**不代表环境坏了**；换成真模型后才会变成 ✓。详见 [MODELS.md](MODELS.md)。

**处置**：接真模型，见 **[MODELS.md](MODELS.md)**。接好后用 ROADMAP 的命中率口径自查：拿 20 个你实际回复过的片段，看引擎给的选项里有没有你当时真选的那条，**超过 50%** 才算真的在理解（[ROADMAP.md](ROADMAP.md) 最后一节）。

## 9. 上传中文名的聊天记录文件失败

**现象**：选 `samples\qq_sample_小鹿.txt` 上传时报错，或提示解析失败。

**原因**：个别 HTTP 客户端在 multipart 请求里处理非 ASCII 文件名有问题。

**处置**：把文件复制一份，改成英文名再上传（内容不受影响）：

```bat
copy "samples\qq_sample_小鹿.txt" sample.txt
```

或者用「导入」页下半部分的「**或者直接粘贴文本**」：格式是每行 `2024-01-01 12:00:00 昵称`，下一行是内容，消息之间空一行。

## 10. 想手动自查：`--doctor` 与 `/api/health`

**`wingman.cmd --doctor`**：不启动服务，只做检查。输出长这样：

```
============================================================
WingMan 启动前自检 · preflight
============================================================
仓库目录  ：<你的解压目录>
Python    ：3.11.9 · ...（项目虚拟环境）
控制台    ：cp936（取决于你的终端）
待查端口  ：8787（默认值）

[ 1/11] 项目结构              [通过] backend/app/main.py 与 backend/requirements.txt 都在
[ 2/11] Python 版本           [通过] 3.11.9（需要 >= 3.11）· 项目虚拟环境
...
[11/11] 配置与 Provider      [提醒] 配置来源：.env 0 个、运行时覆盖 0 项 · provider：llm=mock；embedder=hash；asr=mock
           修复建议：当前是默认的 Mock 配置：…… 要接真实模型，见 docs/MODELS.md …
------------------------------------------------------------
提醒（不阻断启动，N 项）
...
结论：可以启动（11 项检查：8 通过 / 3 提醒 / 0 阻断）
下一步：在仓库根目录运行 wingman.cmd；需要语音能力就加 --with-asr。
------------------------------------------------------------
```

> 上面的数字随你的机器变化：没装可选语音依赖、或还是默认的 Mock 配置，都会多出几条「提醒」——**提醒不阻断启动**，只有「阻断」项才需要先修。

退出码：`0` = 环境就绪；`2` = 有阻断项必须先修（**端口被占用也算 2**）；`3` = 虚拟环境/依赖还没准备好（按提示先跑 `wingman.cmd --setup-only`）；`1` = 自检自身没跑完。
（直接跑下面的 `scripts\preflight.py` 时只有 `0` / `2` / `1` —— 它不负责装环境，所以没有 `3`。）

**进阶用法**（也可以直接用项目虚拟环境里的 Python 跑自检脚本，拿到机器可读输出）：

```bat
REM 机器可读的 JSON（纯 ASCII，任何代码页下都能解析；人类摘要走 stderr）
backend\.venv\Scripts\python.exe scripts\preflight.py --json

REM 服务已经在跑、只想跳过端口检查
backend\.venv\Scripts\python.exe scripts\preflight.py --port 0

REM 仓库被放在别处时指定根目录
backend\.venv\Scripts\python.exe scripts\preflight.py --project-dir D:\somewhere\WingMan
```

`--json` 里能拿到每一项检查的 `id` / `status` / `blocking` / `detail` / `hint`，以及 `env_files_hit`（命中了哪些 `.env`）、
`runtime_override_keys`（控制台写入的运行时覆盖）和各 provider 的可用性 —— 这就是「配置来源」的可机器读版本。

**`GET /api/health`**（服务运行中，看运行期实际生效的东西）：

```bat
curl -s http://127.0.0.1:8787/api/health
```

```powershell
curl.exe -s http://127.0.0.1:8787/api/health
```

返回版本、数据库文件路径、消息/事实/会话计数，以及每个 provider 的 `name` / `available` / `note`（运行期实际用的是谁，一眼可见）。

> 关于密钥：自检与健康输出里**不会出现密钥明文**。配置里的密钥一律以**掩码**出现
> （长密钥形如 `sk-a******xyz`，短密钥统一显示为 `***`，长度也不外泄），
> 另有 `xxx_set` / `"set": true` 之类的标志表示「配没配」。所以 `--doctor`、`--json`、`/api/health` 的输出可以直接贴给别人看。

## 11. 想彻底重来（重置环境 / 清空数据）

- **只重置运行环境**（依赖装乱了、想从头装）：

```bat
rmdir /s /q backend\.venv
wingman.cmd
```

PowerShell：

```powershell
Remove-Item -Recurse -Force backend\.venv
.\wingman.cmd
```

- **清空记忆与配置**（聊天记录、画像、运行时设置都会没）：删掉 `backend\data\wingman.db`。
  注意这个库同时保存了你在控制台「设置」里填的内容，删了就回到 `.env` / 默认值。
- **清空一切**：直接把整个解压目录删掉，重新从 zip 解压一次。

---

## 还有问题？

请按这个格式反馈（信息越全，定位越快）：

1. 你的系统版本 + `python -V` 的输出；
2. `wingman.cmd --doctor` 的完整输出；
3. 出错的**完整命令**与**完整报错**（不要只贴最后一行）；
4. 用的哪个终端（cmd / PowerShell / Windows Terminal）。

相关文档：[QUICKSTART.md](QUICKSTART.md)（3 步上手）、[MODELS.md](MODELS.md)（接模型）、[COMPLIANCE.md](COMPLIANCE.md)（隐私与红线）。
