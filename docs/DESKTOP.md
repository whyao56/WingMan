# 打包成桌面程序（Windows）

把 WingMan 做成一个**双击就能用的 exe**，对方不需要装 Python、不需要联网
（除非要用云模型），拿到一个文件夹就能跑。

---

## 零、先搞清楚：exe 和 wingman.cmd 是两条不同的路

仓库里有两个「一键启动」，很容易搞混。它们解决的是**不同的前置条件**：

| | `wingman.cmd`（源码路线） | `WingMan.exe`（本文档） |
|---|---|---|
| 需要装 Python | ✅ 3.11+ | ❌ 完全不需要 |
| 首次启动 | 几分钟（建 venv + 装依赖） | 秒级 |
| 要不要联网 | 首次装依赖要 | 不要（云模型除外） |
| 体积 | 仓库本身几 MB | 68 MB / 237 MB |
| 改了代码 | 直接生效 | 要重新打包 |
| 启动前自检 | `wingman.cmd --doctor` | `WingMan.exe --check` |
| 适合 | 自己开发、能装 Python | 只想用 / 分发给不懂技术的朋友 |

**结论**：给自己用、要改代码 → 用 `wingman.cmd`。
要给别人用、或者这台机器装不了 Python → 打包成 exe。

两条路的自检脚本**检查项不完全相同**：`--doctor` 还会查 Python 版本、位数、
venv 完整性、依赖锁一致性（这些在 exe 里根本不存在），
`--check` 则更关注 exe 特有的东西（资源路径、可写目录、原生窗口依赖）。
这也是它们没有合并成一个的原因。

---

## 一、给使用者的话（怎么用）

1. 拿到 `WingMan` 文件夹，整个放到任意位置（桌面、D 盘都行，**不需要安装**）
2. 双击 `WingMan.exe`
3. 程序会自己起服务并弹出窗口，关闭窗口即退出

**你的数据在哪**：`%LOCALAPPDATA%\WingMan\`（在资源管理器地址栏粘贴即可打开）

```
%LOCALAPPDATA%\WingMan\
├── data\wingman.db      你的聊天记录、画像、设置
├── logs\wingman.log     运行日志（出问题先看这个）
├── logs\selfcheck.txt   最近一次自检报告
├── models\              本地语音模型（下过才有）
└── .env                 可选的手工配置
```

> 想彻底卸载：删掉这个文件夹 + 程序目录即可。**没有注册表、没有后台服务。**

### 语音不工作时怎么办

界面上「自检 → 语音链路实测」点一下，它会把中间每一步摊开给你看：

```
[OK  ] 设备: 找到 4 个设备（含 2 个回环）
[FAIL] 录音: 电平 RMS=0.0009，几乎是静音。设备选对了但没收到声音
       —— 检查麦克风是否被静音、音量是否为 0。
