# 对象中心迭代计划（2026-10-05 需求）

> **给执行窗口的话**：先读 [STATUS.md](STATUS.md) 拿到现状，再读本文档。
> 本文档把用户提的 11 条功能 + 1 个 bug + 1 条外设需求，翻译成**具体到数据表 / 接口 / 界面 / 验收标准**的工作项。
> 凡是标注「**建议**」的地方是可以调整的设计选择；标注「**坑**」的地方是踩过或必然踩到的，别绕开。
> **纪律**：不确定就去看代码或问用户，**不要猜**；每个阶段跑全量测试并单独提交；**不要 push**（由复核后统一推）。

---

## 0. 这一轮要做成什么

一句话：**把「以会话为中心」改成「以对象为中心」**。
现在用户打开程序，左栏是一串「会话」；改完之后，左栏是一串**对象（人）** ——
一个人下面挂着他在微信、QQ 上的若干条记录，而他的人物设定、已记住的事实、聊天记录、采集记录、
历史输出全都在这个人身上。导航从 7 项收敛到 4 项。

三条贯穿性的原则（用户第 10 条「不要冗余、要有联动」的落地方式）：
1. **一个概念只在一个地方编辑**：会话信息 / 人物设定 / 已记住的事实 收进「对象」，不再散落在「记忆」页。
2. **能复用的不新造**：导航项减少靠「合并」不靠「隐藏」；同一个后端能力（如导入）并到采集的通道里，不复制一份。
3. **有动作就有痕迹**：指挥台的输出、导入、采集都要可回看（第 11 条）。

---

## 1. 需求逐条拆解

### 需求 1（核心）：以「对象」为中心

**用户原话**：「加一个可以『对象』的功能，要以『对象』为中心。WingMan 本身的一些功能冗余了，『对象』取代『会话』的位置，把『记忆』『会话』的一些功能整合，把『记忆』（『会话信息』、『人物设定』、『已记住的事实』）放到『对象』里，『聊天记录』可以二次编辑，删除、增加、修改等等。可以进行批量操作。把『采集』的『自动采集』做好，说清楚到底怎么操作，怎么『找到密钥』。」

**现状差距**（见 STATUS §2/§4）：
- `persons` 表是一等实体，后端 9 个接口齐全（含合并、渠道、跨渠道时间线），**前端只用了一个 `GET /api/persons`**。
- 左栏是 `#chatlist`（会话），指挥台/记忆都挂在会话上。
- `facts`、`personas` 都以 **chat_id** 为归属键 —— 人物级别的事实与设定**不存在**。
- store 层**没有**消息的 update/delete 方法；聊天记录页纯只读。
- 自动采集给用户的指引只有 `index.html:620-623` 两行。

**设计**

1) **概念映射（写进 ARCHITECTURE）**

| 界面词 | 代码实体 | 说明 |
|---|---|---|
| 对象 | `persons` | 一个人。跨平台、跨渠道唯一 |
| 渠道 / 记录 | `chats`（+`PersonChannel` 视图） | 这个人在某平台（或某次通话/当面）上的一段记录 |
| 记忆 | `facts` + 人物设定 | **上提到对象级**（见下） |

2) **数据模型改动**

- `facts` 加列 `person_id TEXT`（可空，保留 chat_id 以支持「渠道级事实」）：
  - 迁移：`ALTER TABLE facts ADD COLUMN person_id TEXT`，回填
    `UPDATE facts SET person_id = (SELECT c.person_id FROM chats c WHERE c.id = facts.chat_id)`
  - **坑**：`facts` 现有唯一键是 `(chat_id, subject, key, value)`，而 SQLite 里 **NULL 互不相等**，
    person 级事实（`chat_id` 为空）会失去唯一性保护 → 必须补一条部分唯一索引：
    ```sql
    CREATE UNIQUE INDEX IF NOT EXISTS ux_facts_person
      ON facts(person_id, subject, key, value)
      WHERE person_id IS NOT NULL AND (chat_id IS NULL OR chat_id = '');
    ```
    迁移前先清理已存在的重复行（保留 id 最小的一条），否则建索引会失败。
