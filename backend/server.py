# backend/server.py
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, AsyncGenerator, Dict

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.router import process_envelope
from backend.file_ops import FileOps
from backend.context_engine import ContextEngine
from backend.config import load_config

# ============================================================
# INIT
# ============================================================
app = FastAPI(title="ARIA Lite Backend", version="1.0.0")

file_ops = FileOps()
context_engine = ContextEngine()
config = load_config()


# ============================================================
# MODELS
# ============================================================
class Envelope(BaseModel):
    task: str | None = None
    action: str | None = None
    operation: str | None = None
    path: str | None = None
    content: str | None = None
    data: Any | None = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    metadata: Dict[str, Any] | None = None


# ============================================================
# UTILITIES
# ============================================================
def ok_response(operation: str, message: str | None = None, extra: Dict[str, Any] | None = None):
    payload = {
        "status": "ok",
        "operation": operation,
        "timestamp": time.time(),
    }
    if message:
        payload["message"] = message
    if extra:
        payload.update(extra)
    return payload


def error_response(operation: str, detail: str, status_code: int = 400):
    raise HTTPException(
        status_code=status_code,
        detail={
            "status": "error",
            "operation": operation,
            "detail": detail,
            "timestamp": time.time(),
        },
    )


# ============================================================
# HEALTH + PING
# ============================================================
@app.get("/ping")
def ping_get():
    return ok_response("ping")


@app.post("/ping")
def ping_post():
    return ok_response("ping")


@app.get("/health")
def health_get():
    return ok_response("health", "Backend online")


@app.post("/health")
def health_post():
    return ok_response("health", "Backend online")


# ============================================================
# UNIVERSAL ROUTER
# ============================================================
@app.post("/command")
async def command(request: Request):
    try:
        envelope_data = await request.json()
    except Exception as e:
        error_response("command", f"Invalid JSON: {e}")

    print("BACKEND RECEIVED ENVELOPE:", envelope_data)

    if not envelope_data.get("task") and not envelope_data.get("action"):
        error_response("command", "Envelope missing 'task' or 'action'")

    try:
        result = process_envelope(envelope_data)
    except Exception as e:
        return {
            "status": "error",
            "operation": "command",
            "detail": str(e),
            "timestamp": time.time(),
        }

    return result


# ============================================================
# CHAT ENDPOINT (for WebUI)
# ============================================================
@app.post("/chat")
async def chat(request: ChatRequest):
    envelope = {
        "task": "chat",
        "action": "message",
        "operation": "chat",
        "content": request.message,
        "data": {
            "session_id": request.session_id,
            "metadata": request.metadata or {},
        },
    }

    try:
        result = process_envelope(envelope)
    except Exception as e:
        return {
            "status": "error",
            "operation": "chat",
            "detail": str(e),
            "timestamp": time.time(),
        }

    reply = result.get("reply", result)
    return {
        "status": "ok",
        "operation": "chat",
        "reply": reply,
        "timestamp": time.time(),
    }


# ============================================================
# FILE OPS
# ============================================================
@app.post("/file_ops")
def file_operations(envelope: Envelope):
    op = envelope.operation
    path = envelope.path
    content = envelope.content or ""

    if not path:
        error_response("file_ops", "Missing 'path' for file_ops")

    if op == "read":
        return file_ops.read_file(path)
    if op == "write":
        return file_ops.write_file(path, content)
    if op == "delete":
        return file_ops.delete_file(path)

    error_response("file_ops", f"Unknown file_ops operation '{op}'")


# ============================================================
# CONTEXT ENGINE
# ============================================================
@app.post("/context")
def context(envelope: Envelope):
    data = envelope.data or {}
    text = data.get("text", "")
    mode = data.get("mode", "all")

    result = {}

    try:
        if mode in ("entities", "all"):
            result["entities"] = context_engine.extract_entities(text)
        if mode in ("intent", "all"):
            result["intent"] = context_engine.detect_intent(text)
        if mode in ("topics", "all"):
            result["topics"] = context_engine.suggest_topics(text)
        if mode == "snapshot":
            result["snapshot"] = context_engine.get_snapshot()
    except Exception as e:
        return {
            "status": "error",
            "operation": "context",
            "detail": str(e),
            "timestamp": time.time(),
        }

    return {
        "status": "ok",
        "operation": "context",
        "result": result,
        "timestamp": time.time(),
    }


# ============================================================
# DEBUG LOG
# ============================================================
@app.post("/debug/log")
def debug_log(envelope: Envelope):
    return ok_response("debug_log", "Debug log received", extra={"data": envelope.data or {}})


# ============================================================
# STREAMING ENDPOINT (FIXED)
# ============================================================
@app.post("/stream", response_model=None)
async def stream(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        return {
            "status": "error",
            "operation": "stream",
            "message": "Invalid JSON",
            "detail": str(e),
            "timestamp": time.time(),
        }

    prompt = data.get("prompt") or data.get("message") or ""
    provider = data.get("provider", "lmstudio")
    model = data.get("model", "mistral-nemo-12b-instruct-2407")

    async def event_generator():
        try:
            yield f"Provider: {provider}\n"
            yield f"Model: {model}\n"
            yield f"Prompt: {prompt}\n"
            yield "=== BEGIN STREAM ===\n"

            for i in range(10):
                yield f"token_{i}\n"
                await asyncio.sleep(0.05)

            yield "=== END STREAM ===\n"

        except Exception as e:
            tb = traceback.format_exc()
            yield f"[STREAM ERROR] {e}\n{tb}\n"

    return StreamingResponse(event_generator(), media_type="text/plain")


# ============================================================
# SERVER STARTUP
# ============================================================
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5000)
