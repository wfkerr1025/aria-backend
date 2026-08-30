# backend/core/provider_router.py

from __future__ import annotations

from backend.core.mode_manager import ModeManager
from backend.core.auto_selector import AutoSelector
from backend.core.local_model_selector import ensure_default_local_model
from backend.core.complexity_router import select_local_model_for_prompt
from backend.core.task_classifier import classify_task_type, preferred_cloud_provider_for_task
from backend.core.model_registry import get_model as get_model_config, model_violates_mode_separation
from backend.core import model_selector
from backend.llm.providers.provider_registry import get_provider
from backend.core import key_manager
from backend.core import routing_history

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


class ProviderRouter:
    """
    Central provider resolution engine for ARIA Lite.
    Handles:
        - explicit model selection
        - Local / Cloud / Automatic Model Routing mode selection
        - cloud provider fallback
        - local fallback
    """

    def __init__(self, mode_manager: ModeManager | None = None):
        logger.debug("Initializing ProviderRouter")
        self.mode_manager = mode_manager or ModeManager()
        self.auto_selector = AutoSelector()
        logger.debug("ProviderRouter initialized successfully")

    # ---------------------------------------------------------
    # Resolve provider + model_id
    # ---------------------------------------------------------
    def resolve(self, model_id: str | None, prompt: str | None = None, history=()):
        logger.debug(
            f"resolve() called → model_id={model_id}, prompt_len={len(prompt or '')}"
        )

        mode = self.mode_manager.get_mode()
        logger.debug(f"Current mode → {mode}")

        # -----------------------------------------------------
        # 1. Explicit model_id — overrides everything EXCEPT absolute
        # mode separation. An explicit model_id can arrive two ways:
        # a one-shot request field, or the session-wide "switch to X"
        # pin (mode_manager.get_explicit_model_override()) — the latter
        # is documented elsewhere as a "permanent, cross-mode pin", but
        # that was never meant to license genuinely crossing the
        # local/cloud registry boundary: a user saying "switch to
        # mistral" while already in Cloud Mode must not silently start
        # using a local model while the UI still reads "Cloud Mode" —
        # exactly the "local provider, in Cloud Mode" contradiction this
        # fixes. Same for an unrecognized model_id in Cloud Mode: without
        # this gate, _resolve_explicit_model()'s own "not found" branch
        # falls back to a LOCAL model, which is just as much a violation.
        # Automatic Model Routing has no such restriction — it's the only
        # mode allowed to use either registry, so this check never
        # applies to it.
        # -----------------------------------------------------
        if model_id and model_id != "automatic":
            if not model_violates_mode_separation(model_id, mode):
                logger.debug(f"Explicit model_id detected → {model_id}")
                provider, resolved_model = self._resolve_explicit_model(model_id)
                logger.debug(
                    f"Explicit resolution → provider={provider}, model={resolved_model}"
                )
                unified_log("provider_router", "INFO", "Model selection: explicit", {
                    "requested_model_id": model_id, "resolved_model_id": resolved_model,
                })
                return provider, resolved_model

            logger.warning(
                f"Ignoring explicit model_id '{model_id}' — incompatible with current mode "
                f"'{mode}' (absolute mode separation). Falling through to normal {mode}-mode resolution."
            )
            unified_log("provider_router", "WARNING", "Explicit model_id ignored — wrong registry for current mode", {
                "requested_model_id": model_id, "mode": mode,
            })
            # Deliberately falls through to the mode-based branches below
            # rather than returning — "ignore the incompatible pin and
            # resolve normally for this mode" is the correct behavior,
            # not an error.

        # -----------------------------------------------------
        # 2. Local Mode — ABSOLUTE MODE SEPARATION: this branch never
        # imports, calls, or falls back to anything cloud-related
        # (no AutoSelector, no key_manager, no cloud_rank) — every path
        # through it ends at a local model_id via
        # backend.core.complexity_router / local_model_selector, full
        # stop. Task-complexity-based tier selection (not a single
        # sticky "default" model): "ARIA must choose the best local
        # model for the task" applies in Local Mode the same as
        # Automatic Model Routing's local branch (see
        # AutoSelector.select_provider()), it
        # just never has the option to escalate to cloud, and a hardware
        # limit steps DOWN to a smaller local model
        # (complexity_router.py's ladder), never sideways to cloud.
        # Falls back to ensure_default_local_model() only if the
        # registry has no usable local model at all (complexity_router
        # itself already falls through to the registry's
        # default/fallback/emergency roles first, so this is a
        # last-resort net, not the common path).
        # -----------------------------------------------------
        if mode == "local":
            logger.debug("Mode=local → selecting local model by task complexity")
            local_model = select_local_model_for_prompt(prompt or "") or ensure_default_local_model()
            provider = get_provider("local")
            logger.debug(f"Local mode resolved → model={local_model}")
            unified_log("provider_router", "INFO", "Model selection: local mode", {
                "resolved_model_id": local_model,
            })
            return provider, local_model

        # -----------------------------------------------------
        # 3. Cloud Mode — ABSOLUTE MODE SEPARATION: this branch never
        # imports, calls, or falls back to anything local (no
        # complexity_router, no local_model_selector, no "local"
        # provider) — every path through it ends at a cloud provider or
        # (None, None), full stop; see the "return None, None" below,
        # which the caller (backend/websocket/handlers.py,
        # backend/server.py, backend/rest/router.py) turns into a
        # structured safety_warning rather than any kind of local
        # fallback. Only ever returns a provider that actually has a
        # configured key (backend.core.key_manager — OS secure storage
        # or an env var). The user's manually chosen provider always
        # wins outright — "Manual override persists until the user
        # switches modes again" (see mode_manager.set_cloud_provider(),
        # cleared by set_mode()). Only when nothing was ever manually
        # chosen does this pick task-type-aware ("best cloud model for
        # the task — reasoning, coding, ...", same preference
        # AutoSelector's Automatic-Model-Routing cloud branch already
        # uses, see
        # task_classifier.preferred_cloud_provider_for_task()) instead of
        # flatly defaulting to "openai" regardless of what's being asked.
        # If the preferred/requested provider isn't configured, falls
        # through AutoSelector.cloud_rank to the next configured one
        # instead of handing back a keyless provider that would only
        # fail once the real API call is made. Cloud Mode never silently
        # drops to local — if truly no cloud provider is configured at
        # all, that's surfaced as None here (see _resolve_explicit_model
        # and the safety/chat paths, which already handle a None model
        # cleanly) rather than pretending the user got what they asked for.
        # -----------------------------------------------------
        if mode == "cloud":
            manually_chosen = self.mode_manager.get_cloud_provider()
            if manually_chosen:
                requested = manually_chosen
            else:
                task_type = classify_task_type(prompt or "")
                requested = preferred_cloud_provider_for_task(task_type) or "openai"
                logger.debug(f"Mode=cloud → no manual provider chosen, task_type={task_type} → preferred={requested}")
            configured = key_manager.list_configured_providers()

            if configured.get(requested, False):
                provider_name = requested
            else:
                provider_name = next(
                    (name for name in self.auto_selector.cloud_rank if configured.get(name, False)),
                    None,
                )
                if provider_name:
                    logger.warning(
                        f"Mode=cloud → requested provider '{requested}' has no configured key; "
                        f"falling back to '{provider_name}'"
                    )
                    unified_log("provider_router", "WARNING", "Cloud mode fallback: requested provider not configured", {
                        "requested_provider": requested, "fallback_provider": provider_name,
                    })
                else:
                    logger.error(f"Mode=cloud → no cloud provider is configured (requested '{requested}')")
                    unified_log("provider_router", "ERROR", "Cloud mode: no cloud provider configured", {
                        "requested_provider": requested,
                    })
                    return None, None

            logger.debug(f"Mode=cloud → provider={provider_name}")
            provider = get_provider(provider_name)

            # Batch 2: resolve a REAL model id for this provider — before
            # this, Cloud Mode always returned (provider, None), which
            # every cloud wrapper (backend/llm/providers/*_wrapper.py)
            # then sent to the real API as `"model": None`. Falling back
            # to None here on a selection failure (rather than raising)
            # preserves the existing "still attempt the call" behavior
            # for a provider with no default/override mapping — the
            # switch-time gate (backend.core.routing_guard) is what
            # actually blocks entering Cloud Mode without a resolvable
            # model in the first place, so reaching this branch with an
            # unmapped provider should be rare.
            selection = model_selector.select_cloud_model(provider_name)
            model_id = selection.model_id if isinstance(selection, model_selector.ModelInfo) else None
            if model_id is None:
                logger.warning(f"Mode=cloud → could not resolve a model for provider '{provider_name}': {getattr(selection, 'message', '')}")

            unified_log("provider_router", "INFO", "Model selection: cloud mode", {
                "provider": provider_name, "model_id": model_id,
            })
            return provider, model_id

        # -----------------------------------------------------
        # 4. Automatic Model Routing (Copilot-style) — the default mode
        # whenever the user hasn't explicitly forced Local or Cloud Mode.
        # Chooses between ALL models (local or cloud) based on task
        # complexity, hardware limits, and provider availability — see
        # AutoSelector.select_provider().
        #
        # mode_used is which provider THIS message routed to — it must
        # NOT be written back into self.mode_manager. Doing that used to
        # collapse "automatic" into a fixed "local"/"cloud" the moment
        # the very first message resolved, so every later message
        # skipped AutoSelector entirely (mode was no longer "automatic")
        # and the per-message complexity classification never ran again
        # for the rest of this mode_manager's life. Now that
        # mode_manager.set_mode() persists to disk (see mode_manager.py),
        # that bug would have made "automatic" permanently un-selectable
        # after one message — Automatic Model Routing is supposed to
        # re-evaluate every single request.
        # -----------------------------------------------------
        logger.debug("Mode=automatic → delegating to AutoSelector")
        mode_used, provider, local_model = self.auto_selector.select_provider(
            prompt or "", history)
        logger.debug(
            f"AutoSelector result → mode_used={mode_used}, provider={provider}, model={local_model}"
        )

        # "Last Cloud Escalation" (dual-context diagnostics spec) — the
        # only place Automatic Model Routing actually decides, per
        # message, to use a cloud provider instead of local; see
        # backend.core.routing_history's own docstring for why this is a
        # plain timestamp rather than a ring buffer.
        if mode_used == "cloud":
            routing_history.record_cloud_escalation()

        unified_log("provider_router", "INFO", "Model selection: automatic model routing", {
            "mode_used": mode_used, "resolved_model_id": local_model,
        })
        return provider, local_model

    # ---------------------------------------------------------
    # Resolve explicit model_id → provider
    # ---------------------------------------------------------
    def _resolve_explicit_model(self, model_id: str):
        logger.debug(f"_resolve_explicit_model() called → {model_id}")

        # Single source of truth: backend.core.model_registry (backed by
        # backend/config/models.json, resolved via __file__). This used to
        # re-read a *different*, relative "config/models.json" path here —
        # relative to whatever the process's cwd happened to be (repo root,
        # after ws_server.py's os.chdir), which doesn't contain a
        # models.json at all. So every explicit model_id silently missed
        # and fell through to the "not found" branch below, no matter how
        # valid the id was — resolved_model_id came back None and every
        # downstream stream_token logged model_id=None.
        cfg = get_model_config(model_id)

        if cfg:
            provider_name = cfg.get("provider", "local")
            logger.debug(f"Explicit model config found → provider={provider_name}")
            provider = get_provider(provider_name)
            return provider, model_id

        logger.warning(f"Explicit model_id '{model_id}' not found in registry — falling back to default local model")
        unified_log("provider_router", "WARNING", f"Unknown model_id in explicit resolution: {model_id}", {
            "requested_model_id": model_id,
        })
        local_model = ensure_default_local_model()
        if local_model is None:
            logger.error("No local model available to fall back to.")
            unified_log("provider_router", "ERROR", "Fallback failed: no local model available", {
                "requested_model_id": model_id,
            })
        provider = get_provider("local")
        return provider, local_model
