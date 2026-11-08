# v0.11.0 设计：对话监控 + 任务 / 对话抽屉 + 外观（2026-11-08）

## 目标
1. 不经过代理，直接读各客户端自己写在本机的对话记录（JSONL / SQLite），每轮对话结束后由「整理模型」判断有没有值得记的东西。
2. 主页加两个抽屉：AI 对话抽屉（主人用自然语言指挥 AI 管理星愿）、任务抽屉（星愿自己的任务 + 从客户端捕获的任务）。
3. 设置新增「对话监控」「外观」两页。

## 硬约束（主人定的）
- 关窗即停服务，不做托盘、不常驻。监控线程只活在服务进程里。
- 不依赖代理。监控靠读客户端本地记录；Stop 钩子只是「提醒快一点」，没有钩子也能靠轮询捕获。
- 监控 AI 没有对话上下文：每次只看「新的一轮」（+ 该会话的未闭环任务摘要），输出 JSON。
- 轻：只 stat（mtime/size）轮询，几秒一次；单个工作线程；偏移量落盘，不重复读；下次启动从偏移量补读（关着的时候聊的也不丢）。
- 核心记忆规则不变：自动通道绝不直接改核心；重要偏好 → 核心提案；普通偏好 / 事实 → 普通星星。
- 未闭环任务：按类别存碎片，标签「未闭环」，按 客户端+会话+任务 归组；看到任务完成（或主人标记）→ 合并成一个完整、可复用的任务（适合就存成技能，否则存成带「任务」标签的星），状态 closed；失败 → failed。日常偏好不算任务。

## 结构
```
app/watch/
  turns.py     Turn 数据结构、截断、琐碎判断
  adapters.py  各客户端：发现文件 + 解析成 Turn（带字节偏移）
  opencode.py  OpenCode SQLite（只读 mode=ro，自动识别 1.x message/part 与 2.x session_message）
  state.py     偏移量 soul/watch/offsets.json（原子写）
  store.py     watch_tasks / watch_fragments 两张表
  extract.py   整理模型抽取 + 落库（星 / 核心提案 / 任务）+ 闭环合并
  watcher.py   轮询线程 + 单工作队列 + 立即归纳 + notify + inbox 补读
  hooks.py     各客户端 Stop 钩子安装 / 卸载（备份、幂等、干净卸载）
  notify.py    `--notify <client>`：读钩子 JSON → POST /api/watch/notify；服务没开就写 soul/watch/inbox.jsonl；永远 0 退出
app/jobs.py    星愿自己的任务记录（归纳、对话指令、整理……）
app/agent.py   对话抽屉后端：默认对话模型 + 工具调用
```

### 一轮（Turn）
`Turn{client, session_id, cwd, ts, user_text, assistant_text, tool_summary, turn_complete, start, end}`。
JSONL 类：从「真正的用户消息」开始，到下一条用户消息之前结束。最后一轮未完成时，偏移量停在这一轮开头（下次只重读这一轮）；以下任一情况视为完成：后面出现新的用户消息 / Codex `task_complete` / 收到 notify / 文件静止超过 `idle_sec`。

