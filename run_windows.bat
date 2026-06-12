@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"

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
  echo ERROR: Python with Tkinter was not found.
  echo Install Python 3.11 or newer from python.org including Tcl/Tk.
  pause
  exit /b 1
)

echo Using: %PYCMD%
%PYCMD% run_gui.py
if errorlevel 1 (
  echo.
  echo The application exited with an error.
  echo Check SNES-Cheat-Patcher-crash.log in this folder.
  pause
)
