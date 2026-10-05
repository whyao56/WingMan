# 排错手册

先做这几件事，能解决大半问题：

1. 在仓库根目录跑一次体检：`wingman.cmd --doctor`（PowerShell 里写 `.\wingman.cmd --doctor`）。
   它**不修改配置、不建库、不动你的数据、也不启动服务**（只体检；唯一会留下的东西是 Python 自动生成的
   字节码缓存 `__pycache__`），逐项报出：仓库完整性、Python 版本与位数、解释器路径、虚拟环境、
   依赖锁与依赖完整性、数据目录、端口，最后给一句结论（体检通过 / 体检未通过：N 项前置问题 / 依赖尚未就绪：N 项 / 数据目录/路径有问题：N 项）。
   如果端口被占用，它会给出占用进程的 PID 与进程名，并提示用 `wingman.cmd --port 8788` 换端口
   （如果占用的就是你自己已经启动的 WingMan，忽略这条即可 —— 浏览器直接打开就能用）。
2. 想知道更细的（项目结构、数据库各表、示例数据、每个配置键的来源），
   用独立自检脚本 `scripts\preflight.py`，见第 9 节。
3. 对照下面的**退出码**找到对应的那一节（个别故障的退出码会随入口变化，例如 §5 的数据目录不可写；
   那种情况以输出里那一行是不是 `✗` / `[阻断]` 为准，比只看退出码可靠）。

| 退出码 | 含义 | 有副作用吗 |
|---|---|---|
| `0` | 成功 | — |
| `1` | 其他错误（未预期的异常） | 视情况 |
| `2` | **前置自检拒绝**：环境不满足，服务没有启动（端口被占用、Python 低于 3.11、参数写错都算这一类） | 没有（不会建环境、不改数据） |
| `3` | **依赖/环境准备失败**：装依赖这一步没过；或 `--doctor` 体检出必须先修的项（如数据目录不可写）。服务没有启动 | 可能留下不完整的环境，重跑即可自愈 |

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

## 5. 启动器说数据目录不可写

**现象**：提示数据目录不可写 / 无法创建 `backend\data`；也可能是 `backend\data` 被误建成了**同名文件**。

**先说退出码**：这一节的退出码**不是固定值**，取决于「故障形态」和「你跑的入口」，别只凭退出码对号入座。
实测（Windows 11 + Python 3.11，两种形态分别测过）：

| 你跑的入口 | `backend\data` 是文件 | 目录在、但 ACL 拒绝写入 |
|---|---|---|
| `wingman.cmd --doctor` | `3` | `3` |
| `wingman.cmd`（直接启动） | `1` | `1` |
| `scripts\preflight.py` | `2` | `2` |

原因是三个入口的职责不同：`--doctor` 把这一类问题归到「数据目录/路径」（不算依赖问题），
结论行给「✗ 数据目录/路径有问题：1 项」并以 `3` 结束（它的下一步提示明确写着这类问题 `--setup-only` 修不了）；
直接启动时是**服务进程自己起不来**（内部退出码 3），启动器把它统一记成 `1`（其他错误）；
`scripts\preflight.py` 则把它算作阻断项，以 `2` 结束。

所以**判断依据看输出本身，而不是退出码**：

- `--doctor`：数据目录那一行会区分形态 —— `backend\data 存在但不是目录（同名文件？）` 或 `目录存在但不可写（只读 / 权限 / 被占用）`，
  并各跟一条「修：…」建议；结论行给「✗ 数据目录/路径有问题：N 项」，下一步提示是「确认 `backend\data` 是目录且有写权限……（这类问题 `--setup-only` 修不了）」；
- `preflight.py`：会打 `[阻断]` 并细分原因 —— `存在但不是目录（是文件）` 或 `不可写：PermissionError`，还给出对应的修复建议；
- 直接启动：会打 `✗ 服务进程提前退出（退出码 3）`，并在「常见原因」里列出「数据目录不可写」。

**只要数据目录那一行是 `✗`（不论写的是「存在但不是目录」还是「不可写」），就按下面处置，不用纠结退出码是多少。**

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

## 7. Mock 的输出像废话 / 建议不痛不痒

**现象**：能用，但分析、建议看起来像模板。

**原因**：这是**预期行为**。默认的 Mock 是规则引擎，它只用来验证流程跑通，不懂语义。

> 顺带一提：默认 Mock 下点「设置 → 测试连通」，它会直接告诉你当前是演示引擎，而不是报错：
> `✓ mock — 演示引擎（内置规则）已就绪：不需要 API Key，输出仅用于跑通流程、看效果。要接真实模型：……`
> 所以「测试连通 ✓」不等于接上了真模型 —— **判断依据是 provider 名字**（`mock` 还是 `openai_compat` / `ollama`）。详见 [MODELS.md](MODELS.md)。

