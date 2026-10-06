# WingMan 项目现状（交接文档）

> **用途**：给「全新上下文」的窗口看的一份现状底稿。目标是**读完不用再通读代码**就能动手改。
> **快照**：2026-10-06 18:00（GMT+8），版本 `0.4.0`，全量 **208 passed + 1 skipped**。
> **注意**：文件行号会随改动漂移。改动前先用函数名定位，再用行号确认。
> 本文档答的是「现在是什么样」；「要改成什么样」见 [PLAN-对象中心迭代.md](PLAN-对象中心迭代.md)。

---

## 0. 一句话

WingMan 是一个**跑在本机的聊天记录分析工具**：把微信/QQ 的聊天记录导进来或采进来，
交给大模型做「记忆 + 人物画像 + 回复建议」。
**0.4.0 起以「对象」为中心** —— 一个人可以在多处（微信 / QQ / 换过昵称的老号）有记录，
它们算同一个对象的几个**渠道**，看的是合起来的视野。
数据全部落在本机 SQLite，程序不主动外传任何东西（模型调用按用户自己配的 endpoint 走）。

- 形态：Python FastAPI 后端 + **单文件 HTML 前端**（`frontend/index.html`，4653 行，内联 JS，无构建步骤）
- 分发：PyInstaller 打包成免安装 zip（`WingMan.exe` + `_internal/`），当前 **v0.4.0**，约 34 MiB
- 界面：**4 个一级页签** —— 指挥台 / 对象 / 采集 / 设置（0.4.0 从 7 项收敛而来，见 §4）
- 合规：`docs/COMPLIANCE.md`；采集能力只读用户自己机器上的库，且**不绕过任何权限**（见 §9）

---

## 1. 目录地图

```
wingman/
├─ backend/
│  ├─ app/
│  │  ├─ main.py            209  FastAPI 装配、静态前端挂载、python -m app.main 入口
│  │  ├─ desktop.py         302  桌面壳：pywebview 窗口 + 后台线程跑 uvicorn + --check 自检
│  │  ├─ config.py          204  路径/设置/KEEP_ALIVE_S、DATA_DIR、LOG_DIR、RESOURCE_DIR
│  │  ├─ context.py         156  单例上下文（store/embedder/retriever/llm/settings）
│  │  ├─ store.py          1877  ★ 全部 SQLite 读写（13 张表）+ 迁移
│  │  ├─ schemas.py         394  ★ 全部 Pydantic 模型
│  │  ├─ selfcheck.py       275  8 项体检
│  │  ├─ api/
│  │  │  ├─ __init__.py      17  路由注册顺序 admin→data→persons→collect→engine→health
│  │  │  ├─ routes_admin.py  139  设置/自检/打开目录/health
│  │  │  ├─ routes_data.py   239  导入 3 个接口、会话 CRUD、消息读、导出、索引、事实、画像
│  │  │  ├─ routes_persons.py174  ★ 人物 CRUD/合并/渠道/跨渠道时间线（**前端几乎没用**）
│  │  │  ├─ routes_collect.py345  ★ 采集：clients/matrix/key/preview/run/cursors/semi/*
│  │  │  ├─ routes_engine.py  96  指挥台：suggest/analyze/simulate
│  │  │  └─ routes_health.py 261  /api/health/details（前端未调用）
│  │  ├─ collect/           采集子系统（见 §5）
│  │  │  ├─ pipeline.py     722  自动采集主流程（探测→挑库→取密钥→解密→读表→落库）
│  │  │  ├─ semi.py         592  ★ 半自动采集（剪贴板监听 → 待确认 → 落库）
│  │  │  ├─ clipboard.py    438  剪贴板读取与「复制的文本」解析
│  │  │  ├─ keys.py         451  取密钥（粘贴/缓存/内存扫描两级）
│  │  │  ├─ sqlcipher.py    394  SQLCipher 解密与参数 profile
│  │  │  ├─ reader.py      1169  从已解密的库读消息（列名识别、名字索引、分组）
│  │  │  ├─ winapi.py       389  Windows 进程枚举 + 内存读取
│  │  │  ├─ detect.py       281  客户端安装/版本/运行状态探测
│  │  │  └─ matrix.py       292  ★ 版本支持矩阵（界面直接渲染它）
│  │  ├─ adapters/          导入适配器（qq.py / wechat.py / generic.py / registry.py）
│  │  ├─ memory/            embedder(HashEmbedder 兜底/Cloud) + retriever(混合检索) + profiler
│  │  └─ engine/            analyzer / planner / suggestor / simulator / context / prompts
│  ├─ tests/                19 个测试文件，全量 208 passed + 1 skipped（0.4.0）
│  ├─ data/                 运行时 SQLite（.gitignore；含 collect_cache/）
│  └─ logs/wingman.log
├─ frontend/index.html      4653 单文件前端
├─ scripts/                 build_exe.py / make_release.py / preflight.py / e2e_check.py …
├─ docs/                    本目录（ARCHITECTURE / MODELS / ENGINE_DESIGN / TROUBLESHOOTING / STATUS / PLAN…）
├─ build/wingman.spec       PyInstaller 配置（**要提交**）
├─ dist/ release/           打包产物（.gitignore，make_release 从这里取件上传）
└─ .github/workflows/ci.yml 两个 Python 版本跑 pytest + 冒烟 + 前端 JS 语法
```

---

## 2. 数据模型（`backend/app/store.py`）

DDL：`FACTS_DDL` `48-60`、`SCHEMA` `62-180`；建表 `init()` `284-293`。

| 表 | 行 | 列 | 说明 |
|---|---|---|---|
| `chats` | `66-73`（补列 `302-336`） | id PK, platform, name, peer_name, me_name, created_at, **person_id**, **channel**, **source** | 「一个渠道上的一段记录」。`channel ∈ qq/wechat/call/offline/generic`（由 platform 映射，`190-201`）；`source ∈ import/collect` |
| `messages` | `75-91` | id PK AI, chat_id FK(CASCADE), platform, sender, role(me/peer/system), ts, msg_type, text, ext_id, **ts_source**, **captured_at** | **UNIQUE(chat_id, sender, ts, text)** 是幂等键 + `INSERT OR IGNORE`。`captured_at`=**采集时刻**（S0 新增），与消息自身的 `ts` 分开存 |
| `embeddings` | `96-101` | message_id PK, model, dim, vec BLOB | float32 原始字节 |
| `facts` | `48-60` | id, chat_id FK(**可空**), **person_id**, subject(peer/me/relationship), key, value, confidence, evidence, updated_at | UNIQUE 是**四列**（chat_id, subject, key, value）；`chat_id` 可为空（**对象级事实**没有具体渠道，S0 放开），对象级子集另有部分唯一索引 `ux_facts_person` |
| `summaries` | `105-113` | id, chat_id, kind(daily/weekly/milestone), period, content, created_at | L2 记忆 |
| `personas` | `115-123` | **chat_id PK**, goal, my_style, peer_profile, taboos, stage, updated_at | ★ 渠道级画像（保留作覆盖） |
| `person_personas` | SCHEMA 尾 | **person_id PK**, goal, my_style, peer_profile, taboos, stage, updated_at | ★ **对象级人物设定**（S0 新增，界面只暴露这一层） |
| `kv` | `125-128` | key PK, value | 运行时设置覆盖 |
| `persons` | `136-149` | id PK(`p_`+12hex), name, aliases(JSON), relation, desired_relation, stage_goal, notes, created_at, updated_at | ★ 人物是一等实体 |
| `collect_cursors` | `158-177` | id, platform, account, peer_key, person_id, chat_id, last_ts, last_ext_id, fingerprint, merged_count, collected_from, last_run_at, status, message | UNIQUE(platform, account, peer_key) |
| `engine_runs` | SCHEMA 尾 | id, person_id, chat_ids(JSON), peer_message, analysis/strategy/options/trace(JSON), created_at | ★ 指挥台输出留存（S0 新增） |
| `sim_runs` | SCHEMA 尾 | id, run_id, option_id, option_text, branches(JSON), advice, created_at | ★ 推演留存（S0 新增），`run_id` 关联 `engine_runs.id` |
| `activity_log` | SCHEMA 尾 | id, ts, kind(import/collect_auto/collect_semi/edit/profile), person_id, chat_id, summary, detail | ★ 动作痕迹（S0 新增） |

