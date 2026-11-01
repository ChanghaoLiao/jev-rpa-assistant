@echo off
setlocal
cd /d "%~dp0"
set "PY="
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if defined PY goto found
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto found
py -3.10 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.10"
if not defined PY (
 echo Python 3.11 recommended. Install Python 3.10-3.12 with Python Launcher first.
 echo Read README.md. This installer will not silently install Python or click any client.
 pause
 exit /b 1
)
:found
if exist .venv\Scripts\python.exe goto deps
%PY% -m venv .venv
if errorlevel 1 goto failed
:deps
.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe app.py --doctor
if errorlevel 1 goto failed
echo Setup complete. Run start.cmd. Configure API keys and calibrate Clicknium locators.
pause
exit /b 0
:failed
echo Setup failed. Read the error above. No client messages were sent.
pause
exit /b 1
