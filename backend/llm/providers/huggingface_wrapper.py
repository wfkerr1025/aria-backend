# backend/llm/providers/huggingface_wrapper.py

import os
import requests

from logger import get_logger

logger = get_logger(__name__)


class Provider:
    def __init__(self):
        logger.debug("Initializing HuggingFace Provider")
        self.api_key = os.getenv("HUGGINGFACE_API_KEY", "")
        self.api_url = "https://api-inference.huggingface.co/models/{model}"
        logger.debug("HuggingFace Provider initialized")

    def run(self, request):
        logger.debug(f"run() called → model_id={request.model_id}")
        payload = self.build_payload(request)
        raw = self.call_api(request.model_id, payload)
        text = self.parse_response(raw)
        logger.debug("run() completed successfully")
        return text

    def stream(self, request, callback):
        logger.debug("Streaming API not implemented")
        raise NotImplementedError("HuggingFace streaming not wired yet.")

    def build_payload(self, request, stream: bool = False):
        logger.debug(f"Building payload → stream={stream}")
        return {"inputs": request.prompt}

    def call_api(self, model_id, payload):
        logger.debug("Calling HuggingFace API (non-streaming)")
        url = self.api_url.format(model=model_id)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"Sending HuggingFace API request → model={model_id}, key_present={bool(self.api_key)}")
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        logger.debug("HuggingFace API call succeeded")
        logger.info("HuggingFace API request succeeded")
        return resp.json()

    def parse_response(self, raw):
        logger.debug("Parsing HuggingFace response")
        try:
            if isinstance(raw, list) and raw:
                text = raw[0].get("generated_text", str(raw))
                logger.debug("Normalized via generated_text")
                return text
            logger.debug("Normalized via fallback str(raw)")
            return str(raw)
        except Exception as e:
            logger.debug(f"parse_response() fallback → {e}")
            return str(raw)