> S0（对象中心后端打底）新增了 `person_personas` / `engine_runs` / `sim_runs` / `activity_log` 四张表
> 与 `messages.captured_at`、`facts.person_id` 两列，共 **13 张表**。见文末 §12。

### 必须记住的不变量（踩了会静默出错）

1. **chat 必须有人**：`upsert_chat` 末尾强制 `ensure_person_for_chat`（`store.py:449`；规则「有归属就用、没有就按『对方称呼 > 会话名』新建」，`451-479`）。新写入路径漏了这条，人物列表就是空的。
2. **chat_id 生成规则有两套**：导入 `{platform}-{slug}-{md5[:6]}`（`adapters/registry.py:153-158`）、采集 `{client}:{account}:{peer_key}`（`collect/pipeline.py:277-284`）。**不要新造第三套**。
3. **消息可增删改（S0 起）**：`update_message` / `delete_messages` / `insert_manual_message`。
   幂等键仍是 `UNIQUE(chat_id, sender, ts, text)` —— 编辑撞键时 store **返回结构化错误**
   （`{ok:False, error:"duplicate"}`），接口据此给 409，**不要**改成 500。
4. **对象级事实的唯一性靠部分唯一索引**：`facts.chat_id` 现在可空，SQLite 里 NULL 互不相等，
   所以对象级事实（`chat_id` 为空）必须靠 `ux_facts_person`（`person_id, subject, key, value`
   且 `chat_id IS NULL OR chat_id=''`）保护。**建这条索引前必须先清重复**，否则启动失败。
5. **`captured_at` ≠ `ts`**：`ts` 是「消息在对话里发生的时刻」，`captured_at` 是「采集时刻」。
   不要把抓取时刻写进 `ts`（历史遗留的 `assumed` 也不要再扩散）。
6. **persona 与 persons 未打通**：`persons` 的关系字段只由用户手填，模型永远不写；
   对象级设定存在 `person_personas`，渠道级 `personas` 保留作覆盖。

### 存储方法速查

| 域 | 方法（行号已随 S0 漂移，以函数名为准） |
|---|---|
| 会话 | `upsert_chat` `get_chat` `list_chats` `delete_chat` `rename_chat` `set_roles` · **S0** `set_chat_platform` |
| 人 | `create_person` `get_person` `find_person_by_alias` `list_persons` `update_person` `bind_chat` `unbind_chat` `merge_persons` `delete_person`（**只解绑不删消息**，S0 起顺带清 `person_personas`）`person_detail` `person_messages` |
| 消息 | `insert_messages` `list_messages` `recent_messages` `messages_by_ids` `last_peer_message` `search_text` `all_messages` · **S0** `update_message` `delete_messages` `insert_manual_message` |
| 向量 | `upsert_embeddings` `unembedded` `load_embeddings` |
| 事实/摘要/画像 | `replace_facts` `upsert_fact` `list_facts` `delete_fact` `add_summary` `list_summaries` `get_persona` `save_persona` · **S0** `list_facts_for_person` `upsert_person_fact` `get_person_persona` `save_person_persona` |
| 留存/痕迹 | **S0** `save_run` `list_runs` `get_run` `delete_run` `save_sim_run` `list_sim_runs` `log_activity` `list_activity` |
| 游标 | `get_cursor` `list_cursors` `save_cursor` `reset_cursor` `merged_message_count` `messages_fingerprint` |

---

## 3. HTTP 接口全表

前缀统一 `/api`；`main.py:185-187` 把 `frontend/` 挂在 `/`。

### 采集 `routes_collect.py`
`GET /collect/clients`69 · `GET /collect/clients/{key}`80 · `GET /collect/matrix`88 ·
`POST /collect/key/check`101 · `POST /collect/key/scan`136 · `POST /collect/preview`166 ·
`POST /collect/run`179 · `GET /collect/cursors`187 · `DELETE /collect/cursors`195 ·
`POST /collect/inspect`218 ·
`POST /collect/semi/start`267 · `POST /collect/semi/stop`293 · `GET /collect/semi/status`299 ·
`GET /collect/semi/poll`304 · `POST /collect/semi/commit`310 · `POST /collect/semi/discard`325 ·
`POST /collect/semi/clear`335

### 数据 `routes_data.py`
`POST /import/preview` · `POST /import` · `POST /import/text` ·
`GET /chats` · `GET /chats/{id}` · `DELETE /chats/{id}` ·
`PATCH /chats/{id}`（改 name/peer_name/me_name；可选 `me_names` 批量纠正角色；**S0 起支持 `platform`/`channel`**，channel 按映射重算、**不改 chat_id**；返回里多带 `chat`）·
`GET /chats/{id}/messages` · **S0** `POST /chats/{id}/messages`（手动加一条）·
`GET /chats/{id}/export` · `POST /chats/{id}/index` ·
`GET/POST /chats/{id}/facts` · `DELETE /chats/{id}/facts/{fid}` ·
`GET/PUT /chats/{id}/persona` · `POST /chats/{id}/profile`
**S0 消息二次编辑**：`PATCH /messages/{msg_id}`（撞唯一键 → 409 `error:duplicate`）·
`DELETE /messages/{msg_id}`（不存在 → 404）·
`POST /messages/bulk`（`{ids, action: delete|set_role|set_ts|set_sender, value}` → `{changed, skipped, errors[]}`）

### 人物 `routes_persons.py`
`GET /persons` · `POST /persons` · `GET /persons/{id}` · `PATCH /persons/{id}` ·
`DELETE /persons/{id}` · `POST /persons/{id}/merge` · `GET /persons/{id}/messages` ·
`POST /persons/{id}/channels` · `DELETE /persons/{id}/channels/{chat_id}`
**S0 新增**：`GET /persons/{id}/overview`（对象详情聚合）·
`GET/POST /persons/{id}/facts`、`DELETE /persons/{id}/facts/{fid}`（对象级事实，
列表里逐条标 `scope: person|chat`）· `GET/PUT /persons/{id}/persona`（对象级设定）·
`GET /persons/{id}/history?kind=`（历史输出 + 动作痕迹）

### 指挥台 `routes_engine.py`
`POST /chats/{id}/suggest`（单渠道，返回 `trace.run_id`）· `POST /chats/{id}/analyze` ·
`POST /chats/{id}/simulate`（可带 `run_id` 把推演挂到某次运行上）·
**0.4.0 新增** `POST /persons/{id}/suggest`（**多渠道**：body 可带 `chat_ids`（有序，第 1 个是
主渠道）；不传就用该对象全部渠道。空渠道 400、未知对象/渠道 404、无消息 400）·
`GET /history/runs/{run_id}` · `DELETE /history/runs/{run_id}`

