@echo off
setlocal
cd /d "%~dp0"
if not exist "config.yaml" copy /y "config.example.yaml" "config.yaml" >nul
set "taskPython=.venv\Scripts\python.exe"
if not exist "%taskPython%" set "taskPython=..\.venv\Scripts\python.exe"
if not exist "%taskPython%" (
    echo Ejecuta primero Instalar_windows.bat.
    pause
    exit /b 1
)
"%taskPython%" -m ogame_bot %*
set "taskExit=%errorlevel%"
if not "%taskExit%"=="0" pause
exit /b %taskExit%
