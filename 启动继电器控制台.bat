@echo off
rem ============================================================
rem  USB Relay Console launcher  (ASCII only - cmd safe)
rem  Double click this file to start the GUI without a console.
rem ============================================================
cd /d "%~dp0"

set "PYW="
for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"
if not defined PYW if exist "C:\Python\Python310\pythonw.exe" set "PYW=C:\Python\Python310\pythonw.exe"
if not defined PYW if exist "C:\Python\Python311\pythonw.exe" set "PYW=C:\Python\Python311\pythonw.exe"
if not defined PYW if exist "C:\Python\Python39\pythonw.exe"  set "PYW=C:\Python\Python39\pythonw.exe"

if not defined PYW (
    echo [ERROR] pythonw.exe not found. Please install Python 3.8+ first.
    echo.
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0relay_gui.py"
exit /b 0