### 设置/自检 `routes_admin.py` + `routes_health.py`
`GET /health`23 · `GET /selfcheck`38 · `POST /runtime/open`48 · `GET /settings`73 · `PUT /settings`93 ·
`POST /settings/reset`106 · `POST /settings/test`113 · `GET /settings/models`129 ·
**0.4.0 新增**：`GET /desktop/state`145（有没有原生窗口）·
`POST /desktop/close`157（action ∈ quit/background/cancel，其余 422）·
`POST /desktop/show`174（把「后台运行」藏的窗口调回来）·
`GET /update/check`189（查 GitHub Releases；**查不到时 `ok:false` 且不给 `has_update`**）·
`GET /health/details`（前端未调用）

---

## 4. 前端结构（`frontend/index.html`，4653 行）

> **0.4.0 起是一套「对象中心」的界面。** 拿到 0.3.x 的行号来对会全错，
> 请先用函数名定位，再用行号确认。

### 导航（**4 项**；重要：**用 `data-v`，不是 `data-page`**）
`#nav button[data-v]` `504-509`；切换 `goView(name)` `1570-1578`：高亮 toggle class `on`，
容器 id = `view-` + name，并有两个「进入钩子」—— 进采集页调 `collectOnEnter()`（`3920`）、
进对象页调 `objectsOnEnter()`（`2078`）；离开采集页调 `semiPollStop()` 停轮询。
深链接在 `init()` 尾部 `4027-4038`，`LEGACY_VIEW` `4029-4031` 做旧链接重定向。

| 导航项 | data-v | 容器 | 主渲染函数 |
|---|---|---|---|
| 指挥台 | `copilot` | `#view-copilot` 537 | `renderBundle`1927 `analysisHTML`1844 `optionHTML`1900 `simHTML`1996 |
| 对象 | `objects` | `#view-objects` 587 | `renderObjects`1638 `openObjTab`2098 `loadOverview`2117 `loadObjMemory`2169 `loadObjChat`2286 `loadObjCollect`2574 `loadObjHistory`2636 |
| 采集 | `collect` | `#view-collect` 719 | `loadClients`3356 `paintClients`3373 `paintMatrix`3409 `paintReport`3434 `paintSemi`3693 `loadCursors`3896 |
| 设置 | `settings` | `#view-settings` 882 | `loadSettings`2903 `loadSelfCheck`3062 `renderSideWarn`3034 |

`#view-settings` **一页装了三件事**：模型配置 → `#set-selfcheck`「体检」962（原「自检」页）→
`#set-about`「关于」984（含 `#about-check` 检查更新、`#about-voice` 通话折叠区 1002）。

常驻：品牌 `#brand` 498（内含 `#win-close` × 关闭按钮 **499**）、对象两级列表 `#objlist` `512`、
**`#side-warn` 侧栏自检提醒 `518`**、`#health` `519`、新手横幅 `#wizard` `525`、
指挥台「上手三步」`#cp-steps` `542`。

### 指挥台（**先选对象 → 再勾渠道，可多选**）
`state.cpChatIds`（有序数组，**第 1 个是主渠道**）`1138-1165`。
选对象 `pickCopilotPerson`1768 → 绘胶囊 `renderCopilotPicker`1717 → 勾渠道 `toggleCopilotChat`1786
→ 贴对方消息 `#cp-msg`571 → `#cp-go`573：

- **勾了 1 个** → `POST /api/chats/{id}/suggest`（老路径，不变）
- **勾了 ≥2 个** → `POST /api/persons/{id}/suggest`，body 带 `chat_ids`（**只有对象接口
  才知道怎么把几个渠道合并**，见 §6）

结果进 `#cp-result` `582`。每张建议卡可「复制」/「推演走向」（`act-sim`，带
`run_id` 挂到这次分析上）。左侧没对象时显示 `#cp-nochat` `544`。

### 对象页（5 Tab，原「记忆」页并入这里）
`#obj-tabs` `597`，Tab 名单 `OB_TABS` `2071` = `overview/memory/chat/collect/history`。
切换用 `openObjTab`2098；`#obj-empty`（无对象）/ `#obj-main`（有对象）二选一显示。

| Tab | 容器 | 内容 |
|---|---|---|
| 概览 | `#ob-overview` 605 | 渠道数、消息数（对方/我）、关系与阶段；近期活动流水 |
| 记忆 | `#ob-memory` 607 | 左：**对象级**人物设定（`#op-goal`/`#op-stage`/`#op-taboos`/`#op-mystyle`/`#op-profile`/`#op-save`）；右：**已记住的事实** `#ofacts`，按对象级/渠道级两组、**分别计数不去重** |
| 聊天记录 | `#ob-chat` 651 | **可二次编辑**：`#ob-selall`/`#ob-selnone` 全选清空、`#ob-del` 批量删除、`#ob-role`/`#ob-ts`/`#ob-sender` 批量改、`#ob-add` 手动加一条 |
| 采集 | `#ob-collect` 684 | 该对象的采集游标与半自动进度 |
| 历史 | `#ob-history` 699 | `engine_runs` / `sim_runs` / `activity_log`；可展开、可删除（`data-delrun`） |

### 采集页（**四块，原「导入」页并入这里**）
1. 「自动采集」`746` 起：`#ac-client` `#ac-key` `#ac-check` `#ac-scan` `#ac-preview` `#ac-run` `#ac-report`。
2. 「半自动采集」`796` 起：`#sc-client` `#sc-person`(+`#sc-persons`) `#sc-time` `#sc-start` `#sc-stop` `#sc-clear` `#sc-hint` `#sc-list`。
   **`#sc-time` 的选项是 `inferred`（默认）/`ask`** —— 0.4.0 起不再有「用抓取时刻」这个选择，
   旁边有 `#sc-time-help` 打开 `openTimeHelp()`3609 解释时间的 5 种来源。
3. **「导入」`#imp-drop` 832**：拖放区 + `#imp-file`（**multiple**）、`#imp-files` `#imp-adapter`
   `#imp-name` `#imp-mename` `#imp-preview` `#imp-go`；粘贴区 `#paste-text` + `#paste-name` → `#paste-go`。
   拖放绑定 `bindDrop`2746（区域内）与 `bindWindowDrop`2767（窗口任意位置）。
4. 「采集进度」`#cur-load`/`#cur-list`。

### 右键菜单 / 弹窗 / 关闭窗口
- `contextmenu` 绑在 `.chitem` 上；菜单项 `openChatMenu`1554：打开 / 重命名… /
  **「设置…」1559**（0.4.0 从「会话设置」改名，`openChatSettingsDialog`1397，可改平台
  微信/QQ 与「对方 / 我」的称呼）/ 导出 JSON / 删除渠道…。
- 弹窗 `openModal`1245、`modalButton`1269、`confirmWithCountdown`1289。
- **关闭窗口（需求 9）**：后端拦下系统 × 后调 `window.__wingmanAskClose`（`4015`）→
  `openCloseDialog`3963 渲染「关闭程序 / 关闭弹窗（后台运行）/ 取消」；`#win-close`（`499`）
  走同一个函数。三个动作打到 `POST /api/desktop/close`。
  **配套的回头路**：`POST /api/desktop/show` → `desktop.show_window()`。
  「后台运行」把窗口藏了之后，再双击 `WingMan.exe` 由 `desktop.main()` 的单实例分支
  调这个接口把**老进程的**窗口显示出来。不这么做的话第二次双击会开出一个
  **没有服务的空壳窗口**，关它不会停掉真正在跑的进程。

