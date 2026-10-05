# WingMan 项目现状（交接文档）

> **用途**：给「全新上下文」的窗口看的一份现状底稿。目标是**读完不用再通读代码**就能动手改。
> **快照**：2026-10-05 23:20（GMT+8），对应提交 `1e73d01`，版本 `0.3.1`，CI 全绿。
> **注意**：文件行号会随改动漂移。改动前先用函数名定位，再用行号确认。
> 本文档答的是「现在是什么样」；「要改成什么样」见 [PLAN-对象中心迭代.md](PLAN-对象中心迭代.md)。

---

## 0. 一句话

WingMan 是一个**跑在本机的聊天记录分析工具**：把微信/QQ 的聊天记录导进来或采进来，
交给大模型做「记忆 + 人物画像 + 回复建议」，通话场景下实时给该怎么说。
数据全部落在本机 SQLite，程序不主动外传任何东西（模型调用按用户自己配的 endpoint 走）。

- 形态：Python FastAPI 后端 + **单文件 HTML 前端**（`frontend/index.html`，2787 行，内联 JS，无构建步骤）
- 分发：PyInstaller 打包成免安装 zip（`WingMan.exe` + `_internal/`），当前 **v0.3.1**，33.7 MiB
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
│  │  ├─ store.py          1257  ★ 全部 SQLite 读写（9 张表）+ 迁移
│  │  ├─ schemas.py         319  ★ 全部 Pydantic 模型
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
│  ├─ tests/                17 个测试文件，全量 159 passed + 1 skipped
│  ├─ data/                 运行时 SQLite（.gitignore；含 collect_cache/）
│  └─ logs/wingman.log
├─ frontend/index.html      2787 単文件前端
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
| `messages` | `75-91` | id PK AI, chat_id FK(CASCADE), platform, sender, role(me/peer/system), ts, msg_type, text, ext_id, **ts_source** | **UNIQUE(chat_id, sender, ts, text)** 是幂等键（`90`）+ `INSERT OR IGNORE` |
| `embeddings` | `96-101` | message_id PK, model, dim, vec BLOB | float32 原始字节 |
| `facts` | `48-60` | id, chat_id FK, subject(peer/me/relationship), key, value, confidence, evidence, updated_at | UNIQUE 是**四列**（chat_id, subject, key, value） |
| `summaries` | `105-113` | id, chat_id, kind(daily/weekly/milestone), period, content, created_at | L2 记忆 |
| `personas` | `115-123` | **chat_id PK**, goal, my_style, peer_profile, taboos, stage, updated_at | ★ **画像挂在 chat 上，不挂 person** |
| `kv` | `125-128` | key PK, value | 运行时设置覆盖 |
| `persons` | `136-149` | id PK(`p_`+12hex), name, aliases(JSON), relation, desired_relation, stage_goal, notes, created_at, updated_at | ★ 人物是一等实体，但前端几乎没接（只用了 `GET /api/persons`） |
| `collect_cursors` | `158-177` | id, platform, account, peer_key, person_id, chat_id, last_ts, last_ext_id, fingerprint, merged_count, collected_from, last_run_at, status, message | UNIQUE(platform, account, peer_key) |

**没有**任何「输出留存」表 —— 指挥台的建议/推演**返回即丢**（见 §7）。

### 必须记住的不变量（踩了会静默出错）

1. **chat 必须有人**：`upsert_chat` 末尾强制 `ensure_person_for_chat`（`store.py:449`；规则「有归属就用、没有就按『对方称呼 > 会话名』新建」，`451-479`）。新写入路径漏了这条，人物列表就是空的。
2. **chat_id 生成规则有两套**：导入 `{platform}-{slug}-{md5[:6]}`（`adapters/registry.py:153-158`）、采集 `{client}:{account}:{peer_key}`（`collect/pipeline.py:277-284`）。**不要新造第三套**。
3. **消息没有增删改**：store 只有 `insert_messages`(946) / `list_messages`(964) / `all_messages`(1017)。想「二次编辑聊天记录」必须新增方法（见 PLAN）。
4. **facts / personas 都以 chat_id 为归属键**，人物级别的事实与画像**目前不存在**。
5. **persona 与 persons 未打通**：`persons` 的关系字段只由用户手填，模型永远不写。

