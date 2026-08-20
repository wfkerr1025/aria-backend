# backend/websocket/handlers.py

from __future__ import annotations
import json
import asyncio
import time

from backend.core.provider_router import ProviderRouter
from backend.core.streaming_engine import StreamingEngine
from backend.core.local_inference_engine import InferenceRequest, InferenceMessage

# Safety + model metadata
from backend.core.safety_manager import evaluate_safety
from backend.core.model_registry import get_model, get_default_model_id
from backend.core.lighter_model_engine import LighterModelEngine

from logger import get_logger

logger = get_logger(__name__)


class WebSocketHandler:
    """
    Handles WebSocket messages from the frontend.
    Converts incoming packets → InferenceRequest
    Routes inference through ProviderRouter
    Streams tokens via StreamingEngine
    """

    def __init__(self, websocket):
        self.websocket = websocket
        self.router = ProviderRouter()
        self.streamer = StreamingEngine()
        self.suggester = LighterModelEngine()

        logger.info("WebSocketHandler initialized.")

    # -----------------------------------------------------
    # Main entry point for incoming messages
    # -----------------------------------------------------
    async def handle(self):
        logger.debug("Starting async message loop.")

        try:
            async for raw in self.websocket:
                logger.debug("Raw incoming packet: %s", raw)

                try:
                    packet = self._safe_json(raw)
                    logger.debug("Parsed packet: %s", packet)
                    await self._dispatch(packet)

                except Exception as e:
                    logger.exception("Packet parse error: %s", e)
                    await self._send({
                        "type": "error",
                        "message": f"Invalid packet: {str(e)}"
                    })
        finally:
            logger.debug("Exiting async message loop.")

    # -----------------------------------------------------
    # Safe JSON loader
    # -----------------------------------------------------
    def _safe_json(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        try:
            return json.loads(raw)
        except Exception:
            logger.exception("Malformed JSON: %s", raw)
            raise ValueError(f"Malformed JSON: {raw}")

    # -----------------------------------------------------
    # Dispatch packet types
    # -----------------------------------------------------
    async def _dispatch(self, packet: dict):
        ptype = packet.get("type")
        logger.debug("Dispatching packet type: %s", ptype)

        if ptype == "shutdown":
            logger.info("Shutdown command received.")
            await self.websocket.close()
            loop = asyncio.get_event_loop()
            loop.stop()
            return

        if ptype == "chat_request":
            logger.debug("chat_request received.")
            await self._handle_chat_request(packet)
            return

        if ptype == "load_model_override":
            logger.debug("load_model_override received.")
            await self._handle_model_override(packet)
            return

        if ptype == "switch_to_lighter_model":
            logger.debug("switch_to_lighter_model received.")
            await self._handle_switch_to_lighter_model(packet)
            return

        logger.warning("Unknown packet type: %s", ptype)
        await self._send({
            "type": "error",
            "message": f"Unknown packet type: {ptype}"
        })

    # -----------------------------------------------------
    # Handle chat_request from frontend
    # -----------------------------------------------------
    async def _handle_chat_request(self, packet: dict):
        model_id = packet.get("modelId") or get_default_model_id()
        logger.debug("Handling chat_request for model: %s", model_id)

        if not model_id:
            logger.warning("chat_request has no modelId and no default model is configured.")
            await self._send({
                "type": "error",
                "message": "No modelId provided and no default model is configured"
            })
            return

        model_cfg = get_model(model_id)
        logger.debug("Loaded model config for %s", model_id)

        decision = evaluate_safety(model_cfg)
        logger.debug("Safety decision: requires_warning=%s", decision.requires_warning)

        if decision.requires_warning:
            logger.info("Safety warning triggered for model %s.", model_id)
            suggestions = self.suggester.suggest(decision.snapshot, model_cfg)

            await self._send({
                "type": "safety_warning",
                "model_id": model_id,
                "message": decision.message,
                "projected": {
                    "cpu": decision.projected_cpu_pct,
                    "ram": decision.projected_ram_pct,
                    "vram": decision.projected_vram_pct
                },
                "suggestions": [
                    {"id": s.model_id, "reason": s.reason}
                    for s in suggestions
                ]
            })
            return

        await self._start_inference({**packet, "modelId": model_id})

    # -----------------------------------------------------
    # Handle user override (Proceed Anyway)
    # -----------------------------------------------------
    async def _handle_model_override(self, packet: dict):
        model_id = packet.get("model_id")
        logger.debug("Model override requested for: %s", model_id)

        if not model_id:
            logger.warning("load_model_override missing model_id.")
            await self._send({
                "type": "error",
                "message": "Missing model_id in load_model_override"
            })
            return

        await self._start_inference({
            "modelId": model_id,
            "messages": [{"role": "user", "content": ""}],
            "maxTokens": 2048,
            "temperature": 0.7
        })

    # -----------------------------------------------------
    # Handle user choosing lighter model
    # -----------------------------------------------------
    async def _handle_switch_to_lighter_model(self, packet: dict):
        model_id = packet.get("model_id")
        logger.debug("Switching to lighter model: %s", model_id)

        if not model_id:
            logger.warning("switch_to_lighter_model missing model_id.")
            await self._send({
                "type": "error",
                "message": "Missing model_id in switch_to_lighter_model"
            })
            return

        await self._send({
            "type": "info",
            "message": f"Switching to lighter model: {model_id}"
        })

        await self._start_inference({
            "modelId": model_id,
            "messages": [{"role": "user", "content": ""}],
            "maxTokens": 2048,
            "temperature": 0.7
        })

    # -----------------------------------------------------
    # Start inference (shared by normal + override)
    # -----------------------------------------------------
    async def _start_inference(self, packet: dict):
        model_id = packet.get("modelId")
        messages_raw = packet.get("messages", [])

        logger.info("Starting inference for model: %s", model_id)
        start_time = time.monotonic()

        if not model_id:
            logger.warning("Inference request missing modelId.")
            await self._send({
                "type": "error",
                "message": "Missing modelId in inference request"
            })
            return

        messages = [
            InferenceMessage(role=m.get("role", "user"), content=m.get("content", ""))
            for m in messages_raw
        ]

        logger.debug("Messages: %s", messages_raw)

        request = InferenceRequest(
            model_id=model_id,
            messages=messages,
            max_tokens=packet.get("maxTokens", 2048),
            temperature=packet.get("temperature", 0.7),
        )

        logger.debug("InferenceRequest created.")
        try:
            await self._stream_inference(request)
        finally:
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.info("Inference for model %s finished in %s ms", model_id, elapsed_ms)

    # -----------------------------------------------------
    # Streaming wrapper
    # -----------------------------------------------------
    async def _stream_inference(self, request: InferenceRequest):
        logger.debug("Starting streaming inference.")

        def send_packet_sync(packet: dict):
            logger.debug("Streaming packet: %s", packet)
            asyncio.create_task(self._send(packet))

        try:
            self.streamer.stream(request, send_packet_sync)
        except Exception as e:
            logger.exception("Inference error: %s", e)
            await self._send({
                "type": "error",
                "message": f"Inference error: {str(e)}"
            })

    # -----------------------------------------------------
    # Send packet to frontend
    # -----------------------------------------------------
    async def _send(self, packet: dict):
        try:
            logger.debug("Sending packet: %s", packet)
            await self.websocket.send(json.dumps(packet))
        except Exception as e:
            logger.exception("Failed to send packet: %s", e)