### 通用设施
`$` / `$$` / `esc` / `state`1138 ·
**`api`1167**（网络层失败重发 1 次；4xx/5xx 不重试）· `rawApi`1182 ·
`toast`1195 · `busy`1202 · `busyClock`1217（按钮上显示已等待秒数）·
`loadHealth`1582 · **`loadObjects`1617**（拉对象+渠道、清洗失效选择、重绘左栏；
**导入/采集/改名/删除等会改变归属的动作都调它**）·
`init`4020（loadHealth→**loadObjects**→loadSettings→loadSelfCheck，30s 轮询 health）。

> 0.4.0 之前这里有个 `loadChats`，重构时并进了 `loadObjects`。漏改调用点会
> **静默地**把 init 后半段和指挥台的结果一起搞坏，所以钉了守卫
> `tests/test_frontend_assets.py::test_no_calls_to_undefined_functions`。

---

## 5. 采集子系统

### 5.1 自动采集链路（`pipeline.run` `342-406`）
```
POST /api/collect/run → _request_from(238-261) → pipe.run(store, req)
  ① 探测      detect_client()            352   detect.py:163
  ② 挑库      _dbs_to_collect()          323   用 _is_message_db(304-320) 排除 *_fts.db
  ③ 逐库 _collect_one(409)
       取密钥 obtain_key()               428   keys.py:397  （粘贴→缓存→内存扫描）
       解密   cached_plain_db/decrypt    447/451  sqlcipher.py，指纹 _fingerprint(213)
       认列   pick_message_tables()      479   reader.py:765（可 schema_overrides 覆盖）
       名字索引 build_name_index()       494   reader.py:920
       读表   read_messages()            605   reader.py:999 → 按 _group_key(266) 分组
  ④ 逐会话 _collect_one_chat(533)
       归属 find_person_by_alias / create_person  548-555
       upsert_chat(source="collect", person_id=…)  557
       insert_messages(rows)             564  幂等靠 UNIQUE 三列
       save_cursor(...)                  572
  ⑤ 报告 CollectReport.as_dict()        179
```
预演 `pipe.preview` `710`，内存预算 12 秒（`PREVIEW_MEMORY_BUDGET_S` 707）；正式 60 秒（`DEFAULT_MEMORY_BUDGET_S` 69）。

### 5.2 半自动采集（`semi.py`）
监听**剪贴板**（不是窗口）。原因写在 `clipboard.py:3-13`：微信 4.1 聊天区是自绘窗口、QQ 9.9 是 Chromium 无子节点，UIA 都读不到文本，**只有「选中 + Ctrl+C」这条路**能拿到内容。

```
ClipboardWatcher(clipboard.py:499)  0.4s 轮询 semi.POLL_INTERVAL(60)
   sequence_number() 变了才 read_text()          clipboard.py:104/118
SemiCollector._loop(206) → _tick(216) → watcher.poll(me_names, peer_names)
   parse_copied(408)：块状（称呼[+时间]+正文）/ 单条（需指认）
   _head_of(376)：切出「称呼 + 时间」两段，时间交给 _parse_ts(304)
   _match_known(352) 精确匹配已知称呼，匹配不上就问用户，**绝不猜**
_ingest(235) → PendingItem；归属与时间都有依据则直接 _commit_capture(293)
_commit_capture(293-358)：
   防重 _seen_recently(585) 只在 ts_source 是**推定**来的（inferred/assumed）时启用（DEDUPE_WINDOW=61）
   upsert_chat → insert_messages
挂起的等 POST /semi/commit(359)，支持 apply_to_rest 批量归角色
状态存 DATA_DIR/collect_cache/semi_state.json（_save/_load 532/547）
```
> **「数据集」在代码里不存在** —— 它就是**一个 chat**。界面上的「渠道」= `PersonChannel` = 一个 chat。
> `_resolve_chat_id(514)`：优先复用该人同渠道已有 chat_id，否则 `{client}:{person_id}`，再否则 `{client}:semi:{peer_name}`。

### 5.3 「时间」是怎么填的（0.4.0 已按需求 3 改完）

`messages` 只有两列与时间有关：`ts`（`store.py:81`）+ `ts_source`（`89`）。
**`ts_source` 一共 5 种**，界面上每条都会标注，点 `#sc-time-help` 能看解释：

| `ts_source` | 含义 | 怎么算出来的 |
|---|---|---|
| `clipboard` | 复制出来的文本自己带了年月日 | 直接用，最可信 |
| `relative` | 文本里是「昨天 21:03」这类相对时间 | `_parse_relative_ts(217)` 按复制那一刻往前推 |
| `inferred` | 完全没带时间 | `_infer_iso(618)` 按会话时间线推定（**新的默认**） |
| `assumed` | 旧的「抓取时刻」 | **仅用于兼容已存在的数据**，不再产生新条目 |
| `manual` | 用户手填 | 确认框里输入的 |

- **真正的「抓取时刻」载体是 `Capture.at`**（`clipboard.py:489` 起），它**只写进
  `semi_state.json`，不进消息表** —— 这正是需求 3 要的：采集时刻与消息时刻分开。
- `_ingest`（`semi.py:235`）的兜底：剪贴板带了时间就用它（`clipboard` / `relative`）；
  没带就按时间线推定（`inferred`），**不再有「用抓取时刻顶替」这个行为**。
  `missing_time=='ask'` 时仍然挂起等用户填。
- `_timeline_anchor(274)` 取**该会话库里最后一条消息的时刻**当锚点，`_infer_iso` 在它之后
  逐秒递增，且**绝不越过复制时刻**（消息不可能来自未来）。
- 相对时间解析有两处刻意收紧，都是被真实文本打脸后加的：**裸钟点只在紧跟称呼之后才认**
  （否则正文里的比分 `3:1` 会被当时间，见 `_parse_ts` 的 `allow_bare_clock`），
  以及**算出的时刻晚于当下就回退一天**（「21:03」在上午复制指的是昨晚）。
- 同一客户端每个库的密钥缓存键 = 客户端 + 库 salt（`_key_cache_key` `pipeline.py:639`，`salt_key_for` `keys.py:134`）。

### 5.4 版本支持矩阵（`matrix.py`）
`SUPPORT_MATRIX: dict[client, ClientSpec]`（`190`），键 `wechat` / `wechat3` / `qq`。
`ClientSpec`（`74-87`）：key, display_name, exe_names, low, high, tested, layout(DataLayout), cipher_profile, can_auto_read, why_not, guide, risks。
三个规格：`WECHAT4`（91，实测 4.1.13.12）、`WECHAT3`（129）、`QQ_NT`（152，实测 9.9.20.37051）。
`judge()`（`224`）→ `Support.verdict ∈ {not_installed, unsupported, untested, supported}`；
`supported_versions_table()`（`278`）被 `/api/collect/matrix` 与 `/clients` 直接返回，**前端渲染它而不是硬编码对照表**。

### 5.5 取密钥（`keys.py`）—— 界面要讲清楚的就是这套
`obtain_key`（`397-451`）的顺序：**依赖检查 → 用户粘贴 → 缓存 → 内存扫描**。
两级搜索 `scan_memory_for_key`（`194-391`）：
1. **第 1 级 十六进制串**（`234-284`，秒级）：`x'..'`/引号包裹（`_HEX_WRAPPED` 49-54）、裸 64 位串且邻域含 `sqlite`/`PRAGMA`（`_HEX_BARE` 55，上下文判定 258-260），逐候选 `verify_key`。
2. **第 2 级 32 字节滑窗穷举**（`303-379`，贵）：先按 **salt / 库文件名**找锚点区域（`325-330`，对齐 64KB），只在这些区域、只上最可能的 **2 套参数**穷举。
`Budget`（`172-191`）**每层循环都查**（这是 0.3.1 修的）。到点收工 ≠ 失败：返回 `budget_hit=True` + 「扫到哪了」。

