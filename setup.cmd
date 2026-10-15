@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto install
if exist ".python\python.exe" goto project_python
py -3.13 -m venv .venv
if errorlevel 1 goto missing_python
goto install

:project_python
".python\python.exe" -m venv .venv
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m src.diagnostics
if errorlevel 1 goto failed
echo Setup completed. You can now run launch.cmd.
pause
exit /b 0

:missing_python
echo Python 3.13 is not installed.
echo Install the Windows 64-bit version with pip, Tk, and Python Launcher:
echo https://www.python.org/downloads/windows/
pause
exit /b 1

:failed
echo Setup failed. The error is shown above. Check your network and retry.
pause
exit /b 1
