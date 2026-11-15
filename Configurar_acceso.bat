@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Ejecuta primero Instalar_windows.bat.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m ogame_bot.credentials configure
pause