### 存储方法速查

| 域 | 方法（行） |
|---|---|
| 会话 | `upsert_chat`406 `get_chat`481 `list_chats`520 `delete_chat`530 `rename_chat`535 `set_roles`543 |
| 人 | `create_person`565 `get_person`578 `find_person_by_alias`585 `list_persons`601 `update_person`663 `bind_chat`698 `unbind_chat`709 `merge_persons`713 `delete_person`751（**只解绑不删消息**）`person_detail`762 `person_messages`786 |
| 消息 | `insert_messages`946 `list_messages`964 `recent_messages`980 `messages_by_ids`983 `last_peer_message`993 `search_text`1002 `all_messages`1017 |
| 向量 | `upsert_embeddings`1026 `unembedded`1044 `load_embeddings`1057 |
| 事实/摘要/画像 | `replace_facts`1088 `upsert_fact`1106 `list_facts`1125 `delete_fact`1143 `add_summary`1149 `list_summaries`1161 `get_persona`1176 `save_persona`1187 |
| 游标 | `get_cursor`813 `list_cursors`830 `save_cursor`854 `reset_cursor`894 `merged_message_count`914 `messages_fingerprint`924 |

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
`POST /import/preview`46 · `POST /import`67 · `POST /import/text`97 ·
`GET /chats`122 · `GET /chats/{id}`127 · `DELETE /chats/{id}`132 · `PATCH /chats/{id}`139（改 name/peer_name/me_name；可选 `me_names` 批量纠正角色）·
`GET /chats/{id}/messages`158 · `GET /chats/{id}/export`167 · `POST /chats/{id}/index`176 ·
`GET/POST /chats/{id}/facts`187/193 · `DELETE /chats/{id}/facts/{fid}`210 ·
`GET/PUT /chats/{id}/persona`217/223 · `POST /chats/{id}/profile`231

### 人物 `routes_persons.py`（**除 GET /persons 外前端全未接**）
`GET /persons`34 · `POST /persons`39 · `GET /persons/{id}`72 · `PATCH /persons/{id}`80 ·
`DELETE /persons/{id}`93 · `POST /persons/{id}/merge`108 · `GET /persons/{id}/messages`128 ·
`POST /persons/{id}/channels`147 · `DELETE /persons/{id}/channels/{chat_id}`163

### 指挥台 `routes_engine.py`
`POST /chats/{id}/suggest`42 · `POST /chats/{id}/analyze`61 · `POST /chats/{id}/simulate`78

### 设置/自检 `routes_admin.py` + `routes_health.py`
`GET /health`23 · `GET /selfcheck`38 · `POST /runtime/open`48 · `GET /settings`73 · `PUT /settings`93 ·
`POST /settings/reset`106 · `POST /settings/test`113 · `GET /settings/models`129 ·
`GET /health/details`201（前端未调用）

---

## 4. 前端结构（`frontend/index.html`）

### 导航（重要：**用 `data-v`，不是 `data-page`**）
`#nav button[data-v]` `417-425`；切换 `goView(name)` `1345-1351`：高亮 toggle class `on`，
容器 id = `view-` + name；进采集页调 `collectOnEnter()`，离开调 `semiPollStop()`。
深链接 `?view=settings` `2782-2783`。

| 导航项 | data-v | 容器 | 主渲染函数 |
|---|---|---|---|
| 指挥台 | `copilot` | `#view-copilot` 449 | `renderBundle`1515 `analysisHTML`1432 `optionHTML`1488 `simHTML`1560 |
| 记忆 | `memory` | `#view-memory` 481 | `loadPersona`1618 `loadFacts`1629 `loadMessages`1654 |
| 导入 | `import` | `#view-import` 543 | `doPreview`1754 `doImport`1771 `previewHTML`1735 |
| 采集 | `collect` | `#view-collect` 600 | `loadClients`2243 `paintClients`2260 `paintMatrix`2296 `paintReport`2321 `paintSemi`2533 `loadCursors`2739 |
| 通话 | `voice` | `#view-voice` 700 | **无（纯静态占位，无按钮）** |
| 自检 | `check` | `#view-check` 822 | `loadSelfCheck`1954 `updateWizard`2016 `updateSteps`2039 |
| 设置 | `settings` | `#view-settings` 845 | `loadSettings`1831 |

