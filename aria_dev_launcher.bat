@echo off
cd /d "%~dp0"

echo ============================================
echo        ARIA Lite Development Launcher
echo ============================================

REM Create diagnostics script
echo from backend.health import health_report > temp_diag.py
echo print("=== Provider Health ===") >> temp_diag.py
echo for provider, status in health_report().items(): >> temp_diag.py
echo     print(f"{provider:10} : {'ONLINE' if status else 'OFFLINE'}") >> temp_diag.py

py temp_diag.py
del temp_diag.py

echo Starting backend server...
start "ARIA Backend" py -m backend.server

echo Starting ARIA Lite UI...
start "ARIA UI" cmd /k python.exe app.py

echo Development environment ready.
pause