### 各客户端格式（核实情况）
| 客户端 | 位置 | 格式 | 核实 |
|---|---|---|---|
| Claude Code | `~/.claude/projects/<slug>/<session>.jsonl` | 每行 `type: user/assistant`，`message.content` 字符串或块（text / tool_use / tool_result） | Stop 钩子输入字段（session_id、transcript_path、cwd、last_assistant_message）已对照官方 hooks 文档；JSONL 行结构按常见实现，**未在官方文档逐字段核实** |
| Qoder | `~/.qoder/projects`、`~/.qoder-cn/projects`（`QODER_CONFIG_DIR`） | 同 Claude | **未核实**（按主人给的信息） |
| WorkBuddy / CodeBuddy | `~/.workbuddy/projects`、`~/.workbuddy-ai/projects`（`WORKBUDDY_CONFIG_DIR`）、`~/.codebuddy/projects` | 平铺记录：`type: message`（role user/assistant）、`reasoning`、`function_call`、`function_call_result`… | 记录类型与目录来自第三方实测资料；钩子在 `settings.json` 的 `hooks`（CodeBuddy 官方 hooks 文档）；content 具体形状**未核实**，解析做了兼容 |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（`CODEX_HOME`） | `{timestamp,type,payload}`；session_meta / turn_context / response_item / event_msg（user_message、agent_message、task_complete） | 已对照 openai/codex 源码测试与第三方格式说明；notify（顶层 `notify=[...]`，单个 JSON 参数，`agent-turn-complete`）已对照官方 Advanced Config |
| OpenCode | `~/.local/share/opencode/opencode.db`（`XDG_DATA_HOME`） | 1.x：`session / message / part`，`data` 为 JSON；2.x：`session_message(type, seq, data)` | 1.x 表结构已对照源码；2.x `data` 内容**未核实**（尽力解析）；插件：v1 写法放 `~/.config/opencode/plugins/starwish.js`，2.x 插件体系变了，**未核实能否加载**——轮询仍能捕获 |
| DeepSeek Harness (dsh) | 无默认路径，需在设置里填会话根目录 | 每会话一个 `session.jsonl` 或 `session.jsonl.zstd`（默认压缩，`text-chunks` 等打包行） | 存储层次来自官方仓库与手册；事件名**未核实**，实验性：只读 `.jsonl`（装了 zstandard 才读 `.zstd`）；推荐用官方 `dsh-hooks-claude-code` 桥接 Claude 的 Stop 钩子 |

### 抽取
整理模型（`maintenance.chat`，按协议走 OpenAI / Anthropic / Responses），严格 JSON：
`{"items":[{"kind":"preference|fact|task|none","text","category","importance","task_key","task_status":"open|closed|failed"}]}`。
- preference：importance ≥ `core_importance`(默认 4) → 核心提案（preferences 段）；否则 → 星星（标签 偏好 + 类别）。
- fact → 星星。来源 `watch:<client>`。
- task → `watch_tasks`（键 = client:session:task_key）+ `watch_fragments`；同时维护一颗带「未闭环」标签的星（方便召回「上次做到哪」）。closed → 合并（模型归纳，失败退回拼接）→ 适合复用就建技能（trust=agent-created，走技能安全扫描），否则星改为「任务」标签；failed → 标签「失败」。
- 未配置整理模型时不推进偏移量，配好后自动补上。单轮失败 3 次跳过。

### 触发
- 轮询：`poll_sec`（默认 3 秒）只 stat 热文件；`rescan_sec`（默认 60 秒）重新列目录找新文件。第一次启用某客户端时，已有文件从末尾开始（不回灌历史）；之后新出现的文件从头读。
- `POST /api/watch/notify {client, session_id?, transcript_path?, last_assistant_message?}`：把对应文件标记为「本轮已结束」并立刻排队。
- `--notify <client>`：exe / desktop.py 参数；读 stdin（Claude 类）或最后一个参数（Codex）里的 JSON，POST 到运行中的服务（端口从 state.json 的 run_port 或配置取），失败写 inbox，0 退出、不阻塞客户端。启动时服务先补读 inbox。

### 钩子安装
备份到 `soul/watch/hook-backups/`；幂等（先删旧的星愿条目再加）；卸载只删星愿条目，清掉空壳。识别标志：命令里有 `--notify` 且含 `星愿记忆` / `starwish` / `desktop.py`；Codex 行尾注释 `# starwish-notify`；OpenCode 插件文件头 `starwish-notify`。

### 任务与抽屉
- `/api/tasks`：`jobs`（星愿自己的：归纳、对话指令、整理）+ `client_tasks`（捕获的任务）。颜色：完成绿、进行中/未闭环橙、失败红。前端每 4 秒轮询。
- `/api/agent/chat`：仅主人；默认对话模型 + 工具：搜索记忆、增 / 改 / 删星、读核心、改核心（面板是主人通道，未上锁直接生效，上锁转提案）、提核心提案、技能列表 / 查看、立即归纳、开关某客户端监控、读状态。最多 6 轮工具调用；没配模型时支持 `/状态` `/归纳` `/搜索 关键词` 三个离线指令并提示去配置。

