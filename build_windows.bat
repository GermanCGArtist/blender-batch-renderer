@echo off
rem Builds the Windows app: a folder "Blender Batch Renderer" with "Blender Batch Renderer.exe".
rem Needed once (and when an update says it needs a newer app); normal updates come through
rem Help - Check for updates.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat
.venv\Scripts\python -m pip install --quiet "pyinstaller>=6.3" || ( echo Installing PyInstaller failed & pause & exit /b 1 )
.venv\Scripts\python tools\build_win.py || ( pause & exit /b 1 )
echo.
echo Done. Start "Blender Batch Renderer\Blender Batch Renderer.exe" (you can pin it to the taskbar).
pause
