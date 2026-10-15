@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto missing_env
".venv\Scripts\python.exe" -m src.diagnostics
if errorlevel 1 goto failed
pause
exit /b 0

:missing_env
echo Python environment is missing. Run setup.cmd first.
pause
exit /b 1

:failed
echo Diagnostics failed. Run setup.cmd to repair the environment.
pause
exit /b 1