- **建议**：新增 `person_personas(person_id TEXT PRIMARY KEY, goal, my_style, peer_profile, taboos, stage, updated_at)`。
  现有 `personas`（chat 级）**保留**作「渠道级覆盖」，但界面只暴露对象级；迁移时把每个 chat 的 persona
  归并到它的 person（同一 person 多个 chat 都有 persona 时，**取 updated_at 最新的一条**并在报告里写明）。
- `summaries` **建议保持 chat 级**（L2 摘要是「这段对话最近发生了什么」，按渠道更自然）；
  对象级视图用它在 UI 上聚合展示即可，不为它加列。
- 消息增删改（**必须新增**）：
  - `store.update_message(msg_id, patch)` — 允许改 `text / ts / ts_source / sender / role`
  - `store.delete_messages(ids: list[int]) -> int`
  - `store.insert_manual_message(chat_id, ...)`（「增加一条」；走 `insert_messages` 也行，但要注意 `ext_id` 为空）
  - **坑**：幂等键是 `UNIQUE(chat_id, sender, ts, text)`。编辑后的行可能与另一条撞键 →
    `INSERT OR IGNORE` 不适用了，update 时用 `try/except sqlite3.IntegrityError`，
    返回结构化错误「改完和另一条完全一样（同会话+同发送者+同时间+同内容）」，让界面提示用户而不是 500。

3) **接口**（新增，尽量复用 `routes_persons.py` 已有风格）

| 方法 + 路径 | 作用 |
|---|---|
| `GET /api/persons/{id}/overview` | 对象详情聚合：基础字段 + 渠道列表 + 计数 + 最近消息 + 最近输出 + 未完成项（如没配模型） |
| `GET /api/persons/{id}/facts` `POST` `DELETE /{fid}` | 对象级事实 CRUD（含渠道级事实的合并视图，响应里标 `scope: person/chat`） |
| `GET/PUT /api/persons/{id}/persona` | 对象级人物设定 |
| `PATCH /api/messages/{msg_id}` | 改一条消息 |
| `DELETE /api/messages/{msg_id}` | 删一条消息 |
| `POST /api/messages/bulk` | 批量：`{ids[], action: delete\|set_role\|set_ts\|set_sender, value}` |
| `POST /api/chats/{chat_id}/messages` | 手动增加一条消息 |

批量接口要返回 `{changed, skipped, errors[]}`，前端据此提示「改了 N 条、跳过 M 条」。

4) **界面**
- 左栏 `#chatlist` → **对象列表**；标题从「会话 · 右键可设置」改成「对象」。
  每个对象下可展开列渠道（默认展开当前选中的）。
- 对象详情页（新）：Tabs = **概览 / 记忆 / 聊天记录 / 采集 / 历史**。
  - 记忆 = 人物设定（对象级）+ 已记住的事实（对象级/渠道级分组显示）
  - 聊天记录 = 可编辑表格：行内编辑、勾选、批量删除/改角色/改时间、手动加一条、按渠道筛选
  - 采集 = 这个对象的采集游标 + 半自动采集入口（跳到采集页并带上 person_id）
  - 历史 = 第 11 条的输出留存
- **原「记忆」页并入对象详情，原「会话」列表降级为对象下的渠道。**

5) **自动采集「说清楚怎么操作」**（同一个需求的后半句，别漏）
把 `index.html:620-623` 那两行扩成一个可展开的「怎么拿到密钥？」说明块，内容必须是**如实**的：
- 三种途径与代价：① 手动粘贴 64 位十六进制（最稳，当场校验）② 程序缓存复用（换库自动失效）
  ③ 内存扫描（两级：hex 秒级 / 穷举受预算，正式 60 s、预演 12 s，**到点收工 ≠ 没找到**）
