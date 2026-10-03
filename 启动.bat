@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 未找到 Python 环境，请先双击“安装环境.bat”。
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m src.main
