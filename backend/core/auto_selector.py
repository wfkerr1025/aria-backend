# backend/core/auto_selector.py

from __future__ import annotations

from backend.llm.providers.provider_registry import get_provider
from backend.core.task_classifier import classify_task_complexity, classify_task_type, preferred_cloud_provider_for_task
from backend.core.success_predictor import should_use_local
from backend.core.local_model_selector import ensure_default_local_model
from backend.core.complexity_router import prompt_carries_evidence, select_local_model_for_prompt
from backend.core.evidence_routing import model_can_synthesize_evidence
from backend.core import key_manager
from backend.core import model_selector

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


class AutoSelector:
    """
    Automatic Model Routing engine (Copilot-style): the unified selector
    that chooses between ALL models — local or cloud — based on task
    complexity, hardware limits, and provider availability. This is what
    runs whenever the user hasn't explicitly forced Local or Cloud Mode.
    Steps:
        1. Compute complexity
        2. Compute task type
        3. Compute success probability
        4. If success >= threshold → local
        5. If success < threshold → cloud
        6. Choose best cloud provider
        7. If chosen cloud provider unavailable → next-best cloud provider
        8. If no cloud available → fallback to local
    """

    def __init__(self):
        logger.debug("AutoSelector.__init__() called")

        # FIXED: "groq" was never a real registered provider — the
        # actual wrapper (backend/llm/providers/grok_wrapper.py) loads
        # as "grok" (provider_registry.py derives the name from the
        # filename), so this entry could never match and the real grok
        # provider was silently unreachable from Automatic Model
        # Routing's fallback ranking. "azure" was missing entirely. Both fixed; every name
        # here now matches backend.core.key_manager.PROVIDER_ENV_VARS.
        self.cloud_rank = [
            "openai",
            "anthropic",
            "deepseek",
            "grok",
            "mistral",
            "gemini",
            "cohere",
            "together",
            "openrouter",
            "huggingface",
            "replicate",
            "perplexity",
            "azure",
            "custom_http",
        ]

        logger.debug(f"Cloud provider ranking loaded ({len(self.cloud_rank)} providers)")

    # ---------------------------------------------------------
    # Main selection logic
    # ---------------------------------------------------------
    def select_provider(self, prompt: str, history=()):
        logger.debug(f"select_provider() called with prompt length={len(prompt)}")

        # Step 1: Complexity
        complexity = classify_task_complexity(prompt)
        logger.debug(f"Task complexity computed → {complexity}")

        # Step 2: Task type
        task_type = classify_task_type(prompt)
        logger.debug(f"Task type computed → {task_type}")

        # Step 3: Local success prediction
        local_capabilities = {}  # future metadata hook
        use_local = should_use_local(prompt, local_capabilities)
        logger.debug(f"Local success prediction → {use_local}")

        # Step 4: Best local model for THIS prompt's complexity — not a
        # single sticky "default" model (see backend.core.complexity_router
        # for the ladder/hardware-gate logic). Falls back to
        # ensure_default_local_model() only if the registry has no
        # usable local model at all.
        active_local_model = (select_local_model_for_prompt(prompt, history=history)
                              or ensure_default_local_model())
        logger.debug(f"Active local model → {active_local_model}")

        local_provider = get_provider("local") if active_local_model else None

        # -----------------------------------------------------
        # Evidence turns prefer local, when local can be trusted with
        # evidence at all.
        #
        # should_use_local scores a prompt on length and complexity, and
        # an evidence-bearing prompt is long and complex BECAUSE it is
        # carrying evidence -- not because the question is hard.
        # Measured on the live "Taco Bell's newest menu item?" turn:
        #
        #     bare question               29 chars   score 100  -> local
        #     same question + evidence  5609 chars   score  40  -> cloud
        #
        # threshold is 75, so every search turn escalated to cloud once
        # the lookup succeeded, however capable the installed local
        # models were. The better the search, the more certain the
        # escalation.
        #
        # complexity_router already makes exactly this correction one
        # layer down -- "evidence present; ignoring the length heuristic"
        # -- and picks the strongest installed model at or above the
        # evidence floor, stepping down on hardware pressure and never
        # below the floor. Nobody had told the success predictor the same
        # thing, so its verdict was reached before that ladder was ever
        # consulted.
        #
        # Two conditions, both required. The prompt must actually carry
        # evidence, and the model the ladder chose must be one the
        # allowlist trusts to read it: preferring local is only an
        # improvement if the local model can do the job. When it cannot,
        # this does nothing and the cloud branch below runs exactly as
        # before.
        #
        # Nothing here loads a model, pins one past the safety gate, or
        # touches the evidence floor. It changes which branch is taken,
        # and the branch it takes is the one that already knows how to
        # choose safely.
        if not use_local and local_provider and prompt_carries_evidence(prompt):
            if model_can_synthesize_evidence(active_local_model):
                logger.info(
                    "select_provider() -> evidence-bearing prompt and %s is trusted "
                    "with evidence; preferring local over cloud",
                    active_local_model,
                )
                unified_log("auto_selector", "INFO",
                            "Automatic Model Routing: local preferred for evidence", {
                                "model_id": active_local_model,
                                "predicted_score_said": "cloud",
                            })
                use_local = True
            else:
                logger.info(
                    "select_provider() -> evidence-bearing prompt but %s is not trusted "
                    "with evidence; leaving the cloud decision alone",
                    active_local_model,
                )

        # -----------------------------------------------------
        # Local preferred
        # -----------------------------------------------------
        if use_local and local_provider:
            logger.debug("Decision → LOCAL provider selected")
            unified_log("auto_selector", "INFO", "Automatic Model Routing decision: local", {
                "complexity": complexity, "task_type": task_type, "model_id": active_local_model,
            })
            return "local", local_provider, active_local_model

        # -----------------------------------------------------
        # Cloud fallback
        #
        # "Available" means has a configured key (key_manager.
        # list_configured_providers() — OS secure storage or an env
        # var), checked here rather than via each Provider's own
        # is_available(). Only 3 of the 13 wrappers (openai, azure,
        # grok) actually implement that method; the rest fell through
        # `getattr(provider, "is_available", lambda: True)()`'s default
        # of True regardless of whether a key existed — Automatic Model
        # Routing would pick a keyless provider and only discover it doesn't work when
        # the real API call fails, instead of skipping straight to the
        # next-ranked provider or local.
        # -----------------------------------------------------
        logger.debug("Local not selected → checking cloud providers")
        configured = key_manager.list_configured_providers()

        # Task-aware preference: Anthropic tends to be the stronger
        # choice for reasoning-heavy prompts, OpenAI for code/structured
        # tasks — try the task-appropriate provider first (only if it's
        # actually configured), then fall through to the normal
        # cloud_rank order for anything else or if the preferred one
        # isn't available.
        preferred = preferred_cloud_provider_for_task(task_type)
        ordered_rank = (
            [preferred] + [n for n in self.cloud_rank if n != preferred]
            if preferred and configured.get(preferred, False)
            else self.cloud_rank
        )

        for name in ordered_rank:
            provider = get_provider(name)
            if provider and configured.get(name, False):
                logger.debug(f"Cloud provider selected → {name}")
                # Batch 2: resolve a real model id for this provider —
                # same fix as provider_router.py's dedicated Cloud Mode
                # branch. A selection failure here (no default/override
                # mapping) still returns the provider with model_id=None
                # rather than skipping to the next-ranked provider —
                # this is Automatic Model Routing's cloud FALLBACK path,
                # not a user-initiated switch, so it stays permissive.
                selection = model_selector.select_cloud_model(name)
                model_id = selection.model_id if isinstance(selection, model_selector.ModelInfo) else None
                unified_log("auto_selector", "INFO", "Automatic Model Routing decision: cloud", {
                    "complexity": complexity, "task_type": task_type, "provider": name,
                    "task_preferred": name == preferred, "model_id": model_id,
                })
                return "cloud", provider, model_id

        # -----------------------------------------------------
        # Final fallback → local
        # -----------------------------------------------------
        if local_provider:
            logger.debug("No cloud available → fallback to LOCAL provider")
            unified_log("auto_selector", "INFO", "Automatic Model Routing decision: local (cloud fallback)", {
                "complexity": complexity, "task_type": task_type, "model_id": active_local_model,
            })
            return "local", local_provider, active_local_model

        # -----------------------------------------------------
        # Nothing available
        # -----------------------------------------------------
        logger.debug("No providers available → returning NONE")
        unified_log("auto_selector", "WARNING", "Automatic Model Routing decision: no provider available", {
            "complexity": complexity, "task_type": task_type,
        })
        return "none", None, None
