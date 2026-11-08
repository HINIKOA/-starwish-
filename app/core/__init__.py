"""星愿 · 核心记忆模块（独立模块）。

  store.py      三段内容的存储（core.json 唯一真源 / core.md 镜像）与注入文本
  edit.py       所有修改的唯一入口：版本号、锁、主人指令、回滚 / 撤销
  history.py    版本历史与逐行 diff（读自只追加的 events.jsonl）
  proposals.py  提案队列（LLM 后台整理 / 导入 / AI 自发修改只能走这里）
  commands.py   主人指令解析（/核心 … 与「记住，以后叫我X」）

原则：核心记忆不会被自动改写；主人从面板、带主人令牌的 API、代理对话、MCP 发出的指令直接生效；
每次修改都记录 前/后/来源，可追溯、可回滚。
"""
from .store import (SECTIONS, ConfirmRequiredError, CoreLockedError, core_text, core_tokens, load_core,  # noqa: F401
                    norm_section, render_md, sections_of)
from .edit import SOURCE_TITLES, apply_change, owner_update, rollback, set_line, set_lock, set_section, undo_last  # noqa: F401
from .proposals import create_proposal, decide_proposal, get_proposal, list_proposals  # noqa: F401
from .history import diff_lines, list_history  # noqa: F401
from . import commands, history  # noqa: F401
