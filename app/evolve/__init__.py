"""星魂 · 自进化模块（参考 Hermes Agent 的 Skill 自学习与自修复架构）。

  skills.py    技能库（SKILL.md，兼容 Hermes / agentskills.io）：create / patch / edit / archive / restore / 文件 / 历史 / 回滚
  fuzzy.py     热补丁模糊匹配（精确 → 去缩进 → 压缩空白 → 标点归一 → 首尾锚点）
  guard.py     安全扫描 + 信任等级矩阵（AI 自建技能危险时交主人批准）
  review.py    双引擎：前台自觉（SKILLS_GUIDANCE + skill_manage）/ 后台巡检（计数器阈值 → 守护线程回看对话）+ 渐进式注入
  commands.py  /技能 指令与「把刚才的做法存成技能」
"""
from . import commands, guard, review, skills  # noqa: F401