- **必须照实写清**：实测本机 QQ NT 9.9.20.37051 / 微信 4.1.13.12 的自动取密钥**走不通**
  （数据源：`collect/keys.py:13-27` 的实测表），所以**「半自动采集」才是当下确定能跑通的通道**。
- 每一步都给出「你会看到什么」与「失败了下一步做什么」，并与 `/api/collect/matrix` 返回的
  `guide` 字段联动（矩阵里已经有每个客户端专属指引，不要另写一份静态文案）。

**验收**
- 左栏出现的是对象，不是会话；点对象能展开渠道。
- 对象详情里能改人物设定、增删事实、**编辑/删除/批量操作聊天记录**，刷新后仍在。
- 事实在对象级与渠道级都能看到且不重复计数。
- 采集页的「怎么拿到密钥」说明块能被一个没读过代码的人照做；且不出现「它一定能自动找到」这类不实暗示。

---

### 需求 2：优化「导入」并把它并进「采集」

**现状**：导入是独立导航项（`view-import` 543），三个接口（preview/import/import-text），已有预览→确认两步。
**设计**
- **并入**：采集页加第三张通道卡「手动导入」；导航去掉「导入」。导入的接口完全复用，不新造。
- **优化**（按价值排序，至少做前三条）：
  1. 支持拖拽到卡片 + 多文件批量
  2. 解析失败时给**行级原因**（第几行、哪一列对不上），而不是一句「解析失败」
  3. 导入结果明确「归属到哪个对象」，允许当场新建/选择对象（复用 `POST /api/persons` + `/channels`）
  4. 导入历史落 `activity_log`（第 11 条）
  5. 保留预览 → 确认两步，**不要**为了「简化」去掉预览（用户上传的是隐私数据，先看清再入库）
- 兼容：旧的 `?view=import` 深链接要重定向到采集页的导入通道，别留死链。

**验收**：导航里没有「导入」；采集页能完成从预览到入库的全流程；拖拽可用；失败能定位到行。

---

### 需求 3：半自动采集不记「抓取时刻」，改记消息的「回复时刻」

**现状**（STATUS §5.3）：剪贴板带了时间就用它（`ts_source='clipboard'`）；**没带**就用 `now`（抓取时刻）
顶替并标 `assumed`（`semi.py:233-240`）。真正的抓取时刻 `Capture.at` **只写进 `semi_state.json`，不进消息表**。

**设计**
1. 新增 `messages.captured_at TEXT`（=**采集时刻**，与消息时间分开存）；
   `schemas.Msg` 加字段 + `to_row()`；`store.insert_messages` 的 INSERT 补列；
   `semi._commit_capture`（`276-280`）把 `cap.at` 填进去；迁移函数加到 `_migrate_message_columns`（`store.py:338`）。
2. **`ts` 的语义明确为「这条消息在对话里发生/被回复的时刻」**，抓取时刻不再冒充它。
3. 强化「从复制文本里读出时刻」—— 这是「爬取回复时刻」的**唯一可靠来源**（`clipboard._parse_ts` `235`）。
   现在支持的格式有限，要扩到 QQ / 微信复制文本的常见时间头：
   - `2026-10-05 21:03:11`、`2026/10/5 21:03`、`10月5日 21:03`、`10-05 21:03`
   - `今天 21:03` / `昨天 21:03` / `周三 21:03` / `星期一 21:03`
   - `下午 9:03` / `上午 9:03` / `晚上 9:03`（12 小时制换算）
   - `[21:03]` / `21:03:11`（只有时分秒）
4. **只有时分（无日期）时**：用「同一会话里最近一条消息的日期」补全（`inferred`），
   跨天歧义（推断出的时刻比上一条早超过 N 小时）时**走 ask 挂起**，不要自己选。
