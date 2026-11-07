#!/usr/bin/env bash
# 以 MCP stdio 方式运行（供 Claude Desktop / Cursor 等客户端调用）
cd "$(dirname "$0")"
exec .venv/bin/python mcp_server.py
