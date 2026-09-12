@echo off
setlocal enabledelayedexpansion
title ZCode Universal Model Gateway

rem Always work from the folder that contains this file, so double-clicking
rem behaves the same as running it from a terminal.
cd /d "%~dp0"

rem Keep messages ASCII-only here: batch files with non-ASCII text are parsed
rem with the console code page and break on many systems. The Chinese startup
rem banner is printed by the gateway itself (Python) instead.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not defined GATEWAY_HOST set "GATEWAY_HOST=127.0.0.1"
if not defined GATEWAY_PORT set "GATEWAY_PORT=8787"

rem ---------------------------------------------------------------------------
rem Find a suitable Python (3.11+). No machine-specific paths are hard-coded:
rem we try "python"/"python3" from PATH first, then common conda locations.
rem Candidates that are not a real Python 3.11+ (e.g. the Windows Store stub
rem named python.exe) are skipped automatically.
rem ---------------------------------------------------------------------------
set "PY="
for %%P in (
  "python"
  "python3"
  "%USERPROFILE%\miniconda3\python.exe"
  "%USERPROFILE%\anaconda3\python.exe"
  "%LOCALAPPDATA%\miniconda3\python.exe"
  "%LOCALAPPDATA%\anaconda3\python.exe"
  "%ProgramData%\miniconda3\python.exe"
  "%ProgramData%\Anaconda3\python.exe"
  "%CONDA_PREFIX%\python.exe"
) do (
  if not defined PY (
    %%~P -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)

if not defined PY (
  echo [ERROR] No Python 3.11+ found.
  echo         Install Python from https://www.python.org/ or Miniconda,
  echo         make sure "python" is on your PATH, then run this again.
  echo.
  pause
  exit /b 1
)
echo Using Python: %PY%

rem ---------------------------------------------------------------------------
rem Ensure runtime dependencies are present (no-op after the first run).
rem ---------------------------------------------------------------------------
"%PY%" -c "import fastapi, uvicorn, httpx, yaml, pydantic" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies ^(first run only^)...
  "%PY%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Check your network/proxy and retry.
    echo.
    pause
    exit /b 1
  )
)

"%PY%" -m gateway

echo.
echo Gateway stopped.
pause
