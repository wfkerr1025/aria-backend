"""A hand-run smoke check: load nemo-12b and say hello.

NOT A TEST, DESPITE THE NAME
----------------------------
pytest.ini collects test_*.py, so pytest imports this file -- and
everything below used to run at import: a real 8GB model load and a
real inference, on whatever machine happened to be collecting.

That works until the machine is busy. With the Unity Editor open, the
safety gate refused the load, the refusal became a collection ERROR,
and one script took the whole 4,600-test suite down with it before a
single test ran.

The guard is the same one run_all_tests.py needed for the same reason:
a file that DOES something on import cannot also be a file pytest is
allowed to import. Run it directly when you want it:

    python backend/test_local_inference.py
"""

from backend.core.local_inference_engine import (
    InferenceMessage,
    InferenceRequest,
    LocalInferenceEngine,
)

from logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    engine = LocalInferenceEngine()

    request = InferenceRequest(
        model_id="nemo-12b-q5",
        messages=[InferenceMessage(role="user", content="Hello ARIA Lite!")],
        max_tokens=128,
        temperature=0.7,
    )

    logger.info(engine.infer(request))


if __name__ == "__main__":
    main()
