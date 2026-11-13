@echo off
setlocal
cd /d "%~dp0"
set "taskPython=.venv\Scripts\python.exe"
if not exist "%taskPython%" set "taskPython=..\.venv\Scripts\python.exe"
if not exist "%taskPython%" (
    echo Ejecuta primero Instalar_windows.bat.
    pause
    exit /b 1
)
"%taskPython%" -m ogame_bot.validation --minutes 60 --restart-minutes 15
pause
