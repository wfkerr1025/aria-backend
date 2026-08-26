# backend/llm/providers/gemini_wrapper.py

import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    def __init__(self):
        logger.debug("Initializing Gemini Provider")
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        self.api_url = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        logger.debug("Gemini Provider initialized")

    def run(self, request):
        logger.debug(f"run() called → model_id={request.model_id}")
        payload = self.build_payload(request)
        raw = self.call_api(request.model_id, payload)
        text = self.parse_response(raw)
        logger.debug("run() completed successfully")
        return text

    def stream(self, request, callback):
        logger.debug(f"stream() called → model_id={request.model_id}")
        payload = self.build_payload(request, stream=True)

        for chunk in self.call_api_stream(request.model_id, payload):
            text = self.parse_stream_chunk(chunk)
            if text:
                logger.debug(f"stream() chunk received (len={len(text)})")
                callback({"type": "chat_stream", "content": text})

    def build_payload(self, request, stream: bool = False):
        logger.debug(f"Building payload → stream={stream}")
        return {
            "contents": [{"parts": [{"text": request.prompt}]}],
            "stream": stream,
        }

    def call_api(self, model_id, payload):
        logger.debug("Calling Gemini API (non-streaming)")
        url = self.api_url.format(model=model_id)
        params = {"key": self.api_key}
        logger.debug(f"Sending Gemini API request → model={model_id}, key_present={bool(self.api_key)}")
        resp = requests.post(url, params=params, json=payload, timeout=60)
        resp.raise_for_status()
        logger.debug("Gemini API call succeeded")
        logger.info("Gemini API request succeeded")
        return resp.json()

    def call_api_stream(self, model_id, payload):
        logger.debug("Streaming API not implemented")
        raise NotImplementedError("Gemini streaming not wired yet.")

    def parse_response(self, raw):
        logger.debug("Parsing Gemini response")
        try:
            return raw["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.debug(f"parse_response() fallback → {e}")
            return str(raw)

    def parse_stream_chunk(self, chunk):
        logger.debug("Parsing stream chunk")
        return str(chunk)