5. `ts_source` 取值扩为：`exact`（库内原始）/ `clipboard`（复制文本带来的真实时刻）/
   `inferred`（由上下文补全日期）/ `manual`（人填）/ `assumed`（**明确选择用采集时刻**）。
6. **兜底策略改变**：剪贴板没带时间时，默认 `missing_time='ask'`（挂起让用户定），
   三个选项：手填 / 用上下文推断 / 用采集时刻（标 `assumed`，**同时**保留 `captured_at`）。
   把「抓取时刻」这个说法在界面上统一改成「**采集时刻**」，并写明它不是消息时间
   （涉及 `index.html:671 / 2585 / 2586 / 2695 / 2696`）。

**坑**：改了 `ts` 的语义会让已有数据的时间轴解释变得不一致 —— 迁移时**不要**改写历史数据的 `ts`，
而是在界面上把 `captured_at` 与 `ts` 一起显示，并用 `ts_source` 的颜色区分可信度。

**验收**
- 复制一段**带时间头**的 QQ/微信消息，入库后的 `ts` 等于文本里的时间，`captured_at` 等于采集时刻，两者都可见。
- 复制一段**不带时间**的消息，程序**不会**默默用采集时刻顶替，而是按 `missing_time` 的语义停下或标注。
- 有覆盖新格式的单元测试（每种格式一个用例，用固定「当前时间」注入避免 flaky）。

---

### 需求 4：自检并进「设置」

- 导航去掉「自检」；`view-check` 的内容改为设置页的一个「体检」区块（或 Tabs 的一页）。
- 后端**完全复用** `GET /api/selfcheck`，不新造接口。
- 保留：重新体检按钮、打开数据/日志目录、每项的 `fix` 提示、可跳转项（`CHECK_ACTION`）。
- 顶部 `#wizard` 横幅的去留：**建议保留**（它是新手引导），但跳转目标从自检页改成设置页的体检区块。

**验收**：导航没有「自检」；设置页能看到 8 项体检结果与修复提示。

---

### 需求 5：设置加「关于」

内容至少包含：
- 版本号（`GET /api/health` 已有 `version`）、打包版/源码（`selfcheck.runtime.frozen`）、Python 版本、数据目录/日志目录
- **检查更新**：调 `GET https://api.github.com/repos/whyao56/WingMan/releases/latest`，
  与 `__version__` 做 semver 比较；显示「有新版本 vX.Y.Z + 下载链接」或「已是最新」。
  - **坑**：无网/被墙/代理环境下必须优雅降级 —— 超时 5 秒，失败文案是「**没检查成功**（原因）」，
    **绝不能**显示成「已是最新」（这是两件完全不同的事，和 0.1.3 修 `Failed to fetch` 是同一个教训）。
  - **不做**自动下载/静默安装。只给链接。
- **项目地址**：<https://github.com/whyao56/WingMan>
- 许可证、致谢（PyInstaller / FastAPI / pywebview 等）
- 合规声明摘要（指向 `docs/COMPLIANCE.md`）+ 隐私说明（数据只在本机 `DATA_DIR`）

**验收**：关于页能显示版本与项目地址；断网时检查更新给出的文案是「没检查成功」而不是「已是最新」。

---

### 需求 6：移除「通话」，放进「关于」

- 删除导航项 `data-v="voice"`（`index.html:422`）与 `#view-voice`（700-819）。
- 把其中的**有价值内容**（「已经想清楚的思路」那 5 条 + 「还是想要这个功能？」）压缩成
  「关于 → 路线图」一节或 `docs/ROADMAP.md`，**不要**把整页原样搬过去（那是另一种冗余）。
- **注意**：语音转写后端已于 0.1.1 撤下（`acff853`），`docs/COMPLIANCE.md` 与
  `README.md` 里有相关表述 —— 一起核对，别留下「通话页还在」的描述。CSS 里 `.subs/.cap/.meter`（231-243）随之清理。

**验收**：导航里没有「通话」；全仓 grep `view-voice` / `data-v="voice"` 无残留；文档里没有「通话功能」的失实描述。

