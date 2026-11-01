@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
 echo Run setup_windows.cmd first. Read README.md for setup and limitations.
 pause
 exit /b 1
)
.venv\Scripts\python.exe app.py
if errorlevel 1 pause
