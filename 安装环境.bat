@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.13 -m venv .venv
    if errorlevel 1 (
        echo 请安装 Windows 64 位 Python 3.13，并启用 Python Launcher。
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
if errorlevel 1 (
    echo 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip check
".venv\Scripts\python.exe" -m src.diagnostics
pause
