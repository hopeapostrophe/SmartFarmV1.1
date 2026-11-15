@echo off
rem USB bridge launcher for Windows. Double-click to start.
rem Arduino/ESP32 connects over USB Type-C and shows up as a COM port.
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
  echo PowerShell not found. Install Python 3 and run: python usb_bridge.py
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_windows.ps1" %*
if errorlevel 1 pause
endlocal
