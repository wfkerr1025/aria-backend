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

## Unified logging system

Every runtime in the ARIA-Lite stack — the Python backend, the C# AriaLauncher,
the Electron main process, and the webui running inside it — logs into **one
file per ARIA run**, instead of four separate logs scattered across the repo.
They all do this by POSTing to a small local logging server
(`backend/logging_server.py`) that owns that one file for the lifetime of the run.

### Starting the logging server

It must be running before anything else, since it's what creates the run's log
file — everyone else just POSTs into it.

```bash
python backend/logging_server.py
```

It listens on `http://127.0.0.1:5001` and prints `LOG_SERVER_READY` to stdout
once it's up (the same convention `backend/ws_server.py` uses with
`WS_SERVER_READY`). When launching through AriaLauncher, this happens for you
automatically — `BackendManager.StartLoggingServer()` starts it first, waits
for that readiness marker, and only then starts the Python backend and Electron.

### Where logs are stored

`logs/ARIA_Run_<Month>-<Day>-<Year> -<Hour>.<Minute>.log`, relative to the repo
root — created fresh each time `logging_server.py` starts.

**Example:** `logs/ARIA_Run_8-21-2026 -01.31.log`

> **Note on the separator:** the original spec called for a `:` between hour
> and minute (`...-01:31.log`), matching the `%-m-%-d-%Y -%H:%M` format. That's
> illegal in Windows filenames — `:` is reserved for drive letters and NTFS
> alternate data streams, so a literal colon there silently splits the name
> into an empty file plus an invisible stream holding the real content. A `.`
> is used instead (`...-01.31.log`), confirmed with the project owner.
> Month/day are unpadded, hour/minute are zero-padded — matching the original
> example's "01:31" exactly, just with `.` in place of `:`.

### How rotation works

Every time `logging_server.py` starts, it creates a new `ARIA_Run_*.log` file
and then deletes the oldest ones beyond the 10 most recently modified —  so
`logs/` always holds at most 10 run logs. Rotation only runs at startup, not
mid-run, so a single run's file is never split or truncated while it's active.

### How to read logs

Each line is:

```
[YYYY-MM-DD HH:MM:SS.mmm] [LEVEL] [subsystem] message | context={...}
```

For example:

```
[2026-08-21 01:52:37.896] [INFO   ] [websocket] Incoming packet: chat_request | context={'packet_type': 'chat_request'}
[2026-08-21 01:52:38.109] [INFO   ] [provider_router] Model selection: explicit | context={'requested_model_id': 'mistral-7b-q4km', 'resolved_model_id': None}
[2026-08-21 01:52:41.519] [DEBUG  ] [streaming_engine] stream_token | context={'model_id': None, 'request_id': 3061052813200, 'token': 'Hello'}
```

Since every runtime writes into the same file in timestamp order, you can
follow one `chat_request` all the way from the webui (`bridge`/`chat`
subsystems) through Electron (`Electron`), the C# launcher (`Launcher`,
`Backend`, `Electron`), and the Python backend (`websocket`, `provider_router`,
`auto_selector`, `streaming_engine`, `server`, `router`) without cross-
referencing multiple files. `subsystem` tells you which runtime/module wrote
the line; `level` is one of `DEBUG`/`INFO`/`WARNING`/`ERROR`.

To follow a run live on Windows:

```powershell
Get-Content ".\logs\ARIA_Run_8-21-2026 -01.31.log" -Tail 200 -Wait
```

### What gets logged, and from where

| Runtime | File | Sends |
|---|---|---|
| Python backend | `backend/logger.py` (client) | incoming/outgoing packets, model selection, Auto Mode decisions, safety warnings, streaming tokens, exceptions, unknown model_id errors, router/server errors |
| C# AriaLauncher | `AriaLauncher/AriaLauncher/UnifiedRemoteLogger.cs` | backend start/crash, Electron start/crash, exceptions, IPC failures |
| Electron main process | `ARIA-Lite Desktop/log.js` | app ready, window creation, preload failures, IPC errors, WebSocket failures |
| webui (browser) | inline in `webui/core/bridge.js` and `webui/components/chat/chat.js` | bridge connect/errors, outgoing `chat_request`, incoming `chat_response`, chat send, UI errors, bubble rendering failures, exceptions |

> The spec named `webui/core/chat.js` — that file doesn't exist in this repo;
> the chat UI module actually lives at `webui/components/chat/chat.js`, so
> that's where the browser-side chat logging was added.

All four clients are fire-and-forget and swallow every failure: if
`logging_server.py` isn't running yet (or crashes mid-run), nothing else in
the stack breaks — log calls are just silently dropped.

## Contributing

See CONTRIBUTING.md for local workflow and branch naming.
