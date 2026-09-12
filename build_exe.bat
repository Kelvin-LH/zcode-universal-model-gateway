@echo off
rem Build a standalone ZUMG.exe with PyInstaller.
rem
rem Requirements: Python 3.11+ with the project dependencies installed, plus
rem PyInstaller (installed automatically below if missing).
rem
rem Output: dist\ZUMG.exe  (~18 MB, single file, no Python needed to run it)

setlocal
cd /d "%~dp0"

set "PY=python"
where %PY% >nul 2>nul || (
  echo [ERROR] "python" not found on PATH. Install Python 3.11+ first.
  pause
  exit /b 1
)

%PY% -c "import PyInstaller" >nul 2>nul || (
  echo Installing PyInstaller...
  %PY% -m pip install pyinstaller || (echo [ERROR] pip install failed & pause & exit /b 1)
)

echo.
echo Building ZUMG.exe ...
%PY% -m PyInstaller --clean --noconfirm zumg.spec
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed. See the output above.
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo   Build complete:  dist\ZUMG.exe
echo ----------------------------------------------------------
echo   Copy ZUMG.exe to any Windows machine and double-click it.
echo   On first run it creates config.yaml next to itself.
echo   Then open http://127.0.0.1:8787/
echo ==========================================================
echo.
pause
