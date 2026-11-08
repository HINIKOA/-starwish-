"""星愿 StarWish —— MCP 服务器（stdio）。

优先使用官方 `mcp` Python SDK（兼容 1.x 的 FastMCP 与 2.x 的 MCPServer）；
未安装或设置环境变量 STARWISH_MCP_FALLBACK=1 时，退化为内置的极简 JSON-RPC stdio 实现。
直接读写 ./soul/ 灵魂包，不需要先启动网页服务（两者可同时运行，SQLite WAL 模式）。

用法：python mcp_server.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import __version__, config, core, evolve, stars  # noqa: E402
from app.db import init_db  # noqa: E402

INSTRUCTIONS = ("星愿记忆。对话开始时先调用 get_core 获取恒定的核心记忆；需要回忆时先 recall_index 看概况，"
                "再 recall(query) 在 token 预算内取相关摘要，必要时 recall_detail(id) 取全文。"
                "值得长期记住的新信息用 remember 保存。核心记忆（AI身份/主人称呼/主人喜好）不能自作主张修改："
                "只有主人在对话里明确要求时，才用 update_core 并在 owner_instruction 里原样附上主人的原话；"
                "你自己推测出来的变化用 propose_core_change 提案，由主人批准。主人说撤销就用 undo_core。\n"
                "技能（skills）是你的操作手册：开始做事前先 skills_list 看有没有相关技能，有就 skill_view 读全文照做。\n"
                + evolve.review.SKILLS_GUIDANCE)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------- 工具实现（两种模式共用） ----------------

def get_core() -> str:
    """获取恒定的核心记忆（AI身份与名字 / 主人是谁及称呼 / 主人的喜好）。每次对话开始时调用一次。"""
    return core.core_text() or "（核心记忆为空，请主人在星愿面板中填写）"


def recall_index() -> str:
    """记忆库概况：星星数量、日期范围、热门标签。只花几十个 token，先看它再决定查什么。"""
    return stars.recall_index()["text"]


def recall(query: str, budget_tokens: int = 600) -> str:
    """按关键词/问题召回相关记忆摘要，结果严格控制在 budget_tokens 以内（默认 600）。返回 #编号 日期 摘要。"""
    r = stars.recall(query, int(budget_tokens or 600))
    return r["text"] or "（没有找到相关记忆）"


def recall_detail(star_id: int) -> str:
    """查看某颗星的全文、标签、来源以及原始对话片段（如有）。"""
    d = stars.recall_detail(int(star_id))
    if not d:
        return "（不存在这颗星）"
    keep = {k: d[k] for k in ("id", "content", "summary", "tags", "created_at", "importance", "source", "raw") if k in d}
    return _j(keep)


def remember(content: str, summary: str = "", tags: list[str] | None = None, importance: int = 3) -> str:
    """把一条值得长期记住的信息存为一颗星。importance 1~5。近似重复会自动合并。"""
    r = stars.add_star(content, summary, tags or [], importance, source="mcp", merge_near=True, actor="mcp")
    state = "合并到已有" if r.get("merged") else ("已存在" if r.get("duplicate") else "已保存")
    return f"{state} #{r['id']}：{r['summary']}"


def propose_core_change(section: str, content: str, reason: str = "", mode: str = "append") -> str:
    """提议修改核心记忆（section: identity / owner / preferences；mode: append 或 replace）。只会进入提案队列，需主人在面板批准。"""
    p = core.create_proposal(section, content, reason, source="mcp", mode=mode)
    return f"已提交提案 #{p['id']}（{p['section_title']}），等待主人批准。"


def _owner_ok(owner_instruction: str) -> bool:
    return bool((owner_instruction or "").strip()) and bool((config.load_config().get("core") or {}).get("mcp_owner_edit", True))


def update_core(section: str, content: str, owner_instruction: str, mode: str = "replace") -> str:
    """按主人的明确指令修改核心记忆（section: identity / owner / preferences；mode: replace 替换整段 / append 追加一行）。
    owner_instruction 必须原样填写主人说的话（例如「记住，以后叫我天天」）；仅在主人明确要求时调用。
    上锁或未授权时会自动转为提案。每次修改都记入永久日志，可撤销。"""
    m = mode if mode in ("replace", "append") else "replace"
    res = core.owner_update({core.norm_section(section): (content, m)}, source="mcp",
                            authorized=_owner_ok(owner_instruction), instruction=owner_instruction or "")
    return core.commands._describe(res)


def core_history(limit: int = 5) -> str:
    """查看核心记忆最近的修改记录（版本号、时间、来源、改了哪段）。"""
    return core.commands._fmt_history(int(limit or 5))


def undo_core(owner_instruction: str) -> str:
    """撤销最近一次核心记忆修改。仅在主人明确要求撤销时调用，owner_instruction 填主人原话。"""
    if not _owner_ok(owner_instruction):
        return "只有主人明确要求时才能撤销（请在 owner_instruction 填写主人原话）。"
    out = core.commands.execute({"op": "undo"}, source="mcp", authorized=True)
    return out["reply"]


def skills_list(query: str = "") -> str:
    """列出技能（名字 + 一句话说明）。给 query 时只列相关的。做事前先看看有没有现成的技能。"""
    if query.strip():
        items = [s for _, s in evolve.skills.relevant(query, k=10, min_score=0.01)]
    else:
        items = evolve.skills.list_skills()
    if not items:
        return "（还没有相关技能）"
    return "\n".join(f"- {s['name']}：{s['description']}" for s in items[:60])


def skill_view(name: str, file_path: str = "") -> str:
    """读取技能全文（SKILL.md）；file_path 可读辅助文件（如 references/xx.md）。"""
    try:
        if file_path:
            return evolve.skills.read_file(name, file_path)
        v = evolve.skills.view(name)
        return v["content"] + (("\n\n辅助文件：" + ", ".join(v["files"])) if v["files"] else "")
    except KeyError as e:
        return str(e).strip("'")


