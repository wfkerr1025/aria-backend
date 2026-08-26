from dataclasses import dataclass
from typing import List, Optional, Iterable

from .model_loader import ModelLoader
from .model_registry import get_model


from logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------
# Request DTO for local inference
# ---------------------------------------------------------
@dataclass
class InferenceMessage:
    role: str   # "system" | "user" | "assistant" | "tool"
    content: str


@dataclass
class InferenceRequest:
    model_id: str
    messages: List[InferenceMessage]
    max_tokens: int = 2048
    temperature: float = 0.7
    use_tools: bool = False
    tools: Optional[list] = None
    # Set by backend/websocket/handlers.py and backend/server.py after
    # conversation_manager.detect_intent() runs, so providers can make
    # intent-aware decisions (see local_provider.py's
    # select_model_for_request()) without re-deriving intent themselves.
    # Optional/defaulted so existing callers that don't set it keep working.
    intent: Optional[str] = None
    # Set when this request is a "Proceed Anyway" continuation after a
    # safety_warning (backend/websocket/handlers.py::_handle_model_override) —
    # threaded down to ModelLoader.load_model(..., allow_override=True) so
    # the safety gate that already showed the user a warning doesn't just
    # reject the load a second time silently.
    allow_override: bool = False


# ---------------------------------------------------------
# Local Inference Engine
# ---------------------------------------------------------
class LocalInferenceEngine:
    """
    Policy layer over the ModelLoader:
    - Builds prompts from messages
    - Applies sampling defaults
    - Calls ModelLoader (run / run_stream)
    """

    def __init__(self):
        logger.debug("Initializing LocalInferenceEngine")
        self.loader = ModelLoader()
        logger.debug("ModelLoader instance created")

    # -----------------------------------------------------
    # Public API: non-streaming
    # -----------------------------------------------------
    def infer(self, request: InferenceRequest) -> str:
        logger.debug(f"infer() called → model_id={request.model_id}, "
                      f"messages={len(request.messages)}, "
                      f"max_tokens={request.max_tokens}, temp={request.temperature}")

        model_cfg = get_model(request.model_id)
        if model_cfg is None:
            logger.debug(f"ERROR: Unknown model ID '{request.model_id}'")
            raise ValueError(f"Unknown model ID: {request.model_id}")

        prompt = self._build_prompt(request)
        logger.debug(f"Prompt built (length={len(prompt)})")

        self.loader.load_model(request.model_id)
        logger.debug(f"Model loaded → {request.model_id}")

        text = self.loader.run(
            prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        logger.debug(f"Model inference complete (output_length={len(text)})")
        return text

    # -----------------------------------------------------
    # Public API: streaming
    # -----------------------------------------------------
    def infer_stream(self, request: InferenceRequest) -> Iterable[str]:
        logger.debug(f"infer_stream() called → model_id={request.model_id}")

        model_cfg = get_model(request.model_id)
        if model_cfg is None:
            logger.debug(f"ERROR: Unknown model ID '{request.model_id}'")
            raise ValueError(f"Unknown model ID: {request.model_id}")

        prompt = self._build_prompt(request)
        logger.debug(f"Streaming prompt built (length={len(prompt)})")

        self.loader.load_model(request.model_id)
        logger.debug(f"Model loaded for streaming → {request.model_id}")

        for chunk in self.loader.run_stream(
            prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        ):
            logger.debug(f"Streaming chunk received (len={len(chunk)})")
            yield chunk

        logger.debug("Streaming inference complete")

    # -----------------------------------------------------
    # Prompt builder
    # -----------------------------------------------------
    def _build_prompt(self, request: InferenceRequest) -> str:
        logger.debug("Building prompt from messages")

        parts = []
        for msg in request.messages:
            parts.append(f"[{msg.role.upper()}] {msg.content}\n")

        prompt = "\n".join(parts)
        logger.debug(f"Prompt built successfully (lines={len(parts)})")

        return prompt
