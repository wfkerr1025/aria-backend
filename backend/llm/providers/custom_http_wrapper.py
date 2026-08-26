# backend/llm/providers/custom_http_wrapper.py

import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    """
    Generic HTTP-based provider.
    You can point this at any custom LLM endpoint.
    """

    def __init__(self):
        logger.debug("Initializing Custom HTTP Provider")
        self.api_url = os.getenv("CUSTOM_HTTP_URL", "")
        self.api_key = os.getenv("CUSTOM_HTTP_API_KEY", "")
        logger.debug("Custom HTTP Provider initialized")

    def run(self, request):
        logger.debug(f"run() called → model_id={request.model_id}")
        payload = self.build_payload(request)
        raw = self.call_api(payload)
        text = self.parse_response(raw)
        logger.debug("run() completed successfully")
        return text

    def stream(self, request, callback):
        logger.debug(f"stream() called → model_id={request.model_id}")
        payload = self.build_payload(request, stream=True)

        for chunk in self.call_api_stream(payload):
            text = self.parse_stream_chunk(chunk)
            if text:
                logger.debug(f"stream() chunk received (len={len(text)})")
                callback({"type": "chat_stream", "content": text})

    def build_payload(self, request, stream: bool = False):
        logger.debug(f"Building payload → stream={stream}")
        return {
            "model": request.model_id,
            "prompt": request.prompt,
            "stream": stream,
        }

    def call_api(self, payload):
        logger.debug("Calling Custom HTTP API (non-streaming)")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"Sending Custom HTTP API request (key_present={bool(self.api_key)})")
        resp = requests.post(self.api_url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        logger.debug("Custom HTTP API call succeeded")
        logger.info("Custom HTTP API request succeeded")
        return resp.json()

    def call_api_stream(self, payload):
        logger.debug("Streaming API not implemented")
        raise NotImplementedError("Custom HTTP streaming not wired yet.")

    def parse_response(self, raw):
        logger.debug("Parsing Custom HTTP response")
        try:
            return raw.get("output", str(raw))
        except Exception as e:
            logger.debug(f"parse_response() fallback → {e}")
            return str(raw)

    def parse_stream_chunk(self, chunk):
        logger.debug("Parsing stream chunk")
        return str(chunk)