**处置**：接真模型，见 **[MODELS.md](MODELS.md)**。接好后用 ROADMAP 的命中率口径自查：拿 20 个你实际回复过的片段，看引擎给的选项里有没有你当时真选的那条，**超过 50%** 才算真的在理解（[ROADMAP.md](ROADMAP.md) 最后一节）。

## 8. 上传中文名的聊天记录文件（现在可以直接传）

**结论先说**：中文名文件**现在可以正常导入** —— [QUICKSTART.md](QUICKSTART.md) 第 3 步就是让你传
`samples\qq_sample_小鹿.txt`，端到端检查（导入 → 索引 → 画像 → 分析 → 建议 → 推演）实测全绿。

**早期版本**在 multipart 的 filename 里处理非 ASCII 有问题，选中文名文件会报错或解析失败；
如果你遇到的是那种错误，说明你手上的版本比较旧，拉一份最新代码即可。

**万一在你的环境里仍失败**（极少见，通常是本地代理/中间件改写了请求头），兜底做法：

```bat
REM 把文件复制一份改成英文名再上传（内容不受影响）
copy "samples\qq_sample_小鹿.txt" sample.txt
```

或者用「导入」页下半部分的「**或者直接粘贴文本**」：格式是每行 `2024-01-01 12:00:00 昵称`，下一行是内容，消息之间空一行。

## 9. 想手动自查：`--doctor`、独立自检脚本 与 健康接口

有三条自查通道，用途不同，别搞混。

### 9.1 `wingman.cmd --doctor`（启动器体检，最常用）

不修改配置、不建库、不动数据、不启动服务，只体检（唯一会留下的东西是 `__pycache__` 字节码缓存）。真实输出长这样：

```
════════════════════════════════════════════════════════════════
 WingMan 一键启动器
════════════════════════════════════════════════════════════════
      仓库目录        : <你的解压目录>
      操作系统        : Microsoft Windows NT 10.0.x
      控制台编码      : UTF-8 / chcp 65001（原 936，退出时恢复）

· 体检（不修改配置、不建库、不动你的数据、不启动服务）
      唯一会留下的是 Python 自动生成的 __pycache__ 字节码缓存

      仓库完整性      : backend/app/main.py、frontend/index.html、requirements.lock.txt 都在
      探测结果：
        py -3     → Python 3.11.9
      Python          : 3.11.9（py -3，64 位）
      解释器路径      : C:\Windows\py.exe
      虚拟环境        : 存在，Python 3.11.9（<仓库>\backend\.venv\Scripts\python.exe）
      依赖锁          : 25 个精确版本（backend\requirements.lock.txt）
      依赖完整性      : 与锁文件一致，且 app.main 可导入（25 个包）
      数据目录        : 可写（<仓库>\backend\data），数据库：已存在
      端口            : 127.0.0.1:8787 可用

✓ 体检通过：可以直接运行 wingman.cmd 启动服务
```

（措辞与行数随版本和你机器上的情况变化；例如 32 位 Python、未在实测范围内的 Python 版本会变成「提醒」，提醒不阻断启动。）

退出码：`0` = 环境就绪；`2` = 有阻断项必须先修（**端口被占用也算 2**）；
`3` = 体检有必须先修的项（依赖没装齐、虚拟环境不完整，或**数据目录/路径有问题**这类环境问题 ——
`--doctor` 会把数据目录问题单独报成「数据目录/路径有问题：N 项」并给出对症建议，只有依赖问题才提示 `--setup-only`）；`1` = 其他错误。

### 9.2 `scripts\preflight.py`（独立自检脚本，适合脚本化）

这是**另一套独立实现**（启动器的 `--doctor` 并不调用它），逐项检查项目结构、Python 版本、后端依赖、数据目录、
数据库、端口、前端控制台、示例数据与配置/Provider，并给出修复建议。适合放进脚本或 CI 里消费：

```bat
REM 机器可读的 JSON（stdout 是纯 ASCII JSON，人类摘要走 stderr）
backend\.venv\Scripts\python.exe scripts\preflight.py --json

REM 服务已经在跑、只想跳过端口检查
backend\.venv\Scripts\python.exe scripts\preflight.py --port 0

REM 仓库被放在别处时指定根目录
backend\.venv\Scripts\python.exe scripts\preflight.py --project-dir D:\somewhere\WingMan
```

它的退出码只有 `0` / `2` / `1`（没有 `3` —— 它不负责装环境）。
`--json` 里能拿到每一项检查的 `id` / `status` / `blocking` / `detail` / `hint`，以及 `env_files_hit`（命中了哪些 `.env`）、
`runtime_override_keys` 与各 provider 的可用性。

### 9.3 健康接口（服务运行中，看运行期实际生效的东西）

**`GET /api/health`** —— 最小契约，只有四个字段：`version` / `db` / `counts` / `providers`。
判断「现在跑的是不是真模型」就用它（见 [MODELS.md](MODELS.md) 第 2 节）：

```bat
curl -s http://127.0.0.1:8787/api/health
```

```powershell
curl.exe -s http://127.0.0.1:8787/api/health
```