[OK  ] 引擎: local · 本地 faster-whisper · tiny · cpu/int8
[WARN] 转写: 没识别出任何文字
```

**语音出问题时，现象永远是同一句「没反应」**，但原因可能是：没装采集库、
没找到麦克风、麦克风被静音、环境太吵、模型没下载、引擎还是 Mock……
这六种的解法完全不同。这一屏就是用来把「没反应」拆成「第几步不行」。

没有图形界面时（例如远程排查），命令行等价物是：

```bash
WingMan.exe --asr-test                    # 从麦克风录 5 秒并识别
WingMan.exe --asr-test 录音.wav            # 转写指定文件
WingMan.exe --asr-test --seconds 8         # 录 8 秒
WingMan.exe --asr-test --source loopback   # 录「系统正在放的声音」= 听对方那条路
WingMan.exe --check                       # 只跑环境自检
```

`--source loopback` 是验证「听对方」最直接的办法：让系统放一段视频或音乐，
同时跑上面这条命令，看能不能识别出正在播的内容。实测：

```
[音频] 录制设备：Speaker (Realtek(R) Audio)
[音频] 录制 8.0s @ 16000Hz（来源：系统声音）
[音频] 电平 RMS = 0.0722
[引擎] local · 本地 faster-whisper · tiny · cpu/int8
[文本] 周末要不要一起去看展我请你喝奶茶
结论：链路正常
```

结果同时写进 `logs/asr_test.txt`，可以直接发给别人看。

**第一次打开显示的「还有 N 项没配好」是正常的**，不是报错。那是因为：
默认用的是演示引擎、还没导入聊天记录、语音还没接。照着「自检」页逐条点
即可，每配好一项就会变成绿色「已完成」。

---

## 二、给开发者的话（怎么构建）

```bash
# 在仓库根目录
python scripts/build_exe.py              # 标准版（推荐分发用）
python scripts/build_exe.py --with-asr   # 完整版（内置本地语音识别）
python scripts/build_exe.py --zip        # 顺手压成 zip
```

产物在 `dist/WingMan/`。**分发时给整个文件夹**，不是只给那个 exe ——
`_internal/` 里是 Python 运行时和依赖，缺了跑不起来。

### 两个版本的区别

| | 标准版 | 完整版（`--with-asr`） |
|---|---|---|
| 体积（实测） | **68 MB** | **237 MB** |
| 文字链路（记忆 / 分析 / 建议 / 推演） | ✅ | ✅ |
| 云 ASR | ✅ 填密钥即用 | ✅ |
| 本地 ASR（音频不出本机） | ❌ 库里没带 | ✅ 另需下载模型权重 |
| 适用 | 大多数用户 | 明确要用本地转写的人 |

体积差主要在 `faster-whisper` 拖进来的三块（实测）：
`PyAV 67MB` + `ctranslate2 59MB` + `onnxruntime 36MB` ≈ 160MB。

> `onnxruntime` 是 Silero VAD 用的，`PyAV` 是音频解码用的 —— 即使我们把
> `vad_filter=False`、只喂 numpy 数组，它们仍会被 `faster_whisper` 顶层导入，
> 删不掉。删了就是「打得开、一录就崩」。

**`hf_xet` 被显式排除了**（在 `build/wingman.spec` 的 `excludes` 里）。
它是 `huggingface_hub` 的可选下载加速后端，被自动探测到就会跟着进包 ——
在标准版里占 9MB，约 12%。但我们无条件设了 `HF_HUB_DISABLE_XET=1`，
它永远不会被 import，所以是纯死重量。排除它反而更稳：包里有它时，
一旦哪条路径漏设那个环境变量，就可能重新踩上「Xet 的 CAS 服务器国内被拦 → 401」
这个坑；包里没有它，`is_package_available("hf_xet")` 直接为假，自动回落经典 HTTP 下载。

**模型权重两个版本都不打包** —— 那是另外 78–3090 MB，让用户按需下载更合理，
而且要带提示（见下）。

**`soundcard`（音频采集）两个版本都打进去**。很容易误以为「只有本地转写才需要
录音」—— 不是的：走云 ASR 同样要先用它抓系统回环/麦克风，再把音频发给云端。
少了它，两条语音路线一起废。它本身很小，没有理由不打。

### 构建开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `WINGMAN_WITH_ASR` | `0` | 是否打进本地语音识别 |
| `WINGMAN_WITH_WEBVIEW` | `1` | 是否带原生窗口（关掉则退回开浏览器，省约 40MB） |

---

## 三、踩过的坑（都是打包特有的，记下来免得重踩）

### 1. 数据目录必须从程序目录挪走

源码跑的时候数据在 `backend/data/`，看起来没问题。但打包后：

- onefile 模式每次启动解压到**随机临时目录**，退出即删 → 用户导入的记录每次都没了
- 就算 onedir，程序装在 `Program Files` 下也是只读的 → 写入直接失败

所以冻结态一律写 `%LOCALAPPDATA%\WingMan`。判断依据是 `sys.frozen`，
见 `backend/app/config.py` 的 `_writable_root()`。

### 2. uvicorn 的实现类是运行时按字符串挑的

```python
# uvicorn 内部大致是这么干的，静态分析看不见
module = importlib.import_module(f"uvicorn.protocols.http.{auto}")
```

结果就是**源码跑得好好的，exe 一启动就 ImportError**。必须在 spec 里
显式列 `hiddenimports`（见 `build/wingman.spec`）。

### 3. 资源路径不能用 `__file__` 往上找

冻结后 `__file__` 指向的位置毫无意义。一律读 `sys._MEIPASS`
（PyInstaller 注入），见 `config.py` 的 `_resource_dir()`。

### 4. `--noconsole` 会把所有错误变成「双击没反应」

这是打包体验上最坑的一条：开发时你能看到 traceback，打包后什么都没有。

对策是**给每条错误路径都留出口**：

- 日志写文件：`logs/wingman.log`（滚动，2MB × 3）
- 启动异常弹原生对话框，并给出日志路径
- `WingMan.exe --check` 把自检报告写到 `logs/selfcheck.txt`，用户可以直接发给你

### 5. 冻结后 stdout 会掉回 Windows 本地代码页

中文机器上是 GBK，日志里的中文全变乱码。`_setup_logging()` 里显式
`reconfigure(encoding="utf-8", errors="replace")`。

### 6. exe 会被 SmartScreen 拦

未签名的 exe 首次运行会弹「Windows 已保护你的电脑」。这不是 bug，
是 Windows 对没有代码签名证书的程序的默认行为。

消掉的唯一正规办法是买代码签名证书（一年几百到上千元）。
不买的话，给使用者的说明里写一句「点『更多信息』→『仍要运行』」即可。

### 7. 别用 `available` 判断「能不能用」，要用 `ready`

这是本项目里最典型的一类错误，单独记一笔。

最初 `ASREngine.available` 的语义是「这个引擎对象构造得出来」。对本地
whisper 来说，只要 `faster-whisper` 库在，它就是 `True` —— **哪怕模型权重
一个字节都还没下载**。自检面板当时就用它判断，于是：

```
用户：选「本地模型 large-v3」→ 没下载权重 → 自检显示绿色「已完成」
       → 用户以为配好了 → 录音后什么都没发生 → 不知道卡在哪