---

### 需求 7：会话右键「会话设置…」改名「设置」，且能改平台（微信 / QQ）

- 菜单项文案 `index.html:1334`：「会话设置…」→「**设置…**」（标题仍是对象/渠道名，避免歧义写「渠道设置」）。
- 弹窗（`openChatSettingsDialog` 1185-1237）增加「**平台 / 渠道**」下拉：
  `微信 / QQ / 通话 / 当面 / 其他`，写回 `chats.platform` + `chats.channel`。
- 后端 `PATCH /api/chats/{id}`（`routes_data.py:139`）扩展支持 `platform` / `channel`；
  store 新增 `set_chat_platform(chat_id, platform, channel="")`。
  - **坑**：`channel` 是由 `platform` 映射出来的（`store.py:190-201`）。改 platform 时若不同时改 channel，
    两者会不一致 → 由 store 内部按映射表重算，除非显式传入。
  - **坑**：**不要**跟着改 `chat_id`。chat_id 是幂等键与游标的依据，改了会破坏去重与增量。
- 与需求 1 的联动：改平台后，对象详情里该渠道的分组/图标要立即变。

**验收**：右键菜单显示「设置…」；能把一条 QQ 记录改成微信并刷新后仍是微信；改完 `collect_cursors` 不受影响。

---

### 需求 8：指挥台先选「对象」，再勾选（单选/多选）该对象下的「会话」

**现状**：指挥台是**单会话**流程，`state` 里只有 `chatId`，全文件唯一的 checkbox 与选择无关。

**设计**
- 左栏两级：对象（点一下选中）→ 该对象的渠道列表，每项带 checkbox；支持全选/反选。
- 单选：行为同现在（`POST /api/chats/{id}/suggest`）。
- 多选（≥2 条渠道）：新增 `POST /api/persons/{id}/suggest`，body `{chat_ids[], peer_message}`；
  后端把多会话的最近消息按时间合并 + 该对象的对象级事实/画像一起注入上下文
  （复用 `store.person_messages`(786) 与 `engine/context.build_context`；注意**上下文预算**，见 ENGINE_DESIGN）。
- 界面上必须**明确显示当前用的是哪几条渠道**（多选时把渠道名列出来），不要让人以为在看单条会话。
- **坑**：多选跨平台时「对方」的身份可能不同（同一个人的 QQ 和微信昵称不同）→ 由对象统一，
  但建议在上下文里保留渠道来源标注，避免模型把两段对话的情节混成一段。

**验收**：能选中对象 + 勾 1 条或多条渠道；多选时建议的「依据」里能看到来自多条渠道的消息；单条时行为与现在一致。

---

### 需求 9：右上角关闭 → 弹小窗（关闭程序 / 关闭弹窗后台运行）

**现状**：窗口是 pywebview（`desktop.py:110-119`），**关窗即退出**（`desktop.py:249`）；前端没有 `window.close`。

**设计（方案 A，推荐）**
1. `desktop.py` 在 `create_window` 之后注册关闭拦截：
   ```python
   def on_closing():
       # 阻止直接关闭，改为让页面弹小窗
       try:
           window.evaluate_js("wmShowCloseDialog && wmShowCloseDialog()")
       except Exception:
           return True      # 拿不到页面就放行，避免关不掉
       return False         # 取消本次关闭
   window.events.closing += on_closing
   ```
   **坑（务必实测）**：`closing` 事件的处理器跑在 GUI 线程，直接在里面调 `evaluate_js` 有死锁风险。
   若卡住，改为在处理器里 `threading.Thread(target=..., daemon=True).start()` 后再 `return False`。
   不同 pywebview 版本对「返回 False 取消关闭」的支持不一致 —— 先用当前锁定的版本实测确认。
2. 暴露 js_api：`close_app()`（`window.destroy()` 或置退出标志后 `webview.start` 返回）与
   `hide_window()`（`window.hide()`，服务继续跑）。
