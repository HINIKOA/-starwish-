# 更新记录

## v0.2.0（2026-11-07）核心记忆独立模块
- 核心记忆拆成独立模块 `app/core/`（store / edit / history / proposals / commands），所有修改走唯一入口，带版本号 `rev`。
- 面板新增核心记忆模块页面 `/core`：三段分别编辑、锁定开关、待批准提案、版本历史（逐行 diff、回到此版本、撤销这次）、主人指令。
- 主人可从任意客户端直接修改：代理里的 `/核心 …` 与「记住，以后叫我X」、MCP `update_core` / `undo_core` / `core_history`、REST `/api/core/update`、`/api/core/command`、`/api/core/rollback`、`/api/core/undo`。
- 主人令牌（`core.owner_token`，加密存储、掩码显示）；LLM 后台整理、导入、AI 推测仍只能产生提案；上锁时主人远程指令自动转为提案。
- 每次修改记 `core_change` 事件（前/后/来源/主人原话/完整快照）；兼容读取 v1 的 `core_edit` 旧日志。
- 冒烟测试 73 → 112 项。

## v0.1.0 首版
