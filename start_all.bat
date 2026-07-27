@echo off
cd /d "%~dp0"

echo Starting ARIA Lite backend...
start "" python -m backend.server

timeout /t 2 >nul

echo Starting ARIA Lite UI...
start "" python app.py

echo ARIA Lite is launching...
pause
