@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  py -3.13 -c "import sys, tkinter; raise SystemExit(sys.version_info < (3,11))" >nul 2>nul && set "PYCMD=py -3.13"
  if not defined PYCMD py -3.12 -c "import sys, tkinter; raise SystemExit(sys.version_info < (3,11))" >nul 2>nul && set "PYCMD=py -3.12"
  if not defined PYCMD py -3.11 -c "import sys, tkinter; raise SystemExit(sys.version_info < (3,11))" >nul 2>nul && set "PYCMD=py -3.11"
  if not defined PYCMD py -3 -c "import sys, tkinter; raise SystemExit(sys.version_info < (3,11))" >nul 2>nul && set "PYCMD=py -3"
)
if not defined PYCMD (
  where python >nul 2>nul && python -c "import sys, tkinter; raise SystemExit(sys.version_info < (3,11))" >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
  echo ERROR: Python 3.11 or newer with Tkinter was not found.
  pause
  exit /b 1
)

echo Cleaning previous build output...
if exist ".build-venv" rmdir /s /q ".build-venv"
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "SNES-Cheat-Patcher-Studio.spec" del /q "SNES-Cheat-Patcher-Studio.spec"

echo Creating isolated build environment with %PYCMD%...
%PYCMD% -m venv .build-venv
if errorlevel 1 goto :error
call .build-venv\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :error
python -m pip install . pyinstaller ruff
if errorlevel 1 goto :error
python -m ruff check .
if errorlevel 1 goto :error
python -m unittest discover -s tests -v
if errorlevel 1 goto :error

rem Build as a folder rather than a self-extracting one-file executable.
python -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name "SNES-Cheat-Patcher-Studio" --paths src ^
  --icon "src\snes_cheat_patcher\assets\app_icon.ico" ^
  --collect-data snes_cheat_patcher ^
  --contents-directory _internal ^
  run_gui.py
if errorlevel 1 goto :error

echo.
echo ============================================================
echo Build complete.
echo   Folder : dist\SNES-Cheat-Patcher-Studio\
echo   EXE    : dist\SNES-Cheat-Patcher-Studio\SNES-Cheat-Patcher-Studio.exe
echo ============================================================
echo.
pause
exit /b 0

:error
echo.
echo ERROR: the build failed. Review the output above.
pause
exit /b 1
