@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 launcher.py %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During installation, enable "Add Python to PATH", then run this file again.
    pause
    exit /b 1
  )
  python launcher.py %*
)
if errorlevel 1 pause
endlocal
