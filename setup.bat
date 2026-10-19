@echo off
rem One-time setup on Windows
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if errorlevel 1 ( echo Python 3.9+ not found - install it from python.org & pause & exit /b 1 )
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo Setup finished. Start the app with run.bat
pause
