@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run install.bat
  pause
  exit /b 1
)
call .venv\Scripts\activate
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --collect-all imageio_ffmpeg --name YummyAnimeManager main.py
echo.
echo EXE: dist\YummyAnimeManager.exe
pause
