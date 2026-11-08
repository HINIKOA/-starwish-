# 更新记录

## v0.11.1（2026-11-08）宇宙之眼图标
- 宇宙之眼图标改为矢量眼睛（顶栏按钮、观看页标题、设置导航、密钥显示按钮），不再依赖系统 emoji 字体，避免显示成方框。

## v0.11.0（2026-11-08）对话监控、银河系星图、对话 / 任务抽屉、数据同步、设置重做
- **对话监控**（`app/watch/`）：不经过代理，直接读各 AI 客户端写在本机的对话记录。只 stat 轮询（默认 3 秒）+ 单个工作线程，偏移量存 `soul/watch/offsets.json`，关着软件时聊的下次打开补上；第一次启用从现在开始，不回灌历史。
  - 监控 AI 不带上下文：每次只看新的一轮（+ 该会话的未闭环任务摘要），严格 JSON `{"items":[{kind,text,category,importance,task_key,task_status}]}`；用「整理模型」，按服务商协议调用。琐碎轮跳过；没配整理模型时不推进偏移；单轮连续失败 3 次跳过。
  - 核心记忆规则不变：重要偏好（重要度 ≥ 4）→ 核心提案；普通偏好 / 事实 → 普通星（来源 `watch:<客户端>`）。
  - 任务：按 客户端 + 会话 + 任务 存碎片（标签「未闭环」）；闭环时合并成一个完整可复用的任务（适合就存成技能，否则带「任务」标签的星）；失败标红；主人可在任务抽屉里手动标记。
  - 客户端：Claude Code、Qoder、WorkBuddy、CodeBuddy、Codex、OpenCode（SQLite 只读）、DeepSeek Harness（实验性）。
  - 「本轮结束」钩子（设置里一键装 / 卸，先备份、幂等、干净卸载）：Claude 类 `settings.json` 的 Stop 钩子、Codex `config.toml` 顶层 `notify`、OpenCode 插件 `~/.config/opencode/plugins/starwish.js`；dsh 用官方 `dsh-hooks-claude-code` 桥接。钩子命令 `星愿记忆.exe --notify <客户端>`：服务在就 POST `/api/watch/notify`，不在就写 `soul/watch/inbox.jsonl`，永远 0 退出不阻塞；两个启动器对 `--notify` 继承标准输入输出、不显示进度窗。
- **银河系星图**：星系核 = 核心记忆，恒星系 = 大类，行星系 = 小类，星球 = 一条记忆。`stars` 新增 `cat_major / cat_minor / uid`（旧库自动升级）。主模型（默认对话模型，默认 400,000 tokens 上下文）负责融合相同的星、给星分类、合并重复类别；位置由分类推出（`app/galaxy.py`，可复现），星图画出恒星系 / 行星系名字；新旧决定大小亮度不变。对话监控出新星后自动排队整理。
- **对话抽屉**：主页可拖动、可调大小、可收起、记住位置的浮动面板，用自然语言指挥 AI 管理星愿（搜索、增改删星、改核心——面板是主人通道、提提案、看技能、立即归纳、整理星系、开关监控、看状态）。有上下文（`soul/agent/chat.json`）；没配模型时支持 `/状态 /归纳 /整理 /搜索 /清空`。
- **自动压缩**：有上下文的地方（对话抽屉、主模型整理）累计到 380,000 tokens（`llm.compact_tokens`，可配）自动把较早历史压成摘要 + 保留最近几轮，记日志并显示在任务抽屉。
- **任务抽屉**（默认左侧）：「星愿」页列出对话归纳、整理星系、对话指令、备份、上下文压缩、记忆整理；「客户端任务」页列出捕获的任务。完成绿、进行中 / 未闭环橙、失败红，每 4 秒刷新。`/api/tasks`。
- **数据**（`app/datasync.py`）：数据目录（打开目录 / 日志、日志保留期——启动清理一次之后每 12 小时、清除缓存、重置数据——先自动快照）；存储只做三种：本地文件夹、WebDAV（加密）、云服务器（自己服务器上的星愿，`GET/POST /api/sync/delta`，主人令牌）；坚果云 / S3 显示「即将支持」。WebDAV 每天 03:00（北京时间）**增量**上传（只传水位线之后新增 / 修改 / 隐藏的星），关窗错过了下次启动补一次，每次进任务抽屉。同步来的核心记忆只变成提案。
- **设置重做**（仿 Cherry Studio）：整页视图 + 「← 返回」；左栏分组导航（基础 / 工具 / 偏好，可搜索），「数据」有子导航；右侧圆角卡片，改了就自动保存。新增「对话监控」「外观」「数据」页；默认模型页可设上下文窗口与压缩阈值。
- **外观**：主题 浅色 / 深色 / 系统、主题颜色（绿 #00B96B 默认、红、橙、蓝、紫 + 自定义）、缩放、对话抽屉 / 任务列表位置；`web/theme.js` 所有页面生效（星图画面保持夜空）。
- 新增 `tests/watch_test.py` 140 项；截图 `docs/screenshot-drawers.png`、`docs/screenshot-settings-appearance.png`、`docs/screenshot-settings-data.png`。
- **格式核实情况**：Codex rollout 与 notify、Claude Code Stop 钩子输入、CodeBuddy 钩子、OpenCode 1.x 表结构已对照官方文档 / 源码；Claude Code JSONL 行结构按通行格式；Qoder（按 Claude 同款处理）、WorkBuddy 内容细节、OpenCode 2.x `session_message` 内容与插件加载、dsh 事件名**未核实**（解析全部容错；dsh 标为实验性，`.jsonl.zstd` 需装 zstandard）。
- 已知局限：融合关系（哪颗并进了哪颗）不跨设备同步；MCP 的 `log_turn` 未做（轮询 + 钩子已覆盖）。

