@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto missing_env
".venv\Scripts\python.exe" -c "import tkinter, PIL, av, opencc, imageio_ffmpeg, yt_dlp, webview" >nul 2>&1
if errorlevel 1 goto missing_packages
".venv\Scripts\python.exe" -m src.main
if errorlevel 1 goto failed
exit /b 0

:missing_env
echo Python environment is missing.
echo Run setup.cmd first, then open this launcher again.
pause
exit /b 1

:missing_packages
echo Python dependencies are missing or damaged.
echo Run setup.cmd to install or repair them.
pause
exit /b 1

:failed
echo Application startup failed. See logs\app.log for details.
echo You can also run diagnostics.cmd.
pause
exit /b 1
