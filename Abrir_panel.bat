@echo off
setlocal
cd /d "%~dp0"
set "taskPython=.venv\Scripts\python.exe"
if not exist "%taskPython%" (
    echo Ejecuta primero Instalar_windows.bat.
    pause
    exit /b 1
)
"%taskPython%" -m ogame_bot.panel --open
if errorlevel 1 pause
