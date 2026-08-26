"""ARIA Lite - the one way the reasoning core reaches a model.

Engine B (Phase 6 through 9.2) needs a `generate(prompt) -> str`. Before
this module existed it built its own LocalInferenceEngine and called
.infer() directly, which meant every synthesized answer went around
ProviderRouter: Cloud Mode ignored, the session's selected model
discarded, and absolute mode separation -- a registry invariant enforced
at resolve time -- simply not enforced.

The factory here closes over the routing context the transport already
resolved and hands back the plain callable Engine B expects. Everything
downstream of that callable is the same path an ordinary chat turn takes.

Two design points worth stating, because they are what keep the layering
honest:

    Engine B never learns that streaming exists. When a sink is supplied,
    tokens are pushed through it as a side effect and the complete text is
    still returned, so B's contract -- synchronous, returns a string -- is
    unchanged and its tests keep passing untouched. Streaming stays a
    transport concern, which is where it belongs.

    Mode separation raises rather than degrading. A model belonging to the
    wrong registry for the current mode is a caller bug, and quietly
    substituting a local model would hide it behind a plausible answer.
"""

from __future__ import annotations

from typing import Callable

from backend.core.model_registry import model_violates_mode_separation
from backend.core.local_inference_engine import InferenceMessage, InferenceRequest
from backend.core.provider_router import ProviderRouter
from backend.core.streaming_engine import StreamingEngine
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "GenerationUnavailable",
    "ModeSeparationError",
    "make_generator",
]

# Greedy by default, matching synthesis_engine.TEMPERATURE. A synthesis
# that reworded itself between two identical runs would undo the point of
# a deterministic prompt.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 1024


class ModeSeparationError(RuntimeError):
    """A model was requested that the current mode must not serve."""

    def __init__(self, model_id: str, mode: str) -> None:
        super().__init__(
            f"Model '{model_id}' belongs to the wrong registry for mode '{mode}'. "
            "Absolute mode separation forbids serving it; resolve a valid model "
            "before building a generator."
        )
        self.model_id = model_id
        self.mode = mode


class GenerationUnavailable(RuntimeError):
    """No provider could be resolved for this turn."""


def make_generator(
    model_id: str | None,
    mode: str,
    *,
    stream_sink: Callable[[str], None] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
) -> Callable[[str], str]:
    """A `generate(prompt) -> str` that routes like every other turn.

    model_id=None is meaningful and must be passed through: it is what
    tells ProviderRouter to use its own mode-based branches instead of the
    explicit-model one, and is how Cloud Mode and Automatic routing are
    reached at all.

    stream_sink, when given, receives each token as it arrives. The full
    text is still returned, so the caller gets both the live stream and the
    complete answer without Engine B knowing either happened.
    """
    if model_id and model_violates_mode_separation(model_id, mode):
        raise ModeSeparationError(model_id, mode)

    def generate(prompt: str) -> str:
        router = ProviderRouter()
        provider, resolved_model_id = router.resolve(model_id, prompt)

        if provider is None:
            # ProviderRouter returning (None, None) is its documented way
            # of refusing -- Cloud Mode with no provider configured, for
            # instance. Refusing loudly here keeps that refusal visible
            # instead of turning into an empty answer.
            raise GenerationUnavailable(
                f"No provider available for model_id={model_id!r} in mode {mode!r}."
            )

        request = InferenceRequest(
            model_id=resolved_model_id,
            messages=[InferenceMessage(role="user", content=prompt)],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        if stream_sink is None:
            return str(provider.infer(request)).strip()

        chunks: list[str] = []

        def collect(packet: dict) -> None:
            # StreamingEngine speaks in packets; the sink here speaks in
            # tokens, so the translation happens once, in this closure.
            if packet.get("type") == "stream_token":
                token = packet.get("token", "")
                chunks.append(token)
            stream_sink(packet)

        StreamingEngine().stream(request, collect)
        return "".join(chunks).strip()

    return generate