> 这个端点**故意保持最小、不许膨胀**：`scripts/e2e_check.py` 依赖它的契约。

**`GET /api/health/details`** —— 结构化自证报告（只读）。想知道「每个配置到底从哪来」就查它：

```bat
curl -s http://127.0.0.1:8787/api/health/details
```

```powershell
curl.exe -s http://127.0.0.1:8787/api/health/details
```

里面有：`paths`（project / backend / data / frontend / samples / db）、
`env_files` 与 `env_files_hit`（实际命中了哪些 `.env`）、
`runtime_override_keys`（控制台写入的运行时覆盖 —— **只列键名，不含值**）、
`config`（每个配置键的 `source`：`runtime` / `env` / `dotenv` / `default`，以及掩码后的值）、
`providers`（各 provider 的 `kind` / `name` / `available` / `note`）、
`db`（路径、大小、各表计数）、`notes`（人可读的提醒）与 `ok`。

> 关于密钥：以上任何输出里**都不会出现密钥明文**。密钥一律以**掩码**出现 ——
> 长密钥形如 `sk-a******xyz`；在 `/api/health/details` 与自检输出中，**8 个字符以内的密钥固定显示为 `***`，长度也不外泄**；
> 另有 `xxx_set` / `"set": true` 之类标志表示「配没配」。所以 `--doctor`、`--json` 与两个健康端点的输出都能直接贴给别人看。

## 10. 想彻底重来（重置环境 / 清空数据）

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

## 11. 本机 stub / Ollama 明明在跑，应用却报 502（系统代理劫持了回环请求）

**现象**：离线 stub 或本地 Ollama 已经启动、浏览器/curl 直接访问它是好的，但 WingMan 一调用就失败：

- 错误里出现 `网关错误（502）`、`原始响应：`（后面还是空的），或者
- 模型端点在控制台里显示 `available: false` / 「测试连通」失败，而服务本身确实在跑。

**原因**：Python 的 HTTP 客户端（httpx）默认会**继承系统代理**。Windows 上这个代理可能只存在于
**Internet 选项 / 注册表**里，环境变量里根本看不到 —— 所以「我没设 HTTP_PROXY」并不代表没有代理。
一旦代理生效，发往 `127.0.0.1` 的请求也会被交给代理去转发，代理转发不到就回一个空的 502，
于是你看到的是「网关错误」，而不是「连接被拒」。本机代理软件开**全局模式**、或公司统一配了代理时最容易踩。

怎么确认是不是这条：

```powershell
python -c "import urllib.request; print(urllib.request.getproxies())"
```

输出里出现 `http://127.0.0.1:7897` 之类的地址（而 `echo %HTTP_PROXY%` 是空的），就基本可以确定。

**处置**（任选一种）：

1. 把回环地址排除在代理之外（推荐，一次设置对当前终端有效）：

```bat
set NO_PROXY=127.0.0.1,localhost,::1
wingman.cmd
```

```powershell
$env:NO_PROXY="127.0.0.1,localhost,::1"
.\wingman.cmd
```

  想长期生效：Windows「设置 → 网络和 Internet → 代理 → 手动设置代理」里的**例外/绕过列表**加上
  `127.0.0.1;localhost;::1`，或在「环境变量」里新建用户变量 `NO_PROXY`。

2. 把代理软件的**全局模式换成规则/直连模式**（多数代理工具默认会直连本机地址，全局模式则会接管）。

**这一版做了什么**：应用已经把**回环地址**排除在代理之外（`127.0.0.1` / `localhost` / `::1` / `127.*`
的请求一律直连，非回环地址仍照常走代理）。所以升级到本版后，指向本机 stub 与 Ollama 的请求不会再被劫持。

**仍然需要注意**：如果你用的是**非回环**的自建中转（例如公司内网 `http://10.0.0.8:8000/v1`、
局域网的 Ollama `http://192.168.1.10:11434`），它仍然按「外部地址」处理、会走代理 ——
这种地址被代理挡住或转发不到时，请用上面的处置办法把它加进例外列表。

> 同一类坑项目里早有先例：`scripts\e2e_check.py` 用 `trust_env=False`，`scripts\preflight.py` 的端口探测
> 用空的 `ProxyHandler`，启动器探测服务是否就绪时也把 `$req.Proxy` 置空 —— README 的常见问题里也提过一次。

---

## 还有问题？

请按这个格式反馈（信息越全，定位越快）：

1. 你的系统版本 + `python -V` 的输出；
2. `wingman.cmd --doctor` 的完整输出；
3. 出错的**完整命令**与**完整报错**（不要只贴最后一行）；
4. 用的哪个终端（cmd / PowerShell / Windows Terminal）。

相关文档：[QUICKSTART.md](QUICKSTART.md)（3 步上手）、[MODELS.md](MODELS.md)（接模型）、[COMPLIANCE.md](COMPLIANCE.md)（隐私与红线）。
