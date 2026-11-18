@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python scripts\bootstrap.py %*
) else (
  py -3 scripts\bootstrap.py %*
)
if errorlevel 1 (
  echo Startup failed. See README.md for installation steps.
  pause
  exit /b 1
)
