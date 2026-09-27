@echo off
setlocal
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js LTS is required for the official YummyAnime Alloha resolver.
  echo Install Node.js, then run this file again.
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo npm was not found.
  pause
  exit /b 1
)

set ZIP=%TEMP%\yummy-lampa-plugin-main.zip
set TMP=%TEMP%\yummy-lampa-plugin-main

echo Downloading official YummyAnime resolver...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -UseBasicParsing 'https://github.com/yummyanime/yummy-lampa-plugin/archive/refs/heads/main.zip' -OutFile '%ZIP%'; if (Test-Path '%TMP%') { Remove-Item -Recurse -Force '%TMP%' }; Expand-Archive -Force '%ZIP%' '%TEMP%'"
if errorlevel 1 (
  echo Download failed.
  pause
  exit /b 1
)

if exist "alloha-resolver" rmdir /s /q "alloha-resolver"
xcopy /e /i /y "%TEMP%\yummy-lampa-plugin-main\server" "alloha-resolver" >nul

cd /d "%~dp0alloha-resolver"
call npm install
if errorlevel 1 (
  echo npm install failed.
  pause
  exit /b 1
)

echo.
echo Resolver installed to:
echo %~dp0alloha-resolver
echo.
echo Run start_alloha_resolver.bat before downloading from Alloha.
pause