常驻：品牌 `412-416`、会话列表 `#chatlist` `426-429`（标题「会话 · 右键可设置」427）、
`#health` `430-432`、新手横幅 `#wizard` `437-446`、指挥台「上手三步」`#cp-steps` 454。

### 指挥台（**单会话、无多选**）
`state` 只有 `{chats, chatId, bundle, check}`（`937`）。选会话 `selectChat`1422 →
`reflectChat`1410 切显隐 → 贴对方消息 `#cp-msg`465 → `#cp-go`467 →
`POST /api/chats/{id}/suggest`（`1590-1605`）→ `renderBundle` 渲染进 `#cp-result`476。
每张建议卡可「复制」1534 /「推演走向」`act-sim`1541（`POST simulate`）。
**全文件唯一的 checkbox 是 `#cp-persist`470（是否存入记忆）**，与选择无关。

### 记忆页
会话信息 `#mem-id`492 / `#mem-stats`493；人物设定卡 `504-522`（`#p-goal`/`#p-stage`/`#p-taboos`/`#p-mystyle`/`#p-profile`/`#p-save`）；
事实卡 `524-532`（`#f-count`/`#facts`/`#f-key`/`#f-val`/`#f-add`，逐条删除 `.f-del` `1644-1650`）；
聊天记录 `#mem-msgs`537（**只读**，`GET messages?limit=100`）。
另有 `#mem-index`1675 / `#mem-profile`1685 / `#mem-export`1696 / `#mem-del`1699。

### 导入页
`#imp-file`549 accept=`.txt,.log,.bak,.csv,.tsv,.json,.jsonl,.html,.htm`；
字段 `#imp-adapter`553（auto/qq/wechat/generic）、`#imp-name`562、`#imp-mename`566；
按钮 `#imp-preview`570 → `#imp-go`571；粘贴区 `#paste-text`581 + `#paste-name`583 → `#paste-go`584。
**已有预览/确认两步**，导入成功跳记忆页（`1798`）。

### 采集页（四张卡）
1. 「先看你的客户端」`608-616`：`#cl-check`611 / `#cl-matrix`612 / `#cl-list`615 / `#cl-tag`609
2. 「自动采集」`618-646`：`#ac-client`627 `#ac-key`635 `#ac-check`639 `#ac-scan`640 `#ac-preview`641 `#ac-run`642 `#ac-msg`643 `#ac-report`645。
   **给用户的操作指引只有 `620-623` 两行**（「密钥只存在于运行中的客户端进程内存里…先『预览』一次最稳」）—— 用户反馈「说不清楚怎么找到密钥」指的就是这里。
3. 「半自动采集」`648-684`：`#sc-client`658 `#sc-person`665(+`#sc-persons` datalist) `#sc-time`670（`assumed`/`ask`）`#sc-start`677 `#sc-stop`678 `#sc-clear`679 `#sc-hint`682 `#sc-list`683。
   `semiStart()` `2462-2492` → `POST /api/collect/semi/start`，payload `{client, peer_name, person_id, missing_time}`。
4. 「采集进度」`686-696`：`#cur-load`692 `#cur-list`695，重置按钮动态生成 `data-cur` `2755`。

「抓取时刻」出现在 `671`（下拉选项）、`2585`/`2586`（待确认条目按钮与提示）、`2695`/`2696`（点击后文案与 toast）。

