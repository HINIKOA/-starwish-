"""星愿 · 对话监控（v0.11.0）：直接读各 AI 客户端写在本机的对话记录，每轮结束后让整理模型判断有没有值得记的。

  turns.py     一轮对话（Turn）与截断 / 琐碎判断
  adapters.py  各客户端的文件位置与 JSONL 解析（Claude Code / Qoder / WorkBuddy / CodeBuddy / Codex / dsh）
  opencode.py  OpenCode 的 SQLite（只读）
  state.py     偏移量（soul/watch/offsets.json）
  store.py     捕获的任务（watch_tasks / watch_fragments）
  extract.py   抽取与落库（星星 / 核心提案 / 任务碎片与闭环合并）
  watcher.py   轮询线程 + 单工作队列
  hooks.py     各客户端 Stop 钩子的安装 / 卸载
  notify.py    `--notify <client>` 命令行（钩子调用，必须轻、快、永不阻塞）

注意：本文件保持为空导入，`--notify` 只加载 notify.py，不拖进服务端依赖。
"""
