# 许可与署名说明（LICENSE-NOTICE）

## 来源

星魂 SoulStar 的设计灵感、档案格式（`记忆卡.md` 八章结构、`日记/YYYY-MM-DD.md`、`原始对话/YYYY-MM-DD.txt`）
以及「一条记忆一颗星、越新越亮、老星只变暗不消失」的星图理念，来自：

> **星汐 · dsh-xingxi**
> 原仓库：<https://github.com/Yuuyuko-uu/dsh-xingxi>
> 原作者：**Yuuyuko-uu**

星魂是用 Python 独立重写的程序，**没有复制**星汐的 JavaScript 源代码；开发时阅读了星汐的 `README.md`、`index.js`
以理解其档案格式与星图设计，从而实现兼容导入。

## 相对星汐的改动

- 独立运行，不依赖 DSH；Python + FastAPI + SQLite（FTS5 + jieba）
- 新增：恒定核心记忆（三段）+ 锁定 + 提案审批；星星亮度公式（时间衰减 × 召回 × 重要度，带下限）；软删除与只追加事件日志
- 新增：按 token 预算的分层召回（索引 / 摘要 / 全文）
- 新增：MCP 服务器、OpenAI 兼容代理（自动注入记忆、流式透传）、REST API
- 新增：LLM 自动整理（对话 → 星星 + 核心提案）、本地快照、WebDAV 加密备份
- 新增：three.js 3D 星图面板（编辑、搜索变暗、时间线、统计、设置、导入）
- 导入器：读取星汐档案文件夹 / zip，映射为星星与核心提案

## 星汐原许可

星汐使用《Yuuyuko-uu 插件许可 1.1》，全文**原样**保存在本目录的 `LICENSE-XINGXI.txt`，未做任何删改。按该许可：

- 再分发涉及星汐的代码或其实质性部分时，须完整保留该许可文件、在 README 第一屏写明派生来源与原作者、并说明改动（本项目 README 第一屏与本文件已做到）；
- 不得把星汐的成果说成自己原创，不得删除其作者署名；
- 公开的衍生作品须继续使用该许可或更严格的许可。

**发布前请注意**：本项目自身代码为独立实现，但为尊重原作者，如公开发布本项目，建议同样遵循上述许可条款，
或先与原作者 Yuuyuko-uu 确认（其许可第 11 条：「上面没写的情况想用，直接来问我」）。

## 第三方组件

- three.js 0.160.0（`web/vendor/three.module.min.js`）—— MIT License，© three.js authors
- jieba —— MIT License
- FastAPI、uvicorn、httpx、requests、cryptography、mcp 等依赖按各自许可使用