### 右键菜单 / 弹窗 / 窗口
- `contextmenu` **唯一绑定** `1387`（挂在每个 `.chatitem`）；键盘等价 `1393-1400`。
- 菜单项 `openChatMenu` `1329-1339`：打开1331 / 重命名1333 / **「会话设置…」1334** / 导出1335 / 删除1337。打开函数 `openChatSettingsDialog` `1185-1237`。
- 弹窗 `openModal` `1035-1057`：`.mhead` 只有标题+副标题，**没有右上角关闭按钮**；关闭靠遮罩1050 / Esc1067 / 取消按钮。
- **没有**「关闭程序 / 后台运行 / 托盘」任何痕迹；前端**没有** `window.close`。
- 窗口是 `desktop.py:110-119` 的 pywebview `create_window` + `start()`；**关窗即退出**（`desktop.py:249`）。

### 通用设施
`$`932 `$$`933 `esc`934 `state`937 `NET_ERR_TEXT`953 ·
**`api`957-970**（网络层 `TypeError` 时重试 2 次；4xx/5xx 不重试）· **`rawApi`972-983** ·
`toast`985 · `busy`992 · **`busyClock`1007**（按钮上显示已等待秒数）·
`openModal`1035 `modalButton`1059 `confirmWithCountdown`1079（删除用，倒计时不可确认）·
`closeCtxMenu`1131 `openCtxMenu`1135 · `exportChat`1283 `openRenameDialog`1297 ·
`loadHealth`1355 `loadChats`1372 `init`2774（loadHealth→loadChats→loadSettings→loadSelfCheck，30s 轮询 health）。

**没有历史记录 UI**（全文件仅 `814`、`1457` 两处含「历史」字样，都不是功能）。

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
ClipboardWatcher(clipboard.py:409)  0.4s 轮询 semi.POLL_INTERVAL(52)
   sequence_number() 变了才 read_text()          clipboard.py:103/117
SemiCollector._loop(191) → _tick(201) → watcher.poll(me_names, peer_names)
   parse_copied(319-392)：块状（称呼[+时间]+正文）/ 单条（需指认）
   _match_known(266) 精确匹配已知称呼，匹配不上就问用户，**绝不猜**
_ingest(220) → PendingItem；归属与时间都有依据则直接 _commit_capture(244)
_commit_capture(253-304)：
   防重 _seen_recently(532) 只在 ts_source=='assumed' 时启用（DEDUPE_WINDOW=50，264）
   upsert_chat(291-295) → insert_messages(296)
挂起的等 POST /semi/commit(306-358)，支持 apply_to_rest 批量归角色
状态存 DATA_DIR/collect_cache/semi_state.json（51，_save/_load 479/494）
```
> **「数据集」在代码里不存在** —— 它就是**一个 chat**。界面上的「渠道」= `PersonChannel` = 一个 chat。
> `_resolve_chat_id(461-475)`：优先复用该人同渠道已有 chat_id，否则 `{client}:{person_id}`，再否则 `{client}:semi:{peer_name}`。

### 5.3 「时间」现在是怎么填的（需求 3 要改的就是这里）
- `messages` 只有两列与时间有关：`ts`（`store.py:81`）+ `ts_source`（`89`）。
- **真正的「抓取时刻」载体是 `Capture.at`**（`clipboard.py:402`，赋值 `434`），它**只写进 `semi_state.json`，不进消息表**。
- `_ingest`（`semi.py:220-242`）的兜底逻辑：
  - 剪贴板**带了**时间 → `ts = c.ts`，`ts_source = 'clipboard'`（这已经是消息的真实时刻）
  - 剪贴板**没带**时间 → `missing_time=='ask'` 就挂起等用户填；否则 **`ts = now`（抓取时刻顶替）+ `ts_source = 'assumed'`** ← 需求 3 明确要去掉的行为
- 用户在确认框填的 → `manual`（`332-336`）；点「用抓取时刻」→ `assumed`（`337-341`）。
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

分层（`engine/context.py:1-14`）：**L1 事实（全量注入）+ L2 摘要（最近几条）+ L3 检索（向量+关键词动态召回）+ 最近对话 + 人物卡**。

- `memory/embedder.py`：`HashEmbedder`（字符 n-gram 哈希，永远可用，**不认近义词**）+ `CloudEmbedder`（OpenAI 兼容），工厂 `build_embedder` `212-239`。
- `memory/retriever.py`：`HybridRetriever`，`score = 0.60·语义 + 0.25·关键词 + 0.15·新鲜度`（`30-32`）；`index_chat`90 `search`121 `search_multi`196。
- `memory/profiler.py`：`build_index`47 `extract_facts`53（→`replace_facts`）`build_profile`111（→`personas`）`summarize_period`168（→`summaries`）。
- `engine/`：`pipeline.run_analysis`（`pipeline.py:21-42`，调用 `45-89`）、`analyzer.analyze`、`planner.plan`、`suggestor.suggest`（本地打分 `207-262`）、`simulator.simulate`（`74-105`）。
- **持久化现状：全部零留存。** 唯一被写库的是「对方那条新消息」（`store_peer_message` `56-58`）。`/suggest` 的 `persist` 参数只管这个消息，**与建议本身无关**。

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
侧边栏已有一个红点 `<span id="nav-badge" class="navdot hidden">`（`423`）与采集页的 `#nav-collect`（`421`）——**外设需求 1 的落点在这里**。
另有非 HTTP 通道：`desktop.py --check` 把报告写到 `DATA_DIR/logs/selfcheck.txt`（`258-298`）。

