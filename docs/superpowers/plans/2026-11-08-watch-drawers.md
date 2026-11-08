# v0.11.0 实施计划（2026-11-08）

1. 夹具：`tests/fixtures/watch/` 各客户端样例（Claude、Qoder、WorkBuddy、Codex、dsh 原始 jsonl；OpenCode 用测试里现建的 SQLite）。
2. 先写 `tests/watch_test.py`：解析 → Turn、偏移量续读、未完成轮次不提前推进、idle/notify 触发完成、抽取落库（模拟整理模型）、核心只出提案、任务碎片 → 闭环合并 → 技能 / 任务星、失败、钩子安装幂等与卸载、Codex TOML、OpenCode 插件、notify CLI（服务在 / 不在）、inbox 补读、/api/watch/*、/api/tasks、/api/agent/chat（模拟模型工具调用 + 无模型兜底）、外观配置、前端文件包含抽屉与设置页。
3. 实现 `app/watch/*`、`app/jobs.py`、`app/agent.py`，接到 `server.py`（生命周期启动 / 停止监控线程，写 run_port）。
4. `desktop.py` / `run.py` 处理 `--notify`、记录 run_port；两个启动器 C 源码对 `--notify` 继承标准输入输出、不显示进度窗。
5. 前端：`theme.js`（全部页面）、`drawers.js`（对话抽屉 + 任务抽屉）、settings.js 新增「对话监控」「外观」两页、style.css 主题变量与浅色覆盖。
6. 跑全部测试（smoke、desktop、eye、fusion、voice、embed、upstream、watch），版本号 0.11.0，更新 CHANGELOG / README / 项目说明 / 使用说明书，提交。

## 追加（同日）
7. 星系：`stars.cat_major/cat_minor/uid` 迁移 → `galaxy.layout` → `organize.organize/schedule` → 星图按布局摆放并画名字。
8. 上下文：`context.window/budget/trim_history/compact_if_needed`，对话抽屉与主模型整理都接上；阈值 380,000 可配。
9. 数据：`datasync`（增量导出 / 合并、WebDAV 增量、云服务器推拉、本地备份、03:00 定时 + 启动补跑、日志清理、缓存、重置）+ `/api/sync/delta`、`/api/data/*`。
10. 设置整页重做（Cherry Studio 风格），截图 `docs/screenshot-settings-appearance.png`、`docs/screenshot-settings-data.png`、`docs/screenshot-drawers.png`。
11. 测试：watch_test 覆盖分类、布局分组、上下文预算、压缩触发、增量选择、定时 / 补跑、云服务器（本地模拟远端）。