**实测结论（`keys.py:13-27`，必须照实告诉用户）**：本机 QQ NT 9.9.20.37051 与微信 4.1.13.12，
全内存穷举 8378 万候选未命中；hex 候选 QQ 30 处全灭、微信 0 处。
→ **自动取密钥在本机走不通；能走通的是「手动粘密钥」和「半自动采集」。**

---

## 6. 记忆分层与指挥台

分层（`engine/context.py`）：**L1 事实（全量注入）+ L2 摘要（最近几条）+ L3 检索（向量+关键词动态召回）+ 最近对话 + 人物卡**。

**0.4.0 起 `build_context` 接受「一组渠道」**（`chat_ids: str | Sequence[str]`，也兼容传单个 str）：

- 第 1 个是**主渠道** —— 人格、昵称（`self_chat_name()`）、检索锚点都取自它，新消息也写进它；
- 事实：**该对象名下的全部渠道级事实 + 对象级事实**合并成一个视野，各带 `scope` 标注，
  **只合并视野、不做去重**；
- 摘要：跨渠道按 `period` 排序取前 N；近期消息：跨渠道合并后**按 `ts` 归并**取尾部 N 条；
- 检索：**逐渠道**做（`per_chat_k = max(5, top_k // len(ids))`），结果统一按 `score` 排序再截断 ——
  否则话多的那个渠道会把话少的整个盖掉；
- 合并了超过 1 个渠道时会往 `warnings` 里放一条说明。

- `memory/embedder.py`：`HashEmbedder`（字符 n-gram 哈希，永远可用，**不认近义词**）+ `CloudEmbedder`（OpenAI 兼容），工厂 `build_embedder` `212-239`。
- `memory/retriever.py`：`HybridRetriever`，`score = 0.60·语义 + 0.25·关键词 + 0.15·新鲜度`（`30-32`）；`index_chat`90 `search`121 `search_multi`196。
- `memory/profiler.py`：`build_index`47 `extract_facts`53（→`replace_facts`）`build_profile`111（→`personas`）`summarize_period`168（→`summaries`）。
- `engine/`：`pipeline.run_analysis(ctx, chat_ids, ...)`（接受一组渠道，`ids[0]` 为主渠道，
  `trace["chat_ids"]` 记下这次用了哪几个）、`analyzer.analyze`、`planner.plan`、
  `suggestor.suggest`（本地打分）、`simulator.simulate`。
- **入口**：`POST /api/chats/{id}/suggest`（单渠道，老路径）与
  `POST /api/persons/{id}/suggest`（多渠道；`chat_ids` 省略=该对象全部渠道）。
- **持久化：0.4.0 起有留存。** 每次分析落一条 `engine_runs`（含 `chat_ids`、判断、建议、推演），
  推演落 `sim_runs`，导入/采集/批量编辑落 `activity_log`；对象页「历史」Tab 读它们。
  `/suggest` 的 `persist` 参数仍然只管「要不要把对方那条新消息写进记忆」，**与建议留存无关** ——
  建议一律留存，这是两条独立的线。

---

## 7. 自检（`selfcheck.py`）

8 项，顺序在 `run()` `256-265`。三档语义 `7-11`，`worst` 计算 `59-65`，`as_dict()` `76-81`。

| key | label | 可能状态 | 分组 | 行 |
|---|---|---|---|---|
| `runtime` | 运行环境 | ok | 运行环境 | 90-96 |
| `storage` | 数据目录可写 | ok/fail | 运行环境 | 112-119 |
| `log_dir` | 日志目录 | ok | 运行环境 | 121-127 |
| `frontend` | 界面资源 | ok/fail | 运行环境 | 133-140 |
| `docs` | 文档 | ok/warn | 运行环境 | 142-149 |
| `database` | 记忆库 | ok/warn(空库)/fail | 核心能力 | 162-186 |
| `llm` | 大模型 | ok/warn(mock)/fail | 核心能力 | 194-218 |
| `embedder` | 记忆检索 | ok/warn(hash)/fail | 核心能力 | 226-250 |

`/api/selfcheck`（`routes_admin.py:38-45`）额外附 `runtime`（`context.runtime_info()` `139-146`）与 `paths`（`runtime_paths()` `268-275`）。
前端消费：`loadSelfCheck` `1954-2013`（分组渲染）、顶部横幅 `updateWizard` `2016-2034`（localStorage 记关闭）、`updateSteps` `2039-2078`、跳转映射 `CHECK_ACTION` `1945-1949`（只对 llm/embedder/database 给跳转按钮）。
侧栏有两个红点：`#nav-badge`（设置）与 `#nav-collect`（采集），以及 **`#side-warn` 自检提醒区**（`512` 附近）。

**外设需求 1（把自检提醒放到侧栏）已在 0.4.0 落地**：`renderSideWarn(r)` `3034`
把 warn / fail 的项渲染成侧栏底部的可点按钮（`data-sw` 带跳转目标），点一下 `goView` 到
设置页对应位置；**全绿时整条隐藏**。在此之前自检结果只有进设置页才看得到。
另有非 HTTP 通道：`desktop.py --check` 把报告写到 `DATA_DIR/logs/selfcheck.txt`（`258-298`）。

---

## 8. 测试 / CI / 发版

- 测试：`backend/tests/` 20 个文件，**从 `backend/` 目录跑**（`conftest.py` 在那里重定向 `DATA_DIR`；从仓库根跑单文件会因 rootdir 落到 `backend/tests` 而跳过 conftest）。
  ```bash
  cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q     # 235 passed, 1 skipped（0.5.0）
  ```
- **0.5.0 新增/扩写的守卫**：
  - `tests/test_adapter_import.py`（新，10 项）：**导入这条路本身能不能走通** ——
    仓库自带的两份示例文件各经其适配器导入（QQ / 微信）、界面写的粘贴格式真能用、
    粘 JSON 也能认、列名匹配拿真实列名、解析为空时提示能指路、
    `ImportResult` 回报归宿渠道、声明来源压过适配器猜测、
    以及一条**静态守卫**：`app/` 下任何模块都不许引用「既没定义也没导入」的全局名
    （用 `symtable` 看符号表）。这一条是被 `iter_blocks` 那个 Bug 逼出来的，见 §10.4。
  - `tests/test_frontend_assets.py`（11→13 项）：收集侧客户端下拉已在 0.4.0 钉住；
    0.5.0 补两条 —— **平台/来源下拉的取值必须在后端平台表里**（粘贴框 + `CHAT_PLATFORMS`），
    以及粘贴框必须给出「来源」入口。
  - `tests/test_persons.py`（24→26 项）：手建的空对象接住同名导入（复用而不是新建），
    以及反例：那个名字下**已有记录**时仍保守新建（重名不能静默合并）。
  - `tests/test_objects_center.py`（28→29 项）：`kept` 是给用户的**保证**而不是模型的成绩单 ——
    用一个「没有证据的字段就不写」的模型验证：模型一个字没提的字段，
    `kept` 里照样要有。实现方式是把 `ctx._llm` 换成只答对象级画像的桩
    （`llm` 是只读属性，换的是 `_llm` 这个缓存槽）。
  - `tests/test_version.py`（新增 1 条）：前端 `BUILD` 常量必须等于后端 `__version__`。