3. 页面加小窗：两个按钮「关闭程序」「关闭后台运行」+ 一句话解释「关闭弹窗后采集仍在继续」。
   - **「后台运行」必须给恢复路径**，否则用户会以为程序消失了：pywebview 没内置托盘，
     **建议**（按成本从低到高）：
     a) 隐藏后**弹一次系统通知/提示**，说明「程序仍在后台，重新双击图标即可回到界面」；
     b) 再次启动 exe 时检测到端口已占用 → 直接打开窗口而不是报错（`desktop.py` 需要处理这种「已有实例」情况）；
     c) 若要做托盘需引入 pystray 等新依赖 —— **先不做**，除非用户明确要。
   - **坑**：浏览器回退模式（`desktop.py:242-248`，无原生窗口）下没有「右上角的叉」，
     这个按钮要么不显示，要么退化为 no-op，不能报错。
4. 「关闭程序」要走**优雅退出**：先停半自动监听线程、关 SQLite 连接，再退出（参考 `desktop.py` 现有的退出路径）。

**验收**：点右上角叉 → 出现小窗；选「关闭后台运行」→ 窗口消失但 `/api/health` 仍可访问、半自动监听仍在；
选「关闭程序」→ 进程真的退出、端口释放。

---

### 需求 10：去冗余、加联动（自行优化）

**落地动作（这就是本轮的「冗余清理清单」）**

| 现在 | 改成 | 理由 |
|---|---|---|
| 导航 7 项 | **4 项：指挥台 / 对象 / 采集 / 设置** | 导入→采集（需求 2）、自检→设置（需求 4）、通话→删除（需求 6）、记忆→对象详情（需求 1） |
| 「记忆」页与「会话设置」弹窗都能改会话信息 | 统一到「对象 → 记忆」与「渠道 → 设置」 | 一个概念一个编辑处 |
| 采集页四张卡各自为政 | 采集页按「三条通道」组织：自动 / 半自动 / 手动导入，下面挂进度与历史 | 用户问的是「我该怎么采」，而不是「有哪几个功能」 |
| 事实能加但没地方看全 | 对象 → 记忆 里按对象级/渠道级分组 | 减少「不知道有没有记住」 |

**联动要求（每条都要能被观察到）**
- 设置里配好模型 → 指挥台不再提示「未配模型」（现在靠 `updateSteps` 检查 `state.check.items` 里的 llm）。
- 采集/导入完成 → 左栏对象列表与计数**立即刷新**（现在导入后要手动跳页才刷新）。
- 指挥台选了对象 → 采集页的半自动入口自动带上这个 `person_id`。
- 自检发现问题 → 侧边栏（外设需求 1）+ 体检区块同时提示，点哪边都能到修复处。

---

### 需求 11：历史留存

**设计**
- 新表：
  ```sql
  CREATE TABLE IF NOT EXISTS engine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT DEFAULT '', chat_ids TEXT DEFAULT '',   -- JSON 数组
    peer_message TEXT DEFAULT '',
    analysis TEXT DEFAULT '',   -- JSON
    strategy TEXT DEFAULT '',   -- JSON
    options  TEXT DEFAULT '',   -- JSON
    trace    TEXT DEFAULT '',   -- JSON
    created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS sim_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER DEFAULT 0, option_id TEXT DEFAULT '',
    option_text TEXT DEFAULT '', branches TEXT DEFAULT '',
    advice TEXT DEFAULT '', created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, kind TEXT NOT NULL,      -- import / collect_auto / collect_semi / edit / profile
    person_id TEXT DEFAULT '', chat_id TEXT DEFAULT '',
    summary TEXT DEFAULT '', detail TEXT DEFAULT ''
  );
  ```
  三张表都要建索引（`person_id`、`chat_id`、`ts`）。
