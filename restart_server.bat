@echo off
cd /d "%~dp0"

echo Stopping old ARIA backend instances...
taskkill /IM "python.exe" /F >nul 2>&1
taskkill /IM "py.exe" /F >nul 2>&1

echo Starting fresh ARIA backend...
start "ARIA Backend" py -m backend.server

echo Backend restarted successfully.
pause
