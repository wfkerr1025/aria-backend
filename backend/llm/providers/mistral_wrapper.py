# backend/llm/providers/mistral_wrapper.py

import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    def __init__(self):
        logger.debug("Initializing Mistral Provider")
        self.api_key = os.getenv("MISTRAL_API_KEY", "")
        self.api_url = "https://api.mistral.ai/v1/chat/completions"
        logger.debug("Mistral Provider initialized")

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
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": stream,
        }

    def call_api(self, payload):
        logger.debug("Calling Mistral API (non-streaming)")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"Sending Mistral API request (key_present={bool(self.api_key)})")
        resp = requests.post(self.api_url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        logger.debug("Mistral API call succeeded")
        logger.info("Mistral API request succeeded")
        return resp.json()

    def call_api_stream(self, payload):
        logger.debug("Streaming API not implemented")
        raise NotImplementedError("Mistral streaming not wired yet.")

    def parse_response(self, raw):
        logger.debug("Parsing Mistral response")
        try:
            return raw["choices"][0]["message"]["content"]
        except Exception as e:
            logger.debug(f"parse_response() fallback → {e}")
            return str(raw)

    def parse_stream_chunk(self, chunk):
        logger.debug("Parsing stream chunk")
        return str(chunk)
