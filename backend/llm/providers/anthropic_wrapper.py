# backend/llm/providers/anthropic_wrapper.py

import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    """
    Anthropic wrapper.
    Future-proof structure:
        - run(): non-streaming
        - stream(): streaming
        - build_payload(): request → provider JSON
        - parse_response(): provider JSON → text
        - call_api(): single place to wire real SDK/HTTP
    """

    def __init__(self):
        logger.debug("Initializing Anthropic Provider")
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.api_url = "https://api.anthropic.com/v1/messages"
        logger.debug("Anthropic Provider initialized")

    # -----------------------------
    # Public API
    # -----------------------------
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

    # -----------------------------
    # Internal helpers
    # -----------------------------
    def build_payload(self, request, stream: bool = False):
        logger.debug(f"Building payload → stream={stream}")
        return {
            "model": request.model_id,
            "messages": [
                {"role": "user", "content": request.prompt}
            ],
            "stream": stream,
        }

    def call_api(self, payload):
        logger.debug("Calling Anthropic API (non-streaming)")
        headers = {
            "x-api-key": self.api_key,
            "content-type": "application/json",
        }
        logger.debug(f"Sending Anthropic API request (key_present={bool(self.api_key)})")
        resp = requests.post(self.api_url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        logger.debug("Anthropic API call succeeded")
        logger.info("Anthropic API request succeeded")
        return resp.json()

    def call_api_stream(self, payload):
        logger.debug("Streaming API not implemented")
        raise NotImplementedError("Anthropic streaming HTTP/SDK not wired yet.")

    def parse_response(self, raw):
        logger.debug("Parsing Anthropic response")
        try:
            return raw["content"][0]["text"]
        except Exception as e:
            logger.debug(f"parse_response() fallback → {e}")
            return str(raw)

    def parse_stream_chunk(self, chunk):
        logger.debug("Parsing stream chunk")
        return str(chunk)
