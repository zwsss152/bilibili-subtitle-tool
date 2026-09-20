@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" bilibili_subtitle_tool.py
) else (
    start "" pythonw bilibili_subtitle_tool.py
)
