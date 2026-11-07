"""技能相关的对话指令（只看最后一条用户消息）。

/技能                    列出技能（按亮度）
/技能 查看 <名字>         看全文
/技能 历史 <名字>         最近修改
/技能 撤销 <名字>         撤销这个技能最近一次修改
/技能 归档 <名字> · /技能 恢复 <名字>
/技能 好评 <名字> · /技能 差评 <名字> <原因>   差评的技能下次巡检优先修
/技能 巡检 [要求]         立即后台巡检
自然语言：「把刚才的做法存成技能」「记住这个流程」→ 立即后台巡检
"""
from __future__ import annotations

import re

from . import review, skills

SLASH = re.compile(r"^\s*[/／](技能|skills?)(?:\s+|$)(.*)$", re.S | re.I)
NATURAL = re.compile(r"(把|将)?(刚才|这个|这套|上面|刚刚)的?(做法|方法|流程|步骤|经验|套路|办法)(存|保存|记|总结|沉淀)(成|为|下来)?(技能|skill)?"
                     r"|(存|保存|总结|沉淀)(成|为)(一个)?(技能|skill)"
                     r"|记住(这个|这套|刚才的?)(做法|方法|流程|步骤|套路|办法)", re.I)

HELP = ("技能指令：\n/技能 —— 列出\n/技能 查看 名字\n/技能 历史 名字 · /技能 撤销 名字\n/技能 归档 名字 · /技能 恢复 名字\n"
        "/技能 好评 名字 · /技能 差评 名字 原因\n/技能 巡检 [要求] —— 立即让后台回看最近对话并进化技能\n"
        "也可以直接说：把刚才的做法存成技能")


def parse(text: str) -> dict | None:
    text = (text or "").strip()
    m = SLASH.match(text)
    if m:
        rest = m.group(2).strip()
        if not rest or rest in ("列表", "list"):
            return {"type": "slash", "op": "list"}
        parts = rest.split(None, 2)
        verb = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        more = parts[2] if len(parts) > 2 else ""
        table = {"查看": "view", "view": "view", "历史": "history", "撤销": "undo", "归档": "archive", "恢复": "restore",
                 "好评": "good", "差评": "bad", "巡检": "review", "review": "review", "帮助": "help", "help": "help"}
        op = table.get(verb)
        if op == "review":
            return {"type": "slash", "op": "review", "focus": (arg + " " + more).strip()}
        if op in ("help", None):
            return {"type": "slash", "op": "help", "error": None if op else f"没看懂：{rest}"}
        if not arg:
            return {"type": "slash", "op": "help", "error": f"「{verb}」需要技能名字"}
        return {"type": "slash", "op": op, "name": arg, "comment": more}
    if len(text) <= 120 and NATURAL.search(text):
        return {"type": "natural", "op": "review", "focus": text}
    return None


def execute(cmd: dict, authorized: bool, source: str = "command") -> str:
    op = cmd["op"]
    if op == "help":
        return ((cmd.get("error") + "\n\n") if cmd.get("error") else "") + HELP
    if op == "list":
        items = skills.list_skills()
        if not items:
            return "还没有技能。AI 做完复杂任务后会自己沉淀，或者说「/技能 巡检」。"
        return "星魂技能（亮→暗）：\n" + "\n".join(
            f"{'★' if s['created_by'] == 'agent' else '·'} {s['name']}（亮度 {s['brightness']:.2f}）：{s['description']}" for s in items[:30])
    if op == "review":
        ok = review.spawn_review(reason="主人要求巡检", focus=cmd.get("focus", ""))
        return "🔭 已在后台启动技能巡检，会回看最近的对话，结果可在面板「技能进化」查看。" if ok else "巡检正在进行中。"
    name = cmd["name"]
    try:
        if op == "view":
            return skills.view(name)["content"]
        if op == "history":
            h = skills.history(name, 5)
            return "\n".join(f"r{e['rev']} · {e['ts'][:16].replace('T', ' ')} · {e['action']} · {e['source_title']}"
                             + (f" · {e['note']}" if e.get("note") else "") for e in h) or "没有修改记录"
        if not authorized:
            return "只有主人可以这样操作技能。"
        if op == "undo":
            h = skills.history(name, 1)
            if not h:
                return "没有可撤销的修改"
            return skills.rollback(name, h[0]["rev"], "undo", source=source)["message"]
        if op in ("archive", "restore"):
            return skills.manage(op, name, source=source, trust="owner")["message"]
        if op in ("good", "bad"):
            return skills.feedback(name, op == "good", cmd.get("comment", ""), source=source)["message"]
    except (KeyError, skills.SkillError) as e:
        return str(e).strip("'")
    return HELP
