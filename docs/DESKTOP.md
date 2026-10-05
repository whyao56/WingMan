# 打包成桌面程序（Windows）

把 WingMan 做成一个**双击就能用的 exe**，对方不需要装 Python、不需要联网
（除非要用云模型），拿到一个文件夹就能跑。

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
| 体积（实测） | **78 MB** | **246 MB** |
| 文字链路（记忆 / 分析 / 建议 / 推演） | ✅ | ✅ |
| 云 ASR | ✅ 填密钥即用 | ✅ |
| 本地 ASR（音频不出本机） | ❌ 库里没带 | ✅ 另需下载模型权重 |
| 适用 | 大多数用户 | 明确要用本地转写的人 |

体积差主要在 `faster-whisper` 拖进来的三块（实测）：
`PyAV 65MB` + `ctranslate2 59MB` + `onnxruntime 36MB` ≈ 160MB。

> `onnxruntime` 是 Silero VAD 用的，`PyAV` 是音频解码用的 —— 即使我们把
> `vad_filter=False`、只喂 numpy 数组，它们仍会被 `faster_whisper` 顶层导入，
> 删不掉。删了就是「打得开、一录就崩」。

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
- [ ] 故意把引擎设成 `local` + 一个没下载的模型，确认自检报 **WARN**
      而不是绿的「已完成」
- [ ] 日志文件有内容且中文不乱码
- [ ] 整个 `dist/WingMan` 拷到另一台没装 Python 的机器上能跑
- [ ] `git status` 里没有 `dist/`、`*.db`、`models/`
