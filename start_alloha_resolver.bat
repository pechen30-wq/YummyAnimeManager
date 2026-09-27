@echo off
cd /d "%~dp0alloha-resolver"
if not exist "package.json" (
  echo Resolver is not installed.
  echo Run install_alloha_resolver.bat first.
  pause
  exit /b 1
)
echo Starting official YummyAnime resolver on http://127.0.0.1:8790
call npm start
