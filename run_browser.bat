@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0..\ai-data-master\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo DB Data Browser
echo URL: http://127.0.0.1:8788
echo.
"%PYTHON_EXE%" "%~dp0server.py" --host 127.0.0.1 --port 8788

echo.
echo Serwer zostal zatrzymany.
pause
