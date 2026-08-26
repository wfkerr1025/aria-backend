import time

from logger import get_logger

logger = get_logger(__name__)


def run_metadata_tool(envelope):
    start = time.monotonic()
    query = envelope.get("query")
    logger.debug("run_metadata_tool invoked: query=%s", query)

    if query == "get_llm_details":
        result = {
            "provider": "local",
            "model": "mistral-nemo-12b-instruct-2407"
        }
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("run_metadata_tool completed: query=%s elapsed_ms=%.2f", query, elapsed_ms)
        return result

    logger.warning("run_metadata_tool: unknown query=%s", query)
    return {"error": "Unknown metadata query"}
