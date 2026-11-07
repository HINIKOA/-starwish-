# 更新记录

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
