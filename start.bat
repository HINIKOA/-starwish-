@echo off
chcp 65001 >nul
REM 星魂 SoulStar 启动脚本（Windows）。首次运行会在本文件夹内创建 .venv 并安装依赖。
REM 换电脑后若 .venv 失效（它绑定了原电脑的 Python 路径），会自动重建。
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import fastapi, uvicorn, jieba" >nul 2>nul || (echo 检测到 .venv 不可用，重新创建... & rmdir /s /q .venv)
)
if not exist ".venv\Scripts\python.exe" (
  echo 首次运行：正在创建虚拟环境 .venv ...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo 未找到 Python 3.10+，请先安装 https://www.python.org/downloads/ 并勾选 Add to PATH
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" run.py %*
pause
