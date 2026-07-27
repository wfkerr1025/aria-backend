@echo off
cd /d "%~dp0"

echo Running ARIA Lite backend diagnostics...

REM Create a temporary Python file
echo from backend.health import health_report > temp_diag.py
echo print("=== ARIA Lite Provider Health ===") >> temp_diag.py
echo for provider, status in health_report().items(): >> temp_diag.py
echo     print(f"{provider:10} : {'ONLINE' if status else 'OFFLINE'}") >> temp_diag.py

py temp_diag.py

del temp_diag.py

pause
