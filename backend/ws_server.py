# backend/ws_server.py
# Path B WebSocket server for ARIA Lite

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

# Ensure UTF‑8 output for logs
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# Force ARIA-Lite root as working directory
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from backend.router import process_envelope  # correct backend router

WS_HOST = "localhost"
WS_PORT = 8765


async def translate_packet(pkt_type: str, payload: dict) -> dict:
    """
    Convert WebUI packets into backend envelopes.
    Convert backend envelopes into WebUI packets.
    """

    # -----------------------------
    # CHAT REQUEST → LLM / intent_engine
    # -----------------------------
    if pkt_type == "chat_request":
        envelope = {
            "task": "llm",
            "prompt": payload.get("text", "")
        }

        result = process_envelope(envelope)

        # router returns: { status, operation, timestamp, result: { result: { content } } }
        outer = result.get("result", {})
        inner = outer.get("result", {})
        content = inner.get("content")

        # Tool result packet (if LLM returned a tool envelope or tool-mode content)
        if isinstance(content, dict) and content.get("task"):
            return {
                "type": "tool_result",
                "payload": {
                    "tool": content.get("task"),
                    "result": content,
                },
            }

        # Normal assistant text
        return {
            "type": "chat_response",
            "payload": {
                "text": content if isinstance(content, str) else "",
            },
        }

    # -----------------------------
    # FILE OPS
    # -----------------------------
    if pkt_type == "file_ops_request":
        envelope = {
            "task": "file_ops",
            **payload,
        }
        result = process_envelope(envelope)
        return {
            "type": "file_ops_response",
            "payload": result,
        }

    # -----------------------------
    # TOOL REQUEST (explicit)
    # -----------------------------
    if pkt_type == "tool_request":
        envelope = {**payload}
        envelope["task"] = envelope.get("task", "tool")

        result = process_envelope(envelope)

        return {
            "type": "tool_result",
            "payload": {
                "tool": envelope.get("task"),
                "result": result.get("result", result),
            },
        }

    # -----------------------------
    # PING / HEALTH
    # -----------------------------
    if pkt_type in ("ping", "health"):
        envelope = {"task": pkt_type}
        result = process_envelope(envelope)
        return {
            "type": pkt_type + "_response",
            "payload": result,
        }

    # -----------------------------
    # DEFAULT FALLBACK
    # -----------------------------
    envelope = {
        "task": pkt_type,
        **payload,
    }
    result = process_envelope(envelope)
    return {
        "type": pkt_type + "_response",
        "payload": result,
    }


async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
    print("[WS] Client connected")

    try:
        async for message in websocket:
            try:
                packet = json.loads(message)
            except Exception:
                print("[WS] Invalid JSON:", message)
                continue

            pkt_type = packet.get("type")
            payload = packet.get("payload", {}) or {}

            print(f"[WS] RECV: {pkt_type} {payload}")

            response = await translate_packet(pkt_type, payload)

            print(f"[WS] SEND: {response}")
            await websocket.send(json.dumps(response))

    except websockets.exceptions.ConnectionClosed:
        print("[WS] Client disconnected")


async def start_ws_server() -> None:
    print(f"[WS] Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    async with websockets.serve(handle_client, WS_HOST, WS_PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(start_ws_server())