```

现在把两个概念分开：

| | 含义 | 用途 |
|---|---|---|
| `available` | 引擎对象构造得出来 | 工厂选择实现类 |
| `ready` | **现在拿一段音频进来就能出字** | 自检面板、界面状态 |

`LocalWhisperASR.ready` = 库在 **且** 权重已下载。`not_ready_reason`
再补一句人话（「模型 large-v3 还没下载（已下载：tiny、small）」），
自检直接展示。

> 教训：状态判断要问「**能不能真的干活**」，而不是「**对象有没有造出来**」。
> 这两者的差集，正是「看着配好了、用起来没反应」的全部来源。

### 8. 录音采集是**两条语音路线的公共依赖**

写 spec 时第一反应是「`soundcard` 只有本地转写才需要，放进 `WITH_ASR` 分支吧」。
这是错的 —— 走云 ASR 时同样要先用 `soundcard` 抓系统回环/麦克风，
再把音频 POST 给云端。两类包都必须带，否则标准版的语音会整条废掉。

另外它出现的位置是**函数内延迟导入**（为了「没装也能跑」）：

```python
def _soundcard():
    try:
        import soundcard as sc   # 函数内 import
    except ImportError:
        return None
```

PyInstaller 的字节码分析**确实**能扫到函数内的 import，所以它「碰巧」被打进去了。
但这是运气 —— 哪天把导入挪进 `try/except` 或换成 `importlib`，就会静默丢包，
而症状是「打包版能启动，但一录音就报没装库」。所以 spec 里显式列了
`hiddenimports += ["soundcard", "soundcard.mediafoundation"]`。

### 9. 「int16 量级的 float32」——最阴的一个坑

测试脚本里写了：

```python
pcm = np.frombuffer(raw, np.int16).astype(np.float32)   # 忘了 / 32768
```

`dtype` 是 `float32` 了，但**数值还在 ±32768**。而当时的 `transcribe()`
只按 `dtype` 判断要不要归一化：

```python
if pcm.dtype == np.int16:
    pcm = pcm / 32768.0      # float32 直接跳过这一步