- 写入点：`engine/pipeline.run_analysis` 返回前（`pipeline.py:81-89`）、`simulator.simulate` 返回前（`100-105`）、
  导入成功处（`routes_data.py:67/97`）、采集落库处（`collect/pipeline.py:557-572`、`semi.py:291-296`）、
  消息批量编辑处（需求 1 的新接口）。
- 接口：`GET /api/persons/{id}/history?kind=`、`GET /api/history/runs/{id}`、`DELETE /api/history/runs/{id}`、
  `POST /api/history/runs/{id}/reuse`（把当时的上下文重新拉起来，方便接着聊）。
- 界面：对象详情「历史」Tab（按 kind 分组，时间倒序）+ 指挥台下「最近输出」（可展开回看、可复制、可删除）。

**坑**：`engine_runs.options` 里存的是模型生成的**回复原文**，属于隐私 ——
必须与消息一样进 `DATA_DIR`，并且**导出/删除要一起覆盖**（`store.export_chat_json` 1257 也要考虑是否带上历史）。

**验收**：跑一次指挥台 → 刷新页面/重启程序 → 历史里能找到刚才那次输出（含分析、策略、候选与推演）；
对象详情能看到这个对象的导入/采集/编辑痕迹。

---

### Bug 1：半自动采集 `'PersonChannel' object has no attribute 'peer_name'`

**根因已定位**（逐行证据见 [STATUS.md](STATUS.md) §10.1）：`semi.py:454` 读了 `PersonChannel` 上不存在的 `peer_name`，
`457` 读了同样不存在的 `me_name`；**触发条件是「person 非空且已绑定 ≥1 个 chat」** ——
正好是「第一次创建数据集之后再选中它」。

**修法**
- `454`：`ch.peer_name` → `ch.name`（`PersonChannel` 里代表对方显示名的就是 `name`）。
- `457`：`PersonChannel` 没有「我」的称呼 → 需要时用 `store.get_chat(ch.chat_id).me_name`，或干脆不收集。
- **更稳的做法**（建议）：`schemas.PersonChannel` 直接补上 `peer_name` / `me_name` 两个字段，
  并在 `store.person_detail`（`store.py:777`）里从 `ChatInfo` 填进去。这样「渠道视图缺字段」这类坑一次性消掉
  —— **但要注意**：`PersonChannel` 是前端在用的响应模型，加字段是**兼容**的，删字段不是。
- **回归测试（必须）**：`tests/test_collect_semi.py` 现在的盲区是
  （a）`_started()`（124）绕过 `_resolve_names`；（b）唯一走真 `start()` 的用例（595）用的 person 没有 chat。
  新增用例：**建 person → `upsert_chat(..., person_id=pid)` → 调真 `start(store, ..., person_id=pid)`**
  → 断言不抛异常且 `_peer_names` 里含该渠道名。
  并且按项目的守卫标准：**先确认这个用例在修之前会失败**（在旧代码上跑一次）。

---

### 外设 1：自检提醒放到侧边栏

**现状**：`#nav-badge` 红点挂在「自检」导航项（`index.html:423`），但只在自检页可见时才起作用；
真正的提醒是顶部 `#wizard` 横幅（`updateWizard` 2016-2034）。

**设计**
- 导航项收缩成 4 项后，红点挂到「**设置**」（体检在里面）；
- 侧边栏底部（`#health` `430-432` 上方）加一行**可点击的状态摘要**：
  - 全部 ok → 一行淡色「一切正常」，不抢注意力
  - 有 warn/fail → 黄色/红色一行，写「N 项待处理：<最严重的一项 label>」，点击直接跳到设置页体检区块
- 顶部 `#wizard` 横幅**保留**但不再重复同样的信息（横幅讲「怎么开始用」，侧边栏讲「哪里有问题」）。
- **坑**：不要每 5 秒重排一次 DOM 造成闪动；体检结果沿用现有的低频刷新节奏（`init` 里调一次 + 手动按钮）。