- **写守卫的标准**：新测试要能在**旧代码上失败**。0.4.0 的 `loadChats` 那条、
  和「缺时间=inferred」那条都实测过在修复前会挂 —— 不会失败的守卫等于没有。
  0.5.0 的六条关键守卫做了脚本化反证（把修复逐条改回旧写法 → 对应用例必须失败，
  跑完自动还原源码），六条全过：

  | 改回旧写法 | 如期失败的用例 |
  |---|---|
  | 删掉 `wechat.py` 的 `iter_blocks` 导入 | `test_bundled_samples_import_through_their_own_adapter` |
  | `_resolve_map` 拿候选表当实际列名 | `test_column_names_are_matched_against_the_real_columns` |
  | `_pick` 直接取 `probes[0]` | `test_nothing_matches_falls_back_to_the_generic_parser` |
  | `_guess_paste_suffix` 一律返回 `.txt` | `test_column_names_are_matched_against_the_real_columns` |
  | `import_file` 忽略用户声明的 `platform` | `test_declaring_the_source_beats_the_adapter_guess` |
  | `kept` 改成「模型也想改时才列入」 | `test_refine_still_reports_a_draft_field_the_model_said_nothing_about` |

  **这次反证有两个额外收获**（这才是它真正的价值）：
  1. 「`kept` 只在模型也想改时才列出来」这条改动**本来没有任何测试盯着** ——
     旧测试用的是 Mock 引擎，而 Mock 会把全部字段都填满，所以新旧写法都能过。
     为此专门加了一个「没有证据的字段就不写」的模型桩，才把这件事钉住。
  2. 第一次写的反证脚本里，「`_resolve_map`」那条改动**不够忠实**（改成了另一种坏法，
     恰好被别的修复兜住了），于是它「通过」了 —— 说明**反证本身也要被怀疑**。
- CI（`.github/workflows/ci.yml`）：push/PR 到 main，矩阵 Python 3.11 + 3.13；步骤＝语法检查 → `pytest tests/ -q` → `tests/test_smoke.py` → 冒烟编码防护 → 前端 JS 语法检查。基线耗时 ~40 秒。
- 发版：
  ```bash
  backend/.venv/Scripts/python.exe scripts/build_exe.py --zip        # 构建 dist/ + 打包（~50 秒）
  backend/.venv/Scripts/python.exe scripts/make_release.py           # 打包 + 建 tag + Release + 上传
  #  ---check 只打包印校验和；--dry-run 模拟上传
  ```
  `make_release.py` 的 token 从 git 凭据管理器取；**刻意不覆盖已存在的 Release**（重发要先在网页删）。
- **PyInstaller 构建不是逐字节可复现**（同源码两次构建，219 个文件里 2 个不同），但 **zip 打包那步是确定的**（同一 `dist/` 连打两次 SHA 相同）。发行说明里印的校验和只能用来核对**下载**，不能用来核对**构建**。

---

## 9. 合规边界（改代码时不要越线）

见 `docs/COMPLIANCE.md`。要点：只读用户**自己机器上、自己已登录**的客户端数据；
不绕过登录/加密的权限边界（密钥来自用户粘贴或进程内存，属于用户自己机器上的既有事实）；
默认只读、不写回客户端；**语音转写已于 0.2.1 撤下**（`6fb9fdd`、`acff853`），
后端入口一并移除，界面上的「通话」于 0.4.0 收进**「设置 · 关于」下的折叠区**
（不再是独立页签），说明文案保留「没有任何按钮」。

> 采集页「自动找密钥」只读**本机正在运行的客户端进程内存**，不写、不注入、不 hook。
> 这是本机用户对自己机器上既有数据的读取，不是对第三方的越权访问。

---

## 10. 已知缺陷与盲区

### 10.1 【已在 S0 修复】半自动采集「再次选中后开始监听」报 `AttributeError`

`AttributeError: 'PersonChannel' object has no attribute 'peer_name'`

**根因**：`PersonChannel`（`schemas.py:266-279`）**没有** `peer_name` / `me_name` 字段
（它是 `ChatInfo` 的子集，只有 `chat_id/channel/platform/name/source/计数/时间/indexed`），
而 `semi._resolve_names` **读了两处不存在的属性**：

```python
# semi.py:448-459
if store is not None and person_id:            # ← 守卫：person_id 非空才进
    person = store.get_person(person_id)
    if person is not None:
        peers.extend([person.name, *(person.aliases or [])])
        detail = store.person_detail(person_id)
        for ch in (detail.channels if detail else []):   # ch 是 PersonChannel
            for name in (ch.name, ch.peer_name):         # ★ 454 崩
                if name:
                    peers.append(name)
            if ch.me_name and ch.me_name not in me:      # ★ 457 同样缺字段
                me.append(ch.me_name)
```

**为什么「第一次创建」不炸、「再次选中」才炸**：
`_resolve_person_id`（`semi.py:412-432`）在库里找不到同名者时返回 `""` →
`_resolve_names` 的守卫 `if store is not None and person_id:` **短路，循环从不执行**。
第一次创建时 person 还不存在（`person_id=""`）→ 不炸；
第一次落库时 `upsert_chat` 的 `ensure_person_for_chat` 已把 person 建好并绑上 chat，
第二次选中时 `person_id` 非空 → 守卫放行 → 遍历 `person_detail().channels` → 崩。

**充要条件**：`person_id` 非空 **且** 该 person 至少绑定一个 chat。

**为什么测试没抓到**：`tests/test_collect_semi.py:124` 的 `_started()` 直接给 `sc._peer_names` 赋值（134），
绕过 `_resolve_names`；唯一走真 `start()` 的用例（595）用的 person 由 `create_person` 建、**没有任何 chat** → `channels == []` → 循环体一次都不进。

**最小修法**：`454` 的 `ch.peer_name` → `ch.name`；`457` 的 `ch.me_name` 需另取（`PersonChannel` 没有该字段）或直接不收集。
**回归测试**：构造「person 已绑定 chat」的用例走真 `start()`（现在没有这个用例）。

**S0 实际采用的修法**（比「最小修法」更稳，见 PLAN Bug 1 的「更稳的做法」）：
`schemas.PersonChannel` 补上 `peer_name` / `me_name` 两个字段（**只加不删**，前端在用的
响应模型加字段是兼容的），并在 `store.person_detail` 里从 `ChatInfo` 填进去 ——
这样「渠道视图缺字段」这一类坑一次性消掉，`semi._resolve_names` 的两处读取随之合法。
**回归守卫**：`tests/test_collect_semi.py::test_start_works_when_the_person_already_has_a_channel`
（真 `start()`；前置=建 person → `upsert_chat(person_id=pid)` → `start(store, person_id=pid)`）。
按项目标准**先在旧代码上跑过**，复现出原始 `AttributeError: 'PersonChannel' object has no attribute 'peer_name'`。

### 10.2 0.4.0 关闭掉的缺口

| 缺口 | 状态 |
|---|---|
| 消息**无增删改** | ✅ 后端 S0 补；**前端 0.4.0 接上了**（`#ob-chat` 的批量选择 / 改角色 / 改时间 / 改发送者 / 手动加一条） |
| 指挥台输出**零留存** | ✅ 后端 S0 补；**前端 0.4.0 接上了**（对象页「历史」Tab） |
| 人物后端接口齐全但**前端没用** | ✅ 0.4.0 起对象页 5 Tab 全面消费；`routes_persons.py` 的跨渠道时间线仍未接（见下） |
| 没有**人物级**事实/画像 | ✅ 数据层 + 接口 S0 补；**前端 0.4.0 落地**（对象级/渠道级分组、**分别计数不去重**） |
| 前端**没有多选/批量基建** | ✅ 0.4.0 补：聊天记录批量操作 + 指挥台渠道多选 |
| 窗口**没有关闭小窗 / 后台运行** | ✅ 0.4.0 补（`POST /api/desktop/close`） |
| 导航 7 项要合并/移除 | ✅ 0.4.0 收敛为 4 项 |
| 自动采集**没说清「怎么拿到密钥」** | ✅ 0.4.0 重写了采集页指引（含「密钥在哪」的分步说明与「自动找密钥」的预期管理） |