def skill_manage(action: str, name: str, content: str = "", category: str = "", old_string: str = "",
                 new_string: str | None = None, replace_all: bool = False, file_path: str = "", file_content: str | None = None) -> str:
    """创建 / 修补技能（你的程序性记忆）。action: create（content=完整 SKILL.md，含 YAML 头 name+description）/
    patch（old_string→new_string 精准热补丁，支持模糊匹配，首选）/ edit（content 整篇重写，慎用）/
    write_file（file_path 必须在 references/ templates/ scripts/ assets/ 下）/ remove_file / archive（归档，不硬删）。
    技能写「教训」不写「日志」：祈使句规则 + 原因。"""
    kw = {k: v for k, v in {"content": content, "category": category, "old_string": old_string, "file_path": file_path}.items() if v}
    if new_string is not None:
        kw["new_string"] = new_string
    if file_content is not None:
        kw["file_content"] = file_content
    r = evolve.skills.manage(action, name, replace_all=replace_all, source="mcp", trust="agent-created", **kw)
    if r.get("success"):
        evolve.review.note_skill_manage()
    return r.get("message", "") or _j(r)


def skill_feedback(name: str, good: bool, comment: str = "") -> str:
    """技能好用 / 不好用的反馈。差评的技能会在下一次后台巡检被优先修复。"""
    try:
        return evolve.skills.feedback(name, bool(good), comment, source="mcp")["message"]
    except KeyError as e:
        return str(e).strip("'")


TOOLS = [get_core, recall_index, recall, recall_detail, remember, propose_core_change, update_core, core_history, undo_core,
         skills_list, skill_view, skill_manage, skill_feedback]

SCHEMAS = {
    "get_core": {"type": "object", "properties": {}},
    "recall_index": {"type": "object", "properties": {}},
    "recall": {"type": "object", "properties": {
        "query": {"type": "string", "description": "要回忆的内容/关键词"},
        "budget_tokens": {"type": "integer", "default": 600, "description": "返回内容的 token 上限"}},
        "required": ["query"]},
    "recall_detail": {"type": "object", "properties": {"star_id": {"type": "integer"}}, "required": ["star_id"]},
    "remember": {"type": "object", "properties": {
        "content": {"type": "string"}, "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3}},
        "required": ["content"]},
    "propose_core_change": {"type": "object", "properties": {
        "section": {"type": "string", "enum": ["identity", "owner", "preferences"]},
        "content": {"type": "string"}, "reason": {"type": "string"},
        "mode": {"type": "string", "enum": ["append", "replace"], "default": "append"}},
        "required": ["section", "content"]},
    "update_core": {"type": "object", "properties": {
        "section": {"type": "string", "enum": ["identity", "owner", "preferences"]},
        "content": {"type": "string"},
        "owner_instruction": {"type": "string", "description": "主人的原话，必须原样填写"},
        "mode": {"type": "string", "enum": ["replace", "append"], "default": "replace"}},
        "required": ["section", "content", "owner_instruction"]},
    "core_history": {"type": "object", "properties": {"limit": {"type": "integer", "default": 5}}},
    "undo_core": {"type": "object", "properties": {"owner_instruction": {"type": "string"}}, "required": ["owner_instruction"]},
    "skills_list": {"type": "object", "properties": {"query": {"type": "string"}}},
    "skill_view": {"type": "object", "properties": {"name": {"type": "string"}, "file_path": {"type": "string"}}, "required": ["name"]},
    "skill_manage": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["create", "patch", "edit", "write_file", "remove_file", "archive"]},
        "name": {"type": "string", "description": "小写字母/数字/-_.，最长 64"},
        "content": {"type": "string", "description": "create/edit：完整 SKILL.md（YAML 头 + 正文）"},
        "category": {"type": "string"},
        "old_string": {"type": "string", "description": "patch：要替换的原文（允许空白/缩进/标点有出入）"},
        "new_string": {"type": "string", "description": "patch：替换后的内容；空字符串表示删除"},
        "replace_all": {"type": "boolean", "default": False},
        "file_path": {"type": "string"}, "file_content": {"type": "string"}},
        "required": ["action", "name"]},
    "skill_feedback": {"type": "object", "properties": {"name": {"type": "string"}, "good": {"type": "boolean"},
                                                          "comment": {"type": "string"}}, "required": ["name", "good"]},
}


# ---------------- 极简 JSON-RPC stdio 实现（后备） ----------------

def run_fallback() -> None:
    funcs = {f.__name__: f for f in TOOLS}
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # 通知，无需回复
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "starwish", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": (f.__doc__ or "").strip(), "inputSchema": SCHEMAS[n]}
                                    for n, f in funcs.items()]}
            elif method == "tools/call":
                name = params.get("name")
                if name not in funcs:
                    raise ValueError(f"未知工具：{name}")
                try:
                    text = funcs[name](**(params.get("arguments") or {}))
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                except Exception as e:
                    result = {"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})


def run_sdk() -> bool:
    server = None
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x

        server = MCPServer("starwish", instructions=INSTRUCTIONS, version=__version__)
    except Exception:
        try:
            from mcp.server.fastmcp import FastMCP  # mcp 1.x

            server = FastMCP("starwish", instructions=INSTRUCTIONS)
        except Exception:
            return False
    for f in TOOLS:
        server.tool()(f)
    server.run("stdio")
    return True


def main():
    init_db()
    core.load_core()
    if os.environ.get("STARWISH_MCP_FALLBACK") == "1" or not run_sdk():
        run_fallback()


if __name__ == "__main__":
    main()