**验收**：故意把 llm 设成 mock → 侧边栏出现黄色提示且可点跳转；修好后提示消失。

---

## 2. 分阶段执行（每阶段独立可测、独立提交）

> **每阶段结束必须**：`cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q` 全绿
> + 前端 JS 语法检查通过（CI 里那条 node 脚本）+ 单独 commit。
> **不要 push。**

### S0 · 后端打底（不动界面，纯加能力）

> **状态：已完成**（未推送，前端未动）。落地清单与遗留问题见 [STATUS.md](STATUS.md) §12。

1. 修 Bug 1 + 补回归测试（**先验证新测试在旧代码上失败**）。
2. 迁移与新表：`messages.captured_at`、`facts.person_id`（含去重 + 部分唯一索引）、
   `person_personas`、`engine_runs`、`sim_runs`、`activity_log`。**迁移必须幂等**（重复启动不报错）。
3. store 新方法：`update_message` / `delete_messages` / `insert_manual_message` /
   `set_chat_platform` / `save_run` / `list_runs` / `log_activity` / 对象级 facts 与 persona 读写。
4. 新接口：`/api/messages/{id}`(PATCH/DELETE)、`/api/messages/bulk`、`/api/persons/{id}` 的
   `overview|facts|persona|history`、`/api/chats/{id}/messages`(POST)、`PATCH /api/chats/{id}` 扩展 platform/channel。
5. **测试**：每个新接口至少一条 200 路径 + 一条边界（撞幂等键、空 ids、不存在的 person）。

### S1 · 前端骨架：对象中心
导航 7→4；左栏改对象（两级）；对象详情 5 个 Tab；记忆页并入；会话设置改名 + 平台可改；聊天记录可编辑 + 批量。
**注意**：`index.html` 是**单文件 2787 行**，改动会很集中 —— 建议按「先加容器和函数、再切导航、最后删旧代码」的顺序，
每步都能在浏览器里点一遍；**不要**一次性大重写（回归无法定位）。

### S2 · 指挥台：对象 + 多选渠道 + 历史
左栏选择器、`POST /api/persons/{id}/suggest`、历史 Tab 与「最近输出」。

### S3 · 采集与设置收尾
导入并入采集通道（含拖拽/多文件/行级错误）、自动采集「怎么拿到密钥」说明块、
半自动「采集时刻 vs 消息时刻」的 UI 与新解析、体检并入设置、关于（检查更新/项目地址/通话内容）、关闭小窗。

### S4 · 收尾
去冗余复查（对照需求 10 的表格逐条确认）、文档（ARCHITECTURE 概念映射 / TROUBLESHOOTING 新增坑 /
CHANGELOG / ROADMAP / README 界面截图与导航描述）、版本号 → **0.1.4**、
`docs/releases/v0.1.4.md`、全量测试、`scripts/preflight.py`。

---

## 3. 给执行窗口的几条硬约束

1. **不确定就问**：需求里有模糊处（例如「对象」是否允许多人合并成一个、多选渠道的上下文预算上限）先问用户，不要自行发明规则。
2. **守卫式测试**：新增测试要能在**旧代码上失败**（项目已有这个标准，见 `test_http_keepalive.py` 的注释与 0.1.3 的教训）。
3. **隐私红线不变**：真人姓名已从代码与 git 历史清除，`backend/tests/test_privacy_pseudonyms.py` 是守卫；
   新代码、新测试、新文档里**不要**引入真实姓名（用「小鹿」）。历史输出（需求 11）是隐私数据，进 `DATA_DIR`，导出/删除要覆盖。
4. **不要动合规边界**：采集只读本机、用户自己的数据；不新增任何绕过权限的行为。
5. **行号会漂**：用函数名/符号名定位，改完顺手在本文档或 STATUS.md 里更新受影响的引用。
6. **提交信息**：沿用仓库风格 `type(scope): 中文描述 —— 说清为什么`，body 里写「改了什么 / 为什么 / 怎么验的」。
