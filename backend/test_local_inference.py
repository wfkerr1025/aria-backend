from backend.core.local_inference_engine import LocalInferenceEngine, InferenceRequest, InferenceMessage

from logger import get_logger

logger = get_logger(__name__)

engine = LocalInferenceEngine()

req = InferenceRequest(
    model_id="nemo-12b-q5",
    messages=[InferenceMessage(role="user", content="Hello ARIA Lite!")],
    max_tokens=128,
    temperature=0.7
)

logger.info(engine.infer(req))
