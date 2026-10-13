@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE="

if exist "%PROJECT_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_DIR%venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%venv\Scripts\python.exe"
if not defined PYTHON_EXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        set "PYTHON_EXE=%%P"
        goto :python_found
    )
)

:python_found
if not defined PYTHON_EXE (
    echo No se encontro Python. Instala Python o crea un entorno .venv en esta carpeta.
    exit /b 1
)

if not exist "%PROJECT_DIR%.env" (
    echo Aviso: no se encontro .env. Configura las variables de WhatsApp antes de procesar mensajes.
)

start "Zonatech Flask" /D "%PROJECT_DIR%" cmd /k ""%PYTHON_EXE%" app.py"
start "Zonatech Worker" /D "%PROJECT_DIR%" cmd /k ""%PYTHON_EXE%" worker.py"

echo Flask y el worker se iniciaron en ventanas separadas.