### 10.3 仍然存在的缺口

- `export_chat_json` **尚未**带上 `engine_runs` / `activity_log` —— PLAN 需求 11 的隐私要求是
  「导出/删除要覆盖历史输出」。**当前导出只覆盖消息、事实、画像，不含分析留存**
  （→ 历史 Tab 里删单条 `run` 是可以的，但「导出这个对象的全部数据」还不完整）。
- 自动采集（`collect/pipeline._collect_one_chat`）的 `activity_log` 写入点没有独立单测
  （需要解密后的库，成本高），靠人工核对 + 半自动那条同构守卫。
- `delete_person` 删除对象级人物设定、但**不删**对象级事实（假设：事实属不可再生记忆资产，
  与「删人不删消息」一致）；如需一起删请明确。
- `routes_persons.py` 的**跨渠道时间线**接口前端仍未使用（对象页概览用的是对象 overview）。
- **Prompt 调优**仍是最大的质量缺口：默认 Mock 引擎让链路能跑，但建议内容是空的。

### 10.4 【0.5.0 修复】三个「静默坏了很久」的问题

这一轮最值得记的不是新功能，而是**三个一直坏着、但没有任何东西会报错的问题**。
它们有共同的形状：**编译不报、导入不报、界面照常打开**，只有真走到那一条路时才出问题 ——
而那条路恰好没有测试。

| 问题 | 坏在哪 | 为什么没人发现 | 现在的守卫 |
|---|---|---|---|
| **导入微信记录必崩** | `wechat.py` 用了 `iter_blocks` 却从未导入它（从项目**第一个提交**起就这样） | 当时**没有任何测试跑过微信适配器**；自带的 `samples/wechat_sample_阿哲.txt` 也是这份坏代码的一部分 | 两份示例文件端到端导入 + `symtable` 静态守卫（见下） |
| **通用 JSON / CSV 列名匹配失效** | `_resolve_map` 把「候选列名表」当成「数据里实际有的列名」传进去，于是永远只返回候选表的第一个名字 | 只测过「列名恰好等于候选表首项」的形状，而模块文档里写的 `who` / `content` 从来没被测过 | `test_column_names_are_matched_against_the_real_columns`（5 种形状） |
| **粘贴框读不了自己写的格式** | 界面写着「`时间 昵称` + 内容」，默认适配器却是只认 JSON / CSV 的 `generic` | 前后端各测各的，没人把「界面上的示范格式」当成输入去跑一遍 | `test_the_paste_format_documented_in_the_ui_actually_parses` |

**「删掉一个没用的桩」这类改动要特别小心。** `iter_blocks` 在 `qq.py` 里是正常导入的，
`wechat.py` 里少了这一行 —— 从 diff 上看只差一行 import，很容易被当成「多余」删掉或漏加。

**静态守卫怎么做**（`test_adapter_import.py::test_no_module_references_a_global_that_was_never_defined`）：
用标准库 `symtable` 编译每个模块，对每个作用域取符号表，找「`is_global()` 且被引用、
但本模块既没赋值也没导入、又不是内置名」的符号。Python 自己已经算出了这个事实，
我们只是把它问出来。**带 `import *` 的文件跳过**（星号导入会让符号表失真）。

---

## 11. 当前版本与发布

- 版本号：`backend/app/__init__.py` 的 `__version__ = "0.5.0"`（**改版本只改这一处**；
  前端 `frontend/index.html` 顶部的 `const BUILD` 必须跟着改，`test_version.py` 会盯着）
- 仓库：<https://github.com/whyao56/WingMan>（public），
  tag `v0.2.0` / `v0.2.1` / `v0.3.0` / `v0.3.1` / `v0.4.0` / `v0.5.0`
- v0.5.0 Release：<https://github.com/whyao56/WingMan/releases/tag/v0.5.0>
  附件 `WingMan-0.5.0-win64.zip`（221 个文件，33.8 MB，解压后约 77 MB），
  SHA256 `d96894fe2a41eb4091357c6938ebe87a84cbe0082f790c3ce698aadc93d0b184`
  （**实测同一份 `dist/` 连打两次 SHA 相同**，所以这个值是可核对的事实）
- **本地 tag 可能是旧的**：这个仓库只在远端有 `v0.3.x` / `v0.4.x` 标签（本地只推过 `v0.2.x` 时
  容易误判成「没发过版」）。查远端用 `git ls-remote --tags origin`，别只看 `git tag`。
- 检查更新查的是 GitHub Releases API（`routes_admin.py` 的 `/api/update/check`）。
  **查不到时不给 `has_update` 字段**，提示「不等于已是最新」—— 这是刻意的，别改成默认「已是最新」。
- **历史遗留**：曾用 `git-filter-repo` 重写过历史清掉真人姓名；旧 SHA 仍能被 GitHub 缓存视图取到，
  彻底清除需删库或联系 Support（**尚未做**，注意现在删库会连带删掉已发布的 Release）。
  隐私守卫测试：`backend/tests/test_privacy_pseudonyms.py`（扫源码 + `git log -S` 查历史）。

---

## 12. 0.4.0（对象中心）已落地

> 这一版把 S0（后端打底）→ S1/S2/S3（前端三个方向）→ S4（收尾）一次做完。
> 计划文档见 [PLAN-对象中心迭代.md](PLAN-对象中心迭代.md)；面向用户的说明见
> [releases/v0.4.0.md](releases/v0.4.0.md)。

### 12.1 后端

1. **Bug 1 修复**：`schemas.PersonChannel` 补 `peer_name` / `me_name`（只加不删），
   在 `store.person_detail` 里从 `ChatInfo` 填进去 —— 「渠道视图缺字段」这一类坑一次性消掉。
   回归守卫 `test_collect_semi.py::test_start_works_when_the_person_already_has_a_channel`
   **真的调 `start()`**（旧测试绕过了出事那一段），且实测在旧代码上会复现原始 `AttributeError`。
2. **迁移与新表（幂等）**：`messages.captured_at`、`facts.person_id`（放开 `chat_id` 可空、
   回填、去重、部分唯一索引 `ux_facts_person`），新表 `person_personas` / `engine_runs` /
   `sim_runs` / `activity_log`。
3. **多渠道上下文**（`engine/context.py` + `engine/pipeline.py`）：
   `build_context` / `run_analysis` 接受 `chat_ids` 序列，主渠道决定人格/昵称/检索锚点，
   事实/摘要/近期消息跨渠道合并、检索逐渠道做。单渠道旧路径完全兼容。
4. **新接口**：`POST /api/persons/{id}/suggest`（多渠道分析）、
   `GET /api/desktop/state`、`POST /api/desktop/close`、`POST /api/desktop/show`、
   `GET /api/update/check`。
   S0 另有：`PATCH/DELETE /api/messages/{id}`、`POST /api/messages/bulk`、
   `POST /api/chats/{id}/messages`、`GET /api/persons/{id}/overview|facts|persona|history`、
   `POST/DELETE /api/persons/{id}/facts`、`GET/DELETE /api/history/runs/{id}`、
   `PATCH /api/chats/{id}` 扩展 `platform`/`channel`。