```

于是送进 Whisper 的波形被放大了 3 万倍。结果是：

```
实际说的：今天加班到十点，好累啊，你周末有空吗
识别出来：玩玩玩玩玩玩!
```

**不报错、不警告，只是给你错结果。** 排查时一度怀疑是 tiny 模型太弱，
跑了英文对照实验 —— 英文也是乱的，才确认是管线问题而不是模型问题。

修法是 `asr/base.py` 的 `to_float_mono()`：不只看 `dtype`，还用**峰值兜底**
（float32 但峰值 > 1.5 一律按 int16 量级处理）。修完后三种输入形式
（已归一化 float32 / 未归一化 float32 / 原始 int16）输出逐字一致：

| 模型 | 识别结果 | 置信度 |
|---|---|---|
| tiny | 今天加班到10点好累呀你周末有空吗 | 0.62 |
| small | 今天加班到10点,好累呀,你周末有空吗? | **0.78** |

> 教训：跨模块传数组时，「*dtype 对*」不等于「*量纲对*」。
> 接口契约要写清值域（这里是 ±1.0），并且**用数值特征做兜底校验**，
> 而不是只信类型标签。

### 10. Python 里的 `"/tmp/xxx"` 不是 Git Bash 的 `/tmp`

Windows 上 `Path("/tmp/a.wav")` 会解析成 `C:\tmp\a.wav`，而 Git Bash 的
`/tmp` 实际指向 `C:\Users\<用户>\AppData\Local\Temp`。写测试脚本时踩了这个，
表现为「文件明明存在，SAPI 却报『参数错误』」。

用 `tempfile.gettempdir()` 拿真实临时目录即可。

### 11. 「静态分析看不见」只对**字符串动态导入**成立，别推广到函数内的 import

第 2 条说的 uvicorn 坑，很容易被误记成「函数内的 import 也看不见」—— 不是的。
PyInstaller 用字节码分析，**函数内写的 `from X import Y` 照样能追到**。
唯一追不到的是「模块名是运行时拼出来的」那种（`importlib.import_module(name)`）。

这个区别很实际：本次就因为它差点做错一个判断。`models.py` 里
`from huggingface_hub import snapshot_download` 是写在函数里的，我据此
推断它没被打进包、准备去「修」—— 但用归档清单核对后发现
`huggingface_hub` 有 147 个子模块，本来就在包里。

**核对包内容别靠猜，也别靠 grep 二进制里的字符串**（docstring 里的模块名
一样会被打进去，数出来的是假阳性）。用 PyInstaller 自己的读取器看真实 TOC：

```python
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
r = CArchiveReader("dist/WingMan/WingMan.exe")
open("pyz.pyz", "wb").write(r.extract("PYZ.pyz"))     # 真正的模块归档在里层
names = sorted(ZlibArchiveReader("pyz.pyz").toc)      # 这才是权威清单
```

### 12. 手工验证时，先确认「你连的是自己刚起的那个进程」

窗口版为了做单实例，会在启动时探测目标端口上是否已有 WingMan；有就直接复用、
**不起新服务**。这在日常使用里是对的，但做验证时是个陷阱：如果你机器上还留着
早前的服务（源码模式跑 `wingman.cmd`、或上一次没关的窗口），你新起的 exe 会
静默退出，而你的 `curl` 打到的是**那个旧进程** —— 数据目录、ASR 引擎全是旧的，
得出一堆看似矛盾的结论（「明明打的是完整版，怎么 ASR 是 mock？」）。

所以验证时：换一个没人用的端口，并核对响应里的数据目录是不是你预期的那一个。
顺带一提，`--no-window` 下的日志现在会明说「不打开界面」，不再一律印
「直接打开界面」—— 之前那句误导过排查。

### 13. 发版时的两个「静默失效」

发版不像写代码，错了不会报错，只会**悄悄出错**。这一轮撞到两个：

**一是校验和不可复现。** zip 条目默认记录文件 mtime，而压缩包里的
「使用说明.txt」是打包时当场生成的 —— 时间就是那一刻。于是**同样的代码重打一次，
字节就变了，SHA256 也跟着变**。后果不是失败，而是发行说明里印的校验和
**永远复现不出来**：用户认真去核对，反而会以为自己下到了被篡改的包。
修法是打包时把时间戳钉死、把遍历排序，让「校验和」成为可被独立验证的事实，
而不是一次性的快照。现在 `make_release.py --check` 连跑两次得到完全相同的 SHA256。

**二是版本号会四处漂移。** `__version__` 写在一个文件里，但还会出现在
CHANGELOG 章节名、README 的「当前版本」、发行说明的文件名、下载链接里的 tag
—— 四处都是手写的。这次就撞上了：exe 自检打印 `v0.1.0`，而我准备发的 tag 是 `v0.2.0`。
不崩溃，但用户报 bug 时说不清装的是哪个包。
这类「到处都要改、漏一处也不报错」的字段靠人记是靠不住的，
所以加了 `backend/tests/test_version.py`：四处任一脱节就测试失败。

### 14. 用户说「排版全乱了」时，先查同名 CSS 类有没有互相覆盖

这一轮收到「字体排版挤在一起很乱」，第一反应是字号和行距的问题，
实际原因完全不在这里：**一个类名被两个模块占了**。

`.sub` 既是各页标题下的副标题段落，又是通话页的字幕行。字幕行那段写着
`display:grid; grid-template-columns:66px 1fr` —— CSS 后定义者胜，
于是**每个页面的副标题都被压成 66px 宽的一列**，一个词一行。
两处单独看都没毛病，也不报任何错，只有把页面真的渲染出来才看得见。

修法两条：字幕行改名 `.cap`；再加一条守卫测试，
禁止同一份 CSS 里出现两个顶层同名类规则（`test_frontend_assets.py`）。
要复用样式，请把选择器用逗号合并成一条 `.a,.b{...}`。

同类的静默失效还有一个，顺手记在这里：**「云模型填了也连不上」**。
预设里的 DeepSeek 模型名 `deepseek-chat` 在 2026-07-24 被官方下线，
照着填必然 400 —— 但界面上完全看不出「这个预设已经过期了」。
现在预设给的是当前可用的名字，并且读到退役名会当场提示、一键替换。

---

## 四、本地语音模型为什么让用户自己下

`faster-whisper` 的权重放在 HuggingFace 上，`small` 就要约 486 MB。
所以设计成：

1. 程序里列清楚每个规格的体积和定位，用户自己选
2. 下载有真实进度条（按目录体积对比预期体积估算）
3. **支持切国内镜像** —— `huggingface.co` 在国内经常超时，
   界面上勾一下就走 `hf-mirror.com`

对应代码：`backend/app/asr/models.py` 与 `backend/app/api/routes_asr_models.py`。

> 没有模型时直接加载会静默卡住好几分钟，用户只会以为程序死了。
> 所以 `LocalWhisperASR._get_model()` 会先检查模型在不在，不在就明确报错
> 让他去下载。

---

## 五、发布检查清单

两条语音路线都验过一遍的清单（标准版 + 完整版各走一次）：

- [ ] `python scripts/build_exe.py` 构建通过
- [ ] `WingMan.exe --check` 自检全绿或只有预期内的 WARN
- [ ] 双击能开窗口、能导入记录、能出建议与推演
- [ ] **关掉再开，数据还在**（这条最容易被忽略，也最致命）
- [ ] `WingMan.exe --asr-test` 能录到声音并识别出文字
- [ ] 「自检 → 语音链路实测」在界面里点得通
- [ ] 走一遍「云 ASR」路线（填地址）和「本地模型」路线（下模型），
      确认自检面板对**两条路线**的提示都对
- [ ] **在打包版里真的下载一个模型**（`base` 约 145MB 最省事），
      确认「下载 → 加载 → 转写」整条走通 —— 光验「加载已有模型」不够：
      `local_dir` 下载下来的目录结构、以及被排除掉的 `hf_xet`，
      都只在这条路上才会暴露问题
- [ ] 用 `base` 或更大规格跑一句中文，确认**不吐繁体**
      （没带简体 `initial_prompt` 时 `base` 会输出「10點…嗎?」这种）
- [ ] 故意把引擎设成 `local` + 一个没下载的模型，确认自检报 **WARN**
      而不是绿的「已完成」
- [ ] 日志文件有内容且中文不乱码
- [ ] 整个 `dist/WingMan` 拷到另一台没装 Python 的机器上能跑
- [ ] 源码路线也没被改坏：`wingman.cmd --doctor` 仍然正常，`wingman.cmd --setup-only`
      建的 venv 与 `requirements.lock.txt` 一致
- [ ] `git status` 里没有 `dist/`、`*.db`、`models/`

### 发版（把 exe 发到 GitHub）

exe **不进仓库**：`dist/` 是 gitignore 的，且 GitHub 单文件限制 100MB
（完整版解压后 237MB 推不上去）。走 Release 附件 —— 可下载、有稳定 URL、可校验。

- [ ] `backend/app/__init__.py` 里的 `__version__` 已提到本次要发的版本
      （忘了改的后果：exe 自检报旧版本号，用户报 bug 时说不清装的是哪个包。
      `backend/tests/test_version.py` 会盯住 CHANGELOG / README / 发行说明 / 下载链接四处）
- [ ] 写好 `docs/releases/v<tag>.md`：两个包怎么选、解压注意什么、校验和、**诚实的限制清单**
- [ ] **把 zip 解压到仓库之外、且改过名的目录再跑一次** `--check` 与 `--asr-test`
      —— 我们平时都对着仓库内的 `dist/` 验证，而用户拿到的是解压到任意位置的副本。
      这一步才证明产物没有依赖仓库环境，也顺带验证压缩包顶层目录改名没把路径搞坏
- [ ] 打包可复现：`scripts/make_release.py --check` **连跑两次**，两次 SHA256 必须相同
      （zip 条目会记录 mtime，不钉死时间戳的话校验和每次都不一样，
      发行说明里印的校验和就永远复现不出来）
- [ ] 提交并推送后**再**建 tag —— tag 指向 `main` 的当前提交，
      先建 tag 会让它指向一个还没包含版本号提升的提交
- [ ] `python scripts/make_release.py --version X.Y.Z`（先 `--dry-run` 看一眼）
- [ ] 从**公开地址**（不带 token）下载一次附件，核对 SHA256 与本地一致
- [ ] 确认 CI 在 tag 指向的那个提交上是绿的
