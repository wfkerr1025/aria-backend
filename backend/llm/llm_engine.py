"""ARIA Lite - LLM prompt engine.

The seam where a turn's prompts are built. Kept separate from
conversation_manager (which decides WHICH messages survive) and from
memory_prompting (which decides WHAT ARIA knows) so that prompt assembly can
change without touching either.

Memory injection is fail-open by design. Retrieval touches the database, the
embedding backend and the ranking layer; if any of that is unavailable the
turn still has to reach the model. Every entry point here degrades to the
plain prompt and logs, rather than turning a memory problem into a chat
outage.
"""

from __future__ import annotations

import os

from logger import get_logger

try:
    from backend.llm import memory_prompting
except ImportError:  # running from inside the backend directory
    from llm import memory_prompting

logger = get_logger(__name__)

# Set to "0" to send prompts without any memory context.
ENV_MEMORY_ENABLED = "ARIA_MEMORY_PROMPTING"

DEFAULT_MEMORY_LIMIT = 8
# Fewer file chunks than memory items: a chunk is up to 1200 characters,
# where a note is usually a line or two, so six of them already fill more
# prompt than eight notes would.
DEFAULT_FILE_LIMIT = 6

# The headings each context renders under. Callers check for these to know
# what actually made it into a prompt, rather than inferring it from the
# prompt having changed -- with two possible sections, "it grew" no longer
# says which one grew it.
MEMORY_SECTION = "### Memory Context"
FILE_SECTION = "### File Context"


def memory_enabled() -> bool:
    """Whether memory context should be folded into prompts."""
    return os.environ.get(ENV_MEMORY_ENABLED, "1") != "0"


def get_memory_context(query: str, limit: int = DEFAULT_MEMORY_LIMIT) -> dict | None:
    """Build the context bundle for a query, or None if unavailable.

    Returns None rather than raising: an empty query has nothing to retrieve
    against, and a retrieval failure must not cost the user their turn.
    """
    if not memory_enabled() or not (query or "").strip():
        return None
    try:
        return memory_prompting.build_memory_context(query, limit=limit)
    except Exception:
        logger.exception("Memory context unavailable for this turn; prompting without it.")
        return None


def get_file_context(query: str, limit: int = DEFAULT_FILE_LIMIT) -> dict | None:
    """Ranked file chunks for a query, or None when there are none.

    Returns None rather than an empty bundle so a caller can tell "nothing
    relevant was found" from "files were never consulted", and fails open
    for the same reason memory does: an unreachable file index must not cost
    the user their turn.
    """
    if not (query or "").strip():
        return None
    try:
        context = memory_prompting.load_file_context(query, limit=limit)
    except Exception:
        logger.exception("File context unavailable for this turn; prompting without it.")
        return None
    if not context.get("items"):
        return None
    return context


def build_system_prompt(
    base_prompt: str,
    query: str = "",
    memory_context: dict | None = None,
    limit: int = DEFAULT_MEMORY_LIMIT,
    use_memory: bool = True,
    use_files: bool = False,
    file_context: dict | None = None,
    file_limit: int = DEFAULT_FILE_LIMIT,
) -> str:
    """The system prompt for this turn, with memory and/or files appended.

    Pass memory_context/file_context to reuse a bundle already built for the
    same turn; otherwise one is retrieved for `query`. Building each once and
    passing it to both prompt builders avoids running retrieval twice.

    use_memory and use_files are the router's vetoes. Each is checked before
    any retrieval happens, so a turn that cannot benefit from a subsystem
    does not pay for it. Both can be on: they append independently, and a
    turn that wants neither gets the base prompt back untouched.
    """
    prompt = base_prompt

    if use_memory:
        context = memory_context if memory_context is not None else get_memory_context(query, limit)
        if context:
            prompt = memory_prompting.build_system_prompt(prompt, context)

    if use_files:
        files = file_context if file_context is not None else get_file_context(query, file_limit)
        if files:
            prompt = memory_prompting.append_file_context(prompt, files)

    return prompt


def build_user_prompt(
    user_message: str,
    query: str = "",
    memory_context: dict | None = None,
    limit: int = DEFAULT_MEMORY_LIMIT,
    use_memory: bool = True,
    use_files: bool = False,
    file_context: dict | None = None,
    file_limit: int = DEFAULT_FILE_LIMIT,
) -> str:
    """The user prompt for this turn, wrapped with what bears on it.

    `query` defaults to the message itself -- what the user just asked is
    what retrieval should run against.

    When files are in play they replace the memory wrapper rather than
    stacking on top of it: a files.query turn routes with use_memory=False,
    so there is no memory bundle to show, and rendering both would put two
    "### User Message" headings in one prompt.
    """
    if use_files:
        files = file_context if file_context is not None else get_file_context(
            query or user_message, file_limit
        )
        if files:
            return memory_prompting.build_file_user_prompt(user_message, files)

    if not use_memory:
        return user_message
    context = memory_context
    if context is None:
        context = get_memory_context(query or user_message, limit)
    if not context:
        return user_message
    return memory_prompting.build_user_prompt(user_message, context)


def build_turn(base_prompt: str, user_message: str, limit: int = DEFAULT_MEMORY_LIMIT) -> dict:
    """Both prompts for one turn, sharing a single retrieval pass.

    Returns {"system", "user", "memory_context"}; memory_context is None
    when memory is disabled or unavailable, which is what a caller should
    log rather than guessing why a prompt looks bare.
    """
    context = get_memory_context(user_message, limit)
    return {
        "system": build_system_prompt(base_prompt, memory_context=context) if context else base_prompt,
        "user": build_user_prompt(user_message, memory_context=context) if context else user_message,
        "memory_context": context,
    }