---

## 8. 测试 / CI / 发版

- 测试：`backend/tests/` 17 个文件，**从 `backend/` 目录跑**（`conftest.py` 在那里重定向 `DATA_DIR`；从仓库根跑单文件会因 rootdir 落到 `backend/tests` 而跳过 conftest）。
  ```bash
  cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q     # 159 passed, 1 skipped
  ```
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
通话页现在只剩占位文案，且明确声明「没有任何按钮、后端入口也一并移除」。

---

## 10. 已知缺陷与盲区

### 10.1 【崩溃，已定位未修】半自动采集「再次选中后开始监听」报 `AttributeError`

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

### 10.2 其它缺口（本轮迭代要补的）
- 消息**无增删改**（store 层就没有方法），聊天记录页只读。
- 指挥台输出**零留存**；没有 `engine_runs` 之类的表。
- 人物（`persons`）后端接口齐全（含合并/渠道/跨渠道时间线），但**前端只用了一个 `GET /api/persons`** —— 大量能力闲置。
- 事实与画像都挂 chat，**没有人物级的事实/画像**。
- 前端**没有多选/批量基建**（唯一的 checkbox 与选择无关）。
- 弹窗**没右上角关闭按钮**；窗口**没有关闭小窗 / 后台运行**。
- 导航 7 项里有 3 项是要合并/移除的（导入→采集、自检→设置、通话→移除）。
- 自动采集的界面指引只有两行，且**没说清「怎么拿到密钥」**；实测本机自动取密钥不可行这件事，界面也没说透。

---

## 11. 当前版本与发布

- 版本号：`backend/app/__init__.py` 的 `__version__ = "0.3.1"`（改版本只改这一处）
- 仓库：<https://github.com/whyao56/WingMan>（public），tag `v0.2.0` / `v0.2.1` / `v0.3.0` / `v0.3.1`
- v0.3.1 Release：<https://github.com/whyao56/WingMan/releases/tag/v0.3.1>
  附件 `WingMan-0.3.1-win64.zip` 35302614 B，SHA256 `f5c9557b9dc782eb5e693bd9d64bc76c42c5f104f7d041ba3e23be7cda2fdb04`
- 远端 main = `1e73d01`（本地无未推送提交）
- **历史遗留**：曾用 `git-filter-repo` 重写过历史清掉真人姓名；旧 SHA 仍能被 GitHub 缓存视图取到，
  彻底清除需删库或联系 Support（**尚未做**，注意现在删库会连带删掉已发布的 Release）。
  隐私守卫测试：`backend/tests/test_privacy_pseudonyms.py`（扫源码 + `git log -S` 查历史）。