5. **时间语义**（需求 3）：`clipboard` / `relative` / `inferred` / `assumed` / `manual` 五种来源，
   默认从「抓取时刻」改成「按会话时间线推定」，见 §5.3。
6. **写入点**：`run_analysis`→`engine_runs`、`simulate`→`sim_runs`、
   导入/自动采集/半自动采集/批量编辑→`activity_log`。

### 12.2 前端（`frontend/index.html`，4653 行）

- 导航 7→4；旧深链接 `?view=memory/import/check/voice` **做重定向**（`LEGACY_VIEW`）。
- 左栏两级（对象 → 渠道）；对象页 5 Tab；指挥台对象+渠道多选；聊天记录批量编辑；
  采集页并入导入（拖放 + 多文件）；设置页并入体检与「关于」；侧栏自检提醒；
  右上角 × 的关闭小窗。
- **踩到并修掉的一个 P0**：重构把 `loadChats` 并进 `loadObjects` 时漏改 5 个调用点。
  它在 `init()` 里抛 ReferenceError 会让后半段初始化全不执行，在指挥台里会把成功结果
  **覆盖成错误提示**。已修，并加守卫 `test_no_calls_to_undefined_functions`（经过反证）。

### 12.3 未做 / 存疑

- **自动采集取密钥在本机仍跑不通**：实测（QQ NT 9.9.20.37051 / 微信 4.1.13.12）按 SQLCipher
  规格穷举 8378 万候选未命中，十六进制候选 QQ 8 个全灭、微信 0 处。
  0.4.0 改的是「取不到时多快、多如实地说出来」，不是「一定能取到」。
  **半自动仍是最确定能跑通的通道。**
- 导出不覆盖分析留存（见 §10.3 第 1 条）。
- 「对象级 / 渠道级事实分别计数、不去重」是**已定的展示规则**（用户确认过）；
  如果以后想改成合并去重，要同时改后端 `list_facts_for_person` 与前端两组计数。

---

## 13. 0.5.0（先填后补 + 其他聊天 + 三个静默 Bug）已落地

> 面向用户的说明见 [releases/v0.5.0.md](releases/v0.5.0.md)；被修掉的三个静默问题见 §10.4。

### 13.1 后端

1. **对象可提前新建，且空对象会接住之后的同名导入**（`store.ensure_person_for_chat`）：
   判据是 `_channel_count(person_id) == 0`（新增的辅助方法）。空对象里没有数据，
   接住它不可能混掉谁的记忆；**那个名字下已有记录时仍退回保守策略（新建）**，
   把合并留给用户显式发起。这一条同时修掉了「先建对象再导入 → 变成两个同名对象」。
2. **对象级画像整理**（`memory/profiler.build_person_profile` + `POST /api/persons/{id}/refine`）：
   跨渠道读全部消息与事实，逐渠道建索引 / 抽事实（各自 try/except，一个失败不拖垮其它），
   事实按 `对象级 / 渠道级` 标注，抽样**按渠道配额**（`per_chat = max(10, PROFILE_SAMPLE // len(channels))`）。
   合并策略：`_USER_FIELDS = (goal, stage, taboos, my_style)` 有值且非 `overwrite` 就进 `kept`，
   `_MODEL_FIELDS = (peer_profile,)` 允许改写；返回 `kept` / `updated` 两组字段名。
   **`kept` 的判定不看模型有没有给新值** —— 它是给用户的一句保证，不是模型的成绩单（见 §8）。
3. **「其他聊天」渠道类型**：`_CHANNEL_BY_PLATFORM` 加 `other→other` / `generic→other`；
   `_channel_from_platform` 兜底从 `generic` 改成 `other`；新增 `_norm_channel(channel, platform)`
   让**显式传入**的渠道名也过映射；迁移把历史 `generic` 行 UPDATE 成 `other`。
   `SEMI_CLIENTS = ("qq", "wechat", "wechat3", "other")` **刻意不复用 `SUPPORT_MATRIX`** ——
   后者回答「能不能读加密库」，其他聊天读不了但剪贴板采得了。
4. **导入侧三处修复**（`adapters/`）：
   - `wechat.py` 补 `iter_blocks` 导入（**从第一个提交起就缺**，见 §10.4）；
   - `generic._resolve_map(options, rows)` 改成拿**真实列名**去挑，
     `_auto_map_from_rows` 改成按列顺序 + 先定内容列再定说话人列（原来用 `set` 遍历，顺序不确定）；
   - `registry._guess_paste_suffix` 从粘贴内容推后缀（`.json` / `.csv` / `.tsv` / `.txt`）；
     `_pick` 在最高分为 0 时兜底 `GenericAdapter` 而不是听凭注册顺序落到 QQ。
5. **`ImportResult` 新增 `platform` / `channel`**（只加不删），让导入结果回报归宿渠道；
   `import_file` / `import_text` 新增 `platform` 参数，用户声明的来源压过适配器猜测。
   **不认得的平台：渠道兜底 `other`，平台原样保留** —— 与 `openChatSettingsDialog`
   的「（保持原样）」是同一条规矩，理由不同（见 §13.3）。
6. **版本防呆**：`desktop.desktop_state()` 增加 `version / started_at / uptime_seconds /
   mode(exe|source) / pid`；`main.py` 新增 `NoStoreHtmlMiddleware`（只给 `text/html`
   补 `Cache-Control: no-store`，不覆盖已有头）。

### 13.2 前端（`frontend/index.html`，4653 行）

- 左栏：`232px → 248px` 栅格；新增「＋ 新建」与搜索框（对象 < 6 个时隐藏搜索）；
  `.objitem` 改为两行（名字 + 关系/时间/渠道数），空对象标「还没有聊天记录」。
- 对象页：新增对象头（名字 + 关系/阶段/近况 + 去指挥台分析 + 对象设置）；
  空状态区分「一个对象都没有」与「选中的对象还没记录」两种。
- 对象右键菜单（打开 / 设置… / 重命名… / 合并到… / 删除对象…）+ 键盘等价。
- 「记忆」Tab 加「让 AI 帮我整理」与「连我手写的那几项也一起改写」。
- 「检查更新」的**旧进程横幅** `#ver-warn`（`const BUILD` vs `/api/health.version`）。
- 文案：「会话」→「渠道」；导入页「其他聊天记录」不再声称支持「通用文本」；
  粘贴框加「来源」下拉；结果提示带上归宿渠道。

### 13.3 未做 / 存疑

- **`_pick` 兜底 `generic` 会让「所有适配器都打 0 分」的输入落到通用解析器**。
  这是一次刻意的取舍：它能给出最能指路的失败提示，代价是**极端情况下**（某个适配器
  的嗅探规则退化成 0 分而它本来能解析）会少一点容错。若将来出现这种退化，先修 `sniff`。
- **不认得的平台保留原样**意味着库里可能出现 `telegram` 这样的平台值（渠道仍归 `other`）。
  界面下拉只提供已知值，所以这条路只有直接调 API 才会走到；留原样是为了不静默改用户输入。
- `.tsv` / `.csv` 的判定依赖「首行能切出 ≥2 列且**列数一致**或**含已知列名**」。
  粘贴一张**只有一行**的表头 + 一行数据、且列名完全自定义时，可能退回 `.txt` 而读不出来 ——
  此时用户改走「导入文件」即可（那条路带真正的扩展名）。
- 导出仍未覆盖 `engine_runs` / `activity_log`（见 §10.3）。
- 自动采集取密钥在本机仍跑不通（见 §12.3）。
