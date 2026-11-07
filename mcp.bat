@echo off
chcp 65001 >nul
REM 以 MCP stdio 方式运行（供 Claude Desktop 等客户端调用）
cd /d "%~dp0"
".venv\Scripts\python.exe" mcp_server.py
