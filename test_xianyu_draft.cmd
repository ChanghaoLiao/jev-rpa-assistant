@echo off
cd /d "%~dp0"
if not exist ".build-venv\Scripts\python.exe" goto missing
chcp 65001 >nul
".build-venv\Scripts\python.exe" "xianyu_draft_test.py"
echo.
pause
goto :eof
:missing
echo Test Python runtime is missing. No Xianyu action was taken.
pause
