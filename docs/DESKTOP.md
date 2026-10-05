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
| 体积 | 仓库本身几 MB | 约 68 MB |
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
└── .env                 可选的手工配置
```

> 想彻底卸载：删掉这个文件夹 + 程序目录即可。**没有注册表、没有后台服务。**

**第一次打开显示的「还有 N 项没配好」是正常的**，不是报错。那是因为：
默认用的是演示引擎、还没导入聊天记录。照着「自检」页逐条点即可，
每配好一项就会变成绿色「已完成」。

---

## 二、给开发者的话（怎么构建）

```bash
# 在仓库根目录
python scripts/build_exe.py              # 构建
python scripts/build_exe.py --zip        # 顺手压成 zip
```

产物在 `dist/WingMan/`。**分发时给整个文件夹**，不是只给那个 exe ——
`_internal/` 里是 Python 运行时和依赖，缺了跑不起来。

### 只有一个包

**只有 `WingMan-{version}-win64.zip` 一个包**，不再分「标准版 / 完整版」，
没有 `--with-asr`，也没有相关的环境变量。

**语音识别 / 通话转写已经整体撤下，改为「规划中」**，所以打包时不再需要
为它准备任何东西。PyInstaller 的 `excludes` 里已经固化排除了
`faster_whisper` / `ctranslate2` / `tokenizers` / `onnxruntime` / `av` / `soundcard`，
既减小体积，也免去「打进去却用不上」的死重量。

体积（实测）约 **68 MB**。

### 构建开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
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

### 7. Python 里的 `"/tmp/xxx"` 不是 Git Bash 的 `/tmp`

Windows 上 `Path("/tmp/a.wav")` 会解析成 `C:\tmp\a.wav`，而 Git Bash 的
`/tmp` 实际指向 `C:\Users\<用户>\AppData\Local\Temp`。写测试脚本时踩了这个，
表现为「文件明明存在，程序却报『参数错误』」。

用 `tempfile.gettempdir()` 拿真实临时目录即可。

### 8. 「静态分析看不见」只对**字符串动态导入**成立，别推广到函数内的 import

第 2 条说的 uvicorn 坑，很容易被误记成「函数内的 import 也看不见」—— 不是的。
PyInstaller 用字节码分析，**函数内写的 `from X import Y` 照样能追到**。
唯一追不到的是「模块名是运行时拼出来的」那种（`importlib.import_module(name)`）。

这个区别很实际：上一轮就因为它差点做错一个判断。有个模块的导入是写在
函数里的，我据此推断它没被打进包、准备去「修」—— 但用归档清单核对后发现，
它本来就在包里（还带着一百多个子模块）。

**核对包内容别靠猜，也别靠 grep 二进制里的字符串**（docstring 里的模块名
一样会被打进去，数出来的是假阳性）。用 PyInstaller 自己的读取器看真实 TOC：

```python
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
r = CArchiveReader("dist/WingMan/WingMan.exe")
open("pyz.pyz", "wb").write(r.extract("PYZ.pyz"))     # 真正的模块归档在里层
names = sorted(ZlibArchiveReader("pyz.pyz").toc)      # 这才是权威清单
```

### 9. 手工验证时，先确认「你连的是自己刚起的那个进程」

窗口版为了做单实例，会在启动时探测目标端口上是否已有 WingMan；有就直接复用、
**不起新服务**。这在日常使用里是对的，但做验证时是个陷阱：如果你机器上还留着
早前的服务（源码模式跑 `wingman.cmd`、或上一次没关的窗口），你新起的 exe 会
静默退出，而你的 `curl` 打到的是**那个旧进程** —— 数据目录、配置、模型全是旧的，
得出一堆看似矛盾的结论。

所以验证时：换一个没人用的端口，并核对响应里的数据目录是不是你预期的那一个。
顺带一提，`--no-window` 下的日志现在会明说「不打开界面」，不再一律印
「直接打开界面」—— 之前那句误导过排查。

### 10. 发版时的两个「静默失效」

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

### 11. 用户说「排版全乱了」时，先查同名 CSS 类有没有互相覆盖

这一轮收到「字体排版挤在一起很乱」，第一反应是字号和行距的问题，
实际原因完全不在这里：**一个类名被两个模块占了**。

`.sub` 既是各页标题下的副标题段落，又是通话页预览里的字幕行。字幕行那段写着
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

### 12. 文案指错路径，和功能坏掉给人的感觉一样

发布前核对时发现：自检报告其实写在**数据目录**
`%LOCALAPPDATA%\WingMan\logs\selfcheck.txt`，但 zip 里的使用说明、README、
发行说明三处都说它写在「程序目录」—— 而程序目录下压根没有 `logs\` 这个文件夹。

后果不是崩溃，是**用户按说明去找，找不到那个文件**，然后得出
「自检根本没跑 / 这软件坏了」的结论。功能是好的，说明书把人带沟里了。

三个教训：

1. 凡是印给用户的路径，都要真去走一遍。这回是写文案时凭印象写的。
2. 对外文案里出现「程序目录」这种相对说法时，必须补上完整路径 ——
   用户不知道你说的「程序目录」是他解压到的那个文件夹，还是别的地方。
3. 加了守卫测试（`backend/tests/test_version.py`）：所有提到 `selfcheck.txt`
   的文档里，该文件名之前不能出现「程序目录」。

> 写上面这段记录时，守卫测试又失败了一次 —— 因为我引述错误原文时把它照抄了进来，
> 而测试分不清「引述」和「真的指错」。所以这里改成转述：
> 引述一段错误文案，同样会踩中针对这段文案的守卫。有意思的是，这说明那条测试确实在干活。

同类的还有一个，顺手记下：`scripts/make_release.py` 自己的 `--version` 默认值
原本是硬编码的 `0.2.0`。这个脚本存在的意义就是「别让版本号漂移」，
它自己的默认值却会第一个过期 —— 而且过期得静默（不加 `--version` 就会打出
一个版本号与代码不符的包）。现在改成从 `backend/app/__init__.py` 里读。

---

## 四、发布检查清单

发版前过一遍：

- [ ] `python scripts/build_exe.py` 构建通过
- [ ] `WingMan.exe --check` 自检全绿或只有预期内的 WARN
- [ ] 双击能开窗口、能导入记录、能出建议与推演
- [ ] **关掉再开，数据还在**（这条最容易被忽略，也最致命）
- [ ] 日志文件有内容且中文不乱码
- [ ] 整个 `dist/WingMan` 拷到另一台没装 Python 的机器上能跑
- [ ] 源码路线也没被改坏：`wingman.cmd --doctor` 仍然正常，`wingman.cmd --setup-only`
      建的 venv 与 `requirements.lock.txt` 一致
- [ ] `git status` 里没有 `dist/`、`*.db`

### 发版（把 exe 发到 GitHub）

exe **不进仓库**：`dist/` 是 gitignore 的。走 Release 附件 —— 可下载、有稳定 URL、可校验。

- [ ] `backend/app/__init__.py` 里的 `__version__` 已提到本次要发的版本
      （忘了改的后果：exe 自检报旧版本号，用户报 bug 时说不清装的是哪个包。
      `backend/tests/test_version.py` 会盯住 CHANGELOG / README / 发行说明 / 下载链接四处）
- [ ] 写好 `docs/releases/v<tag>.md`：解压注意什么、校验和、**诚实的限制清单**
- [ ] **把 zip 解压到仓库之外、且改过名的目录再跑一次** `--check`
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
