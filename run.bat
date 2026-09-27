@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run install.bat
  pause
  exit /b 1
)
if not "%YUMMY_SKIP_UPDATE%"=="1" (
  .venv\Scripts\python.exe update_source.py
  if errorlevel 1 echo Update failed. Starting installed version.
)
.venv\Scripts\python.exe main.py