### 外观
配置段 `ui`：theme（light/dark/system）、accent（预设绿 #00B96B 默认、红、橙、蓝、紫 + 自定义 hex）、scale（百分比）、chat_side、task_side。`web/theme.js` 在所有页面 head 里同步应用（先用 localStorage 缓存，防闪烁，再拉 /api/settings 校正），通过 CSS 变量与 `html[data-theme]` 生效，缩放用 `zoom`。

## 风险
- 各客户端格式会变：解析全部容错，解析不了的行跳过；未核实项在 CHANGELOG 标明。
- 每轮一次模型调用会花钱：琐碎轮跳过；可按客户端关闭；总开关 `watch.enabled`。
- 钩子改用户配置文件：有备份 + 干净卸载；JSON 解析失败时拒绝改写。

## 追加需求（同日，主人补充，已并入 v0.11.0）

### A. 两种 AI 角色
- **监控子代理**（watch/extract）：不带上下文，只盯客户端、只抽这一轮。
- **主模型**（默认对话模型）：整理星系——内容相同的星交给 `fusion.py` 融合；内容不同但同一类的星放到一起。默认上下文窗口 `llm.context_tokens = 400000`。

### B. 银河系星图（app/galaxy.py、app/organize.py）
- 星系核 = 核心记忆（原点金色主核）；恒星系 = 大类 `cat_major`；行星系 = 小类 `cat_minor`；星球 = 一条记忆。`stars` 表新增两列（旧库自动升级）。
- 主模型分类：只把未分类的星按预算打包给模型（`context.pack`），输出 `{"assign":[…],"merge":[…]}`；先分类再合并类别；融合星继承成员最多的类别；没配主模型按标签粗分。
- 布局可复现：恒星系按星数螺旋排在星系核外，行星系以黄金角散在恒星系周围，星球绕行星系（越新越靠近）。`/api/stars/map` 每颗星仍是 8 个字段，另加 `galaxy: {pos, cat, systems}`；前端按它摆星、按大类着色、画恒星系 / 行星系名字。
- 触发：对话监控归纳出新星后排队整理（防抖，任务抽屉可见）；设置「通用」里的按钮；对话抽屉里说「整理星系」。

### C. 上下文与自动压缩（app/context.py）
- 对话抽屉有上下文：历史存 `soul/agent/chat.json`，按窗口预算取最近的。主模型整理也累积自己的决定（`soul/agent/organizer.json`）。
- 累计到 `llm.compact_tokens`（默认 380,000，且不超过窗口）→ 较早历史压成一条「【之前对话的摘要】」+ 保留最近约 15% 窗口的轮次；模型压缩失败用朴素摘要兜底；记 `context_compact` 事件并在任务抽屉显示「上下文压缩」。

### D. 数据（app/datasync.py，设置 → 数据）
- 数据目录：应用数据 / 日志目录 + 打开按钮、日志保留期（启动清理一次，之后每 12 小时）、清除缓存（显示大小）、重置数据（输入「重置」确认，先自动快照）。
- 存储只做三种：本地（指定文件夹完整快照）、WebDAV（沿用 sync.py 的加密）、云服务器（自己服务器上的星愿：地址 + 主人令牌，`GET/POST /api/sync/delta`）。坚果云、S3 显示为「即将支持」。
- 增量：星新增 `uid` 列；变化时间 = max(创建, 修改, 隐藏)；水位线存 state.json，成功后才推进。合并按 uid、较新的覆盖；同步来的核心记忆只变成提案。已知局限：融合关系不跨设备同步。
- 定时：每天 03:00（北京时间，固定 +08:00，不依赖 tzdata）。判断「上次成功早于最近一个 03:00」即到期——所以关窗错过的，下次启动立即补一次；失败不推进、下一分钟重试；每次都进任务抽屉。

### E. 设置界面重做（仿 Cherry Studio）
整页设置视图（左上「← 返回」）；左栏分组导航（基础 / 工具 / 偏好，可搜索），选中项浅灰圆角底；有子页的「数据」多一栏子导航；右侧白色圆角卡片（粗标题、细分隔线、左标签右控件、灰色说明、主题色开关、红色危险按钮）。所有改动自动保存。浅色 / 深色都适配，跟随主题颜色。
