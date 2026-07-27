# ARIA-Lite

Local development and testing environment for ARIA-Lite.

## Quickstart (local)

1. Start backend and web UI:
   - Windows: .\dev_start.bat
   - Or run servers individually:
     - python -u backend\ws_server.py
     - python -u webui\secure_server.py

2. Tail logs:
   - Get-Content .\dev_server.log -Tail 200 -Wait
   - Get-Content .\dev_backend.log -Tail 200 -Wait

3. Smoke test:
   - pwsh .\scripts\smoke_test.ps1 -Port 3000 -TimeoutSeconds 10

## Project layout

- ackend/ — backend server code
- webui/ — front-end assets and secure_server.py
- scripts/ — helper scripts and smoke tests
- dev_start.bat — launcher for local dev

## Contributing

See CONTRIBUTING.md for local workflow and branch naming.