## v0.10.0（2026-11-08）上游接口格式：新增 OpenAI Responses，补上 Anthropic 漏洞
- 新增 `app/upstream.py`：内部统一用 Chat Completions 格式，按服务商选的格式转换——OpenAI 兼容（`/chat/completions`，原样转发）、Anthropic（`/v1/messages`，x-api-key）、OpenAI Responses（`/responses`，`store:false`）。
- 转换覆盖：system / instructions、图片（data URL 与网址）、工具定义与 tool_choice、工具调用与工具结果、temperature（Anthropic 自动限制在 0~1）/ top_p / max_tokens / stop、流式（含工具参数逐段拼接、字节被切碎也能拼回）、用量、思考内容 → `reasoning_content`、结束原因；上游错误转成 OpenAI 错误格式。
- 补漏洞：以前选 Anthropic 服务商作默认对话 / 整理模型时，代理和后台整理仍发 `/chat/completions`，会失败；现在对话代理、`/v1/models`、后台整理（整理模型可在另一家、另一种格式）、检测、宇宙之眼帧模式都按所选格式调用。
- 设置 → 模型服务的格式下拉新增「OpenAI Responses」，地址预览随之变化；宇宙之眼设置同样可选。
- 新增 `tests/upstream_test.py` 47 项（本地模拟 Anthropic 与 Responses 上游，不联网）。

