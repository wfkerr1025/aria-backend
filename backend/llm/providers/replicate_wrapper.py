# backend/llm/providers/replicate_wrapper.py

import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    def __init__(self):
        logger.debug("Initializing Replicate Provider")
        self.api_key = os.getenv("REPLICATE_API_KEY", "")
        self.api_url = "https://api.replicate.com/v1/predictions"
        logger.debug("Replicate Provider initialized")

    def run(self, request):
        logger.debug(f"run() called → model_id={request.model_id}")
        payload = self.build_payload(request)
        raw = self.call_api(payload)
        text = self.parse_response(raw)
        logger.debug("run() completed successfully")
        return text

    def stream(self, request, callback):
        logger.debug("Streaming API not implemented")
        raise NotImplementedError("Replicate streaming not wired yet.")

    def build_payload(self, request, stream: bool = False):
        logger.debug(f"Building payload → stream={stream}")
        return {
            "version": request.model_id,
            "input": {"prompt": request.prompt},
        }

    def call_api(self, payload):
        logger.debug("Calling Replicate API (non-streaming)")
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"Sending Replicate API request (key_present={bool(self.api_key)})")
        resp = requests.post(self.api_url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        logger.debug("Replicate API call succeeded")
        logger.info("Replicate API request succeeded")
        return resp.json()

    def parse_response(self, raw):
        logger.debug("Parsing Replicate response")
        try:
            return raw["output"]
        except Exception as e:
            logger.debug(f"parse_response() fallback → {e}")
            return str(raw)
