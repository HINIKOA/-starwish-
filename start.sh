#!/usr/bin/env bash
# 星愿记忆 启动脚本（macOS / Linux）。首次运行会在本文件夹内创建 .venv 并安装依赖。
# 换电脑后若 .venv 失效（它绑定了原电脑的 Python 路径），会自动重建。
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ -x ".venv/bin/python" ] && ! .venv/bin/python -c "import fastapi, uvicorn, jieba" >/dev/null 2>&1; then
  echo "检测到 .venv 不可用，重新创建..."; rm -rf .venv
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "首次运行：创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