## v0.9.0（2026-11-08）单文件版 星愿记忆-windows-amd64.exe
- 新增单文件版：整个软件（运行环境、依赖、内置向量模型、声纹模型）装进一个 `星愿记忆-windows-amd64.exe`（约 132MB），拷走这一个文件就能用。
- 第一次打开会显示「首次启动，正在准备运行环境」进度窗，解压到 `%LOCALAPPDATA%\StarWish\runtime\<版本-哈希>\`；以后再开直接启动，不再解压。只有换了新版本（内容哈希变了）或缓存被删时才重新解压一次，并自动清掉旧版本缓存。
- 先解到 `.tmp` 再改名，中途断电或被关不会留下半成品；缓存被清理软件删坏时下次自动重解。
- 记忆数据仍放在 exe 旁边的 `soul\` 文件夹，U 盘随身带走（可用 `STARWISH_SOUL_DIR` 改位置）。
- MCP 接入改为 `星愿记忆-windows-amd64.exe --mcp`（继承标准输入输出）；设置「接入与代理」里自动显示完整命令，`/api/health` 新增 `mcp` 字段。
- 新启动器 `tools/launcher/onefile.c`，解压库 miniz（MIT）；`tools/build_windows.py` 默认同时生成文件夹版与单文件版（`--no-onefile` 只出文件夹版）。
- 注意：从 BGE-M3 一键部署下载的大模型在单文件版里放在缓存目录，升级版本后需要重新部署。
- `tests/desktop_test.py` 增至 72 项（尾部格式、版本标签、负载完整性用同一 miniz 逐个解压校验、中文路径、--mcp）。

## v0.8.0（2026-11-08）更名「星愿记忆」、软件图标、exe 桌面版、依赖预装、设置窗口与模型服务
- 全部「星魂 / SoulStar」改为「星愿 / StarWish」，软件名「星愿记忆」；环境变量前缀改为 `STARWISH_`，快照文件名改为 `starwish-*.zip`。
- 软件图标：用主人设计的图做成圆角图标（`assets/starwish.ico` 16~256 多尺寸、网页 favicon、PWA manifest、顶栏图标）。
- 桌面版：新增 `desktop.py`（后台线程跑服务 + pywebview / WebView2 桌面窗口，关窗即停服务、进程退出；无 WebView2 时退回 Edge/Chrome 应用窗口，再不行用浏览器 + 心跳自动退出；已在运行时只开窗口；端口被占自动换）与 `星愿记忆.exe` 启动器（`tools/launcher/`，无控制台、带图标）。去掉 `start.bat` / `mcp.bat`，改为 `MCP接入.bat`。
- 依赖预装：`tools/build_windows.py` 打出免安装版，内置 Python 3.12 embeddable + 全部依赖（含桌面窗口与声纹插件）+ 内置向量模型 + 声纹模型，启动不再联网下载。
- 设置改为独立浮动窗口（`web/settings.js`）：点「设置」开 / 关，可拖动、可调大小、Esc 关闭；左侧分区：模型服务 / 默认模型 / 向量模型 / 视频交互模型 / 接入与代理 / 记忆与星辰 / 备份与同步 / 安全 / 关于。
- 模型服务（新增 `app/providers.py`）：13 家预设服务商；填 Key 点「检测」即验证并自动同步模型；地址少 `/v1` 自动补；模型自动分类（对话 / 识图 / 向量 / 其他）；每个模型和每个默认模型都能检测；默认模型下拉选择并自动映射到代理、整理（可在另一家）、宇宙之眼、向量接口；服务商 Key 掩码显示、可随主密码加密；旧设置自动显示为一个服务商。
- 新增 `tests/desktop_test.py`。

## v0.7.0（2026-11-07）向量模型三档可选，语义 + 字面混合检索
- 新增 `app/embed/` 与重写 `app/semantic.py`：① 内置 bge-small-zh-v1.5 int8 ONNX（23.9MB，常驻默认，缺了自动下载）；② BGE-M3 int8 ONNX（约 590MB，设置里一键部署，主站不通自动换 hf-mirror）；③ Qwen3-Embedding 接口（百炼 text-embedding-v4 或任何 OpenAI 兼容 /embeddings，单独填 Key，按机密保存）。
- 去掉 sentence-transformers / PyTorch 依赖，改用 onnxruntime + tokenizers + numpy（Windows 约 31MB，写进 requirements）。
- 混合检索：FTS5 与向量两路取较大值；向量只留够像且接近最佳的星；选的档位没准备好或接口出错时自动退回内置。
- 向量表主键改为 (star_id, model)，多模型并存、切换不丢；旧灵魂包自动升级；后台补齐线程；改星内容自动重算；融合星同样有向量。
- 设置页新增「向量模型」与「视频交互模型（宇宙之眼）」两块；统计里显示当前向量档位。
- 新增 `tests/embed_test.py` 46 项。

## v0.6.0（2026-11-07）星图重做：一颗主核星辰起步，记忆在使用中长出、融合进化
- 星图一开始只有一颗金色的主核星辰（核心记忆三段都在里面，填得越满越亮），去掉了 2500 颗背景星尘和旧的三星 + 金色星尘；空星图有提示。
- 新记忆在使用中从主核里诞生并飞到自己的位置；大小和亮度按时间先后区分（越新越大越亮越靠近主核），星少时聚拢、镜头拉近；每 20 秒自动刷新，没变化不重建。
- 新增 `app/fusion.py`：相似记忆自动找组（jieba 实词重合 + 同标签加分），LLM 归纳或朴素拼接，融合星记代数（可再进化），时间取最新成员；成员不删除、不参与召回，可一键拆开还原；重复内容指向融合星；事件 `star_fuse` / `star_unfuse`。
- 每新增 8 颗星自动融合一次；「备份/整理」里可预览与手动融合；星详情显示融合前的记忆；统计多了「融合星」。
- 数据库新增 `fused_into` / `generation` / `fused_from` 三列，旧灵魂包启动时自动升级；星图接口每颗星多两个字段。
- 新增 `tests/fusion_test.py` 39 项；新增截图 `docs/screenshot-map-start.png`、`docs/screenshot-map-grown.png`。

## v0.5.0（2026-11-07）宇宙之声：分辨片中每个人并认出名字
- 新增 `app/voice/`（audio / backend / registry / naming / pipeline / models）与观看页「🌌 宇宙之声」开关、「片中的人」面板、「声纹库」页。
- 可选插件：默认不装；打开时一键下载 sherpa-onnx + CAM++ 中英声纹 + pyannote 分割 int8（约 60MB，放 `models/voice/`，不进灵魂包与备份），支持下载加速前缀与 pip 镜像，可一键卸载。
- 分人：浏览器 16kHz PCM 每 8 秒一段 → 分割 + 声纹 → 与本场登记簿比对；音高判男女编号；只用 ≥1.5 秒的句子学习；定期全场重新聚类自动合并拆开的人。
- 三路线索认名字：自我介绍 + LLM 读带说话人的台词 + 视觉模型回答末尾的【认人】标记（流式隐藏、不朗读）；两路线索或两票自动确认；主人可改名 / 确认 / 否决 / 合并。
- 声纹库按片名 / 系列保存确认过的人，下一集直接认出；录入主人声音后能分清主人说的话；只存向量不存录音。
- 台词按说话人记进画面日记，最近台词、日记、星星里都用名字；结束时星星记下「片中认识的人」。
- 新增配置段 `voice`；新增 `tests/voice_test.py` 94 项（含真实模型四人录音分人：4 人，实时率 0.05）；宇宙之眼 59 项、冒烟 161 项保持通过。

## v0.4.0（2026-11-07）宇宙之眼：一起看屏幕 / 摄像头
- 新增 `app/eye/`（engine 引擎 / session 会话）与 `/eye` 页面，星图顶栏「👁 宇宙之眼」按键，仅主人可用。
- 浏览器采集（getDisplayMedia / getUserMedia）+ 本机变化检测（切场景 + 动作累积选帧）+ 关键帧环形缓冲 + 分镜拼图（对齐 28 像素）。
- 开口即问（流式 SSE，显示首字耗时）；切场景主动吐槽并按节奏限速；语音识别、朗读、插话打断与回声守卫；框选区域。
- 引擎：通用帧模式（任意 OpenAI / Anthropic 兼容识图模型，可沿用 LLM 维护配置）；千问 Qwen3.8-Omni-Flash 视频接口预留（两种协议地址已预置，填 Key 即用；视频格式不认时自动退回多图；可选直接出语音）。
- 能力预检（纯红测试图）、每日 token / 请求预算、图片不进历史（只留文字日记）、画面默认不落盘、结束时沉淀成星星、可选转写电影声音。
- 新增配置段 `eye`（3 个 Key 加密存储、掩码显示）；新增 `tests/eye_test.py` 59 项；冒烟测试 161 项保持通过。

## v0.3.0（2026-11-07）技能自进化（参考 Hermes Agent）
- 新增 `app/evolve/`：技能库（Hermes/agentskills 兼容 SKILL.md，create/patch/edit/write_file/remove_file/archive/restore/rollback）、热补丁模糊匹配、安全扫描 + 信任矩阵、待批准队列。
- 双引擎：MCP 前台自觉（SKILLS_GUIDANCE + skills_list/skill_view/skill_manage/skill_feedback，调用后计数器归零）；代理后台巡检（跨会话计数阈值 → 守护线程回看对话，联合提取星星与核心提案）。
- 自修复：注入过 / 被差评的技能交巡检优先修补；技能亮度随时间变暗、使用变亮，永不硬删除。
- 代理渐进式注入相关技能（预算封顶）；`/技能 …` 指令与「把刚才的做法存成技能」；REST `/api/skills*`、`/api/evolve/*`；面板 `/skills` 页面。
- 冒烟测试 112 → 161 项。

## v0.2.0（2026-11-07）核心记忆独立模块
- 核心记忆拆成独立模块 `app/core/`（store / edit / history / proposals / commands），所有修改走唯一入口，带版本号 `rev`。
- 面板新增核心记忆模块页面 `/core`：三段分别编辑、锁定开关、待批准提案、版本历史（逐行 diff、回到此版本、撤销这次）、主人指令。
- 主人可从任意客户端直接修改：代理里的 `/核心 …` 与「记住，以后叫我X」、MCP `update_core` / `undo_core` / `core_history`、REST `/api/core/update`、`/api/core/command`、`/api/core/rollback`、`/api/core/undo`。
- 主人令牌（`core.owner_token`，加密存储、掩码显示）；LLM 后台整理、导入、AI 推测仍只能产生提案；上锁时主人远程指令自动转为提案。
- 每次修改记 `core_change` 事件（前/后/来源/主人原话/完整快照）；兼容读取 v1 的 `core_edit` 旧日志。
- 冒烟测试 73 → 112 项。

## v0.1.0 首版
