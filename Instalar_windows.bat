@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo Se necesita Python 3.11 o superior instalado.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo No se pudieron instalar las dependencias.
    pause
    exit /b 1
)
if not exist "config.yaml" copy /y "config.example.yaml" "config.yaml" >nul
echo Instalacion terminada. Revisa config.yaml antes de abrir el bot.
pause
