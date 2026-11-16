"""Read-only handoff metadata for the authorized project."""
import re

from . import graphflow_tasks, graphflow_tests


def _test_label(profile, state, code):
    if (isinstance(profile, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", profile)
            and state in {"passed", "failed", "timed_out", "interrupted", "launch_failed"}):
        return f"{profile} [{state}]（退出码：{code if type(code) is int else '无'}）"
    return ""


def render(workspace) -> str:
    lines = ["## 星愿项目交接（历史记录，不是本轮验证）"]
    if graphflow_tasks.enabled():
        try:
            with graphflow_tasks.read_ledger(workspace) as db:
                rows = db.execute("""SELECT t.id,t.state,e.summary FROM tasks t
                    LEFT JOIN task_events e ON e.task_id=t.id AND e.revision=t.revision
                    ORDER BY t.updated DESC,t.id LIMIT 5""").fetchall() if db is not None else []
                linked = graphflow_tasks.latest_verifications(db, [row[0] for row in rows])
        except graphflow_tasks.GraphFlowError:
            rows, linked = [], {}
            lines.append("- 项目任务记录暂不可读。")
        for task_id, state, summary in rows:
            if not graphflow_tasks.valid_task_id(task_id) or state not in {"pending", "blocked", "completed"}:
                continue
            note = " ".join((summary or "").split())[:140]
            verification = linked.get(task_id)
            test = _test_label(verification["profile"], verification["state"], verification["exit_code"]) if verification else ""
            lines.append(f"- 任务 {task_id} [{state}]" + (f"：{note}" if note else "")
                         + (f" · 关联测试 {test}" if test else ""))
    if graphflow_tests.enabled():
        try:
            last = graphflow_tests.latest_finished(workspace)
        except graphflow_tests.GraphFlowError:
            lines.append("- 测试记录暂不可读。")
        else:
            if last:
                test = _test_label(*last)
                if test:
                    lines.append(f"- 项目最近测试 {test}")
    if len(lines) == 1:
        lines.append("- 暂无可交接的任务回填或测试状态。")
    return "\n".join(lines)
