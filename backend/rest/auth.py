# backend/rest/auth.py

"""
Minimal API-key gating for /v1/* routes that need it — Phase 2 scope.

Gates POST /v1/chat and POST /v1/chat/stream (see router.py). /v1/health
and /v1/models stay open (no dependency wired on those routes).

NOT the same system as backend.core.key_manager — that module stores
this app's OWN credentials for calling OUT to cloud LLM providers
(OpenAI, Anthropic, ...). This module instead gates who may call INTO
this server at all: a single shared secret, checked via the
X-ARIA-API-Key header. No per-user keys, no issuance/rotation UI, no
expiry — genuinely minimal, by design, for this phase.

The key defaults to an obvious placeholder ("dev-key-change-me") so a
fresh checkout works immediately without extra setup, but is overridable
via the ARIA_REST_API_KEY environment variable — set that for anything
beyond local development; the default must never be relied on outside a
dev machine. Comparison uses hmac.compare_digest (constant-time) rather
than `==` to avoid a timing side-channel on an otherwise very thin check.
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException

from backend import ipc_errors
from backend.logger import log as unified_log
from logger import get_logger

logger = get_logger(__name__)

API_KEY_HEADER = "X-ARIA-API-Key"

# Intentionally an obvious, greppable placeholder — not a real secret.
# See module docstring for the environment-variable override.
_DEFAULT_DEV_KEY = "dev-key-change-me"


def configured_key() -> str:
    """
    The key this server currently expects. A thin wrapper (rather than
    inlining os.environ.get() at the call site) so tests can monkeypatch
    this one function to pin a known key without touching process
    environment variables.
    """
    return os.environ.get("ARIA_REST_API_KEY", _DEFAULT_DEV_KEY)


def require_api_key(x_aria_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER)) -> None:
    """
    FastAPI dependency. Wire into a route with:

        @router.post("/chat", dependencies=[Depends(require_api_key)])

    Raises 401 with the shared {"error": {"code", "message"}} shape
    (see router.py's _rest_error — duplicated here rather than imported
    to avoid a circular import, since router.py will import this module
    to wire the dependency onto its routes) unless the request carries a
    header matching configured_key().
    """
    expected = configured_key()

    if not x_aria_api_key or not hmac.compare_digest(x_aria_api_key, expected):
        logger.warning("REST auth failed — missing or invalid %s header", API_KEY_HEADER)
        unified_log("rest", "WARNING", "REST auth failed: missing or invalid API key", {
            "header": API_KEY_HEADER,
        })
        raise HTTPException(
            status_code=401,
            detail={"error": {
                "code": ipc_errors.UNAUTHORIZED,
                "message": f"Missing or invalid {API_KEY_HEADER} header.",
            }},
        )
