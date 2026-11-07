"""星魂 SoulStar —— MCP 服务器（stdio）。

优先使用官方 `mcp` Python SDK（兼容 1.x 的 FastMCP 与 2.x 的 MCPServer）；
未安装或设置环境变量 SOULSTAR_MCP_FALLBACK=1 时，退化为内置的极简 JSON-RPC stdio 实现。
直接读写 ./soul/ 灵魂包，不需要先启动网页服务（两者可同时运行，SQLite WAL 模式）。

用法：python mcp_server.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import __version__, core, stars  # noqa: E402
from app.db import init_db  # noqa: E402

INSTRUCTIONS = ("星魂记忆库。对话开始时先调用 get_core 获取恒定的核心记忆；需要回忆时先 recall_index 看概况，"
                "再 recall(query) 在 token 预算内取相关摘要，必要时 recall_detail(id) 取全文。"
                "值得长期记住的新信息用 remember 保存。核心记忆只能用 propose_core_change 提案，由主人批准。")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------- 工具实现（两种模式共用） ----------------

def get_core() -> str:
    """获取恒定的核心记忆（AI身份与名字 / 主人是谁及称呼 / 主人的喜好）。每次对话开始时调用一次。"""
    return core.core_text() or "（核心记忆为空，请主人在星魂面板中填写）"


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


TOOLS = [get_core, recall_index, recall, recall_detail, remember, propose_core_change]

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
                          "serverInfo": {"name": "soulstar", "version": __version__},
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

        server = MCPServer("soulstar", instructions=INSTRUCTIONS, version=__version__)
    except Exception:
        try:
            from mcp.server.fastmcp import FastMCP  # mcp 1.x

            server = FastMCP("soulstar", instructions=INSTRUCTIONS)
        except Exception:
            return False
    for f in TOOLS:
        server.tool()(f)
    server.run("stdio")
    return True


def main():
    init_db()
    core.load_core()
    if os.environ.get("SOULSTAR_MCP_FALLBACK") == "1" or not run_sdk():
        run_fallback()


if __name__ == "__main__":
    main()
