# backend/core/model_loader.py

import os
import time
from llama_cpp import Llama

from . import model_registry
from .model_registry import get_model
from .model_path_resolver import ModelPathResolver
from .safety_manager import evaluate_safety
from .model_metadata import get_model_params, get_quant_bytes, get_quant_difficulty
from .model_size_requirements import load_cache, save_cache, compute_hash, file_fingerprint, should_recompute
from . import auto_balancer

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


class ModelLoader:
    """
    Loads GGUF models via llama.cpp with full adaptive safety:
    - Uses model metadata (params, quant)
    - Applies adaptive performance profiles
    - Estimates RAM/VRAM/CPU usage before loading
    - Warns user when thresholds are exceeded
    - Allows override for advanced users
    """

    def __init__(self):
        logger.debug("Initializing ModelLoader")
        self.active_model_id = None
        self.active_model = None
        self.resolver = ModelPathResolver()
        # What the currently-loaded Llama instance was actually built
        # with — compared against backend.core.auto_balancer's current
        # override (if any) below to decide whether the cached instance
        # is still valid or needs a reload. None for every model that
        # never has an active auto-balance session (i.e. everything
        # except an eligible local CPU-only model under load).
        self._active_threads = None
        self._active_ctx = None
        self._active_path = None
        logger.debug("ModelPathResolver initialized")

    def load_model(self, model_id: str, *, allow_override: bool = False):
        logger.debug(f"load_model() called → model_id={model_id}, allow_override={allow_override}")

        model_cfg = get_model(model_id)
        if model_cfg is None:
            logger.error(f"load_model() — unknown model ID '{model_id}'; registered ids: {model_registry.get_model_ids()}")
            unified_log("model_loader", "ERROR", f"load_model failed: unknown model_id '{model_id}'", {
                "model_id": model_id, "known_model_ids": model_registry.get_model_ids(),
            })
            raise ValueError(f"Unknown model ID: {model_id}")

        # backend.core.auto_balancer's current override for this model_id,
        # if any — a no-op dict lookup for every model that was never
        # registered as an active balance session (every cloud provider,
        # every GPU-offloaded local model, and any CPU-only local model
        # whose CPU has never crossed WARN_CPU). See auto_balancer.py's module
        # docstring for exactly why threads/context/quant can only take
        # effect starting with THIS load_model() call rather than the
        # generation currently in flight.
        overrides = auto_balancer.get_effective_overrides(model_id)

        # Reuse cached model — but only if it was already built with
        # whatever override (if any) is currently in effect. A cached
        # instance built BEFORE a throttle/restore change is stale for
        # threads/context/quant purposes and must be reloaded; this only
        # ever triggers between generations (this whole method only runs
        # when a NEW load_model() call starts), never interrupting one
        # already in progress.
        if self.active_model_id == model_id and self.active_model is not None:
            wanted_threads = overrides.get("n_threads") if overrides else None
            wanted_ctx = overrides.get("max_ctx") if overrides else None
            wanted_path = overrides.get("model_path") if overrides else None
            stale = (
                (wanted_threads is not None and wanted_threads != self._active_threads)
                or (wanted_ctx is not None and wanted_ctx != self._active_ctx)
                or (wanted_path is not None and wanted_path != self._active_path)
                or (wanted_path is None and self._active_path is not None and self._active_path != self.resolver.resolve_model_path(model_id))
            )
            if not stale:
                logger.debug(f"Reusing cached model → {model_id}")
                return self.active_model
            logger.info(f"load_model() → auto-balancer override changed for {model_id}, forcing reload")
            unified_log("model_loader", "INFO", "Reloading model for auto-balancer override", {"model_id": model_id, "overrides": overrides})

        # Evaluate safety BEFORE loading llama.cpp
        decision = evaluate_safety(model_cfg)
        logger.debug(f"Safety decision → requires_warning={decision.requires_warning}")

        if decision.requires_warning and not allow_override:
            logger.debug("Safety warning triggered → aborting load")
            raise RuntimeError(
                f"SAFETY WARNING:\n\n{decision.message}\n\n"
                "To proceed anyway, call load_model(..., allow_override=True)."
            )

        # Unload previous model
        if self.active_model is not None:
            logger.debug(f"Unloading previous model → {self.active_model_id}")
        self.unload_model()

        # Load cached metadata (params/quant/context) for this model —
        # populated by the splash-screen startup pipeline
        # (backend/core/init_pipeline.py); computed on-demand and cached
        # here if that pipeline hasn't seen this model yet (e.g. one
        # added to the catalog after the last startup pass ran).
        self._get_cached_metadata(model_id, model_cfg)

        # Resolve GGUF path — an active quant-tier auto-balance override
        # points at a genuinely different file (a lower-quant sibling
        # already confirmed to exist on disk; see
        # auto_balancer._find_lower_quant_sibling()), not the model's own
        # configured path.
        model_path = (overrides or {}).get("model_path") or self.resolver.resolve_model_path(model_id)
        logger.debug(f"Resolved model path → {model_path}")

        if model_path is None:
            logger.debug("ERROR: Model file not found")
            raise FileNotFoundError(
                f"Model file not found for {model_id}. "
                "UI must prompt user to select the GGUF file."
            )

        # Use adaptive profile settings, with any active auto-balancer
        # override applied on top — threads/context only, never gpu_layers
        # (auto-balancing is scoped to CPU-only inference; see
        # auto_balancer.is_eligible()).
        profile = decision.profile
        if overrides:
            if "n_threads" in overrides:
                profile.n_threads = overrides["n_threads"]
            if "max_ctx" in overrides:
                profile.max_ctx = overrides["max_ctx"]

        logger.debug(
            f"Adaptive profile → ctx={profile.max_ctx}, "
            f"gpu_layers={profile.n_gpu_layers}, threads={profile.n_threads}"
        )

        # Load GGUF model with llama.cpp
        logger.debug("Loading GGUF model via llama.cpp")
        self.active_model = Llama(
            model_path=model_path,
            n_ctx=profile.max_ctx,
            n_gpu_layers=profile.n_gpu_layers,
            n_threads=profile.n_threads,
            verbose=False,
        )

        self.active_model_id = model_id
        self._active_threads = profile.n_threads
        self._active_ctx = profile.max_ctx
        self._active_path = model_path
        logger.debug(f"Model loaded successfully → {model_id}")

        return self.active_model

    def _get_cached_metadata(self, model_id: str, model_cfg: dict) -> dict:
        """
        Look up this model's cached GGUF metadata (see
        backend/core/init_pipeline.py, which populates the same cache
        file from the splash-screen startup pipeline). On a miss —
        wrong/stale hash, or a model the startup pass never saw —
        computes it on-demand and writes it back so the next load is a
        cache hit.
        """
        cache = load_cache()
        cached_entry = cache.get("models", {}).get(model_id)
        file_path = model_cfg.get("path")

        if file_path and not should_recompute(file_path, cached_entry):
            logger.debug(f"_get_cached_metadata() → cache hit for model_id={model_id}")
            return cached_entry

        logger.debug(f"_get_cached_metadata() → cache miss for model_id={model_id}, computing on-demand")
        metadata = {
            "hash": compute_hash(file_path) if file_path else None,
            "fingerprint": file_fingerprint(file_path) if file_path else None,
            "params": get_model_params(model_cfg),
            "quant_bytes": get_quant_bytes(model_cfg),
            "quant_difficulty": get_quant_difficulty(model_cfg),
            "context": model_cfg.get("maxContext"),
            "requirements": model_cfg.get("requirements") or {},
            "timestamp": int(time.time()),
        }
        cache.setdefault("models", {})[model_id] = metadata
        save_cache(cache)
        return metadata

    def unload_model(self):
        if self.active_model_id:
            logger.debug(f"Unloading model → {self.active_model_id}")
        self.active_model = None
        self.active_model_id = None
        self._active_threads = None
        self._active_ctx = None
        self._active_path = None

    # Exposed for backend.core.self_knowledge — the SKR's ground truth
    # for "what model is actually loaded right now" in this loader
    # instance, as opposed to what a request merely asked for.
    def get_active_model_id(self) -> str | None:
        return self.active_model_id

    # ---------------------------------------------------------
    # Model role getters/setters — active/fallback/emergency.
    #
    # These delegate to backend.core.model_registry, which is the single
    # source of truth for role configuration (persisted to
    # backend/config/models.json), rather than tracking a second, competing
    # copy of "which model is active" here. get_active_model_id() above is
    # a different, narrower concept — "what this specific loader instance
    # currently has loaded in memory" — kept distinct on purpose so the two
    # don't get conflated: the configured active model and the model
    # actually resident in llama.cpp right now can briefly differ (e.g.
    # immediately after a role change, before the next request loads it).
    # ---------------------------------------------------------
    def get_configured_active_model_id(self) -> str | None:
        return model_registry.get_active_model_id()

    def set_configured_active_model_id(self, model_id: str) -> bool:
        return model_registry.set_active_model_id(model_id)

    def get_fallback_model_id(self) -> str | None:
        return model_registry.get_fallback_model_id()

    def set_fallback_model_id(self, model_id: str) -> bool:
        return model_registry.set_fallback_model_id(model_id)

    def get_emergency_model_id(self) -> str | None:
        return model_registry.get_emergency_model_id()

    def set_emergency_model_id(self, model_id: str) -> bool:
        return model_registry.set_emergency_model_id(model_id)

    # Non-streaming inference
    def run(self, prompt: str, *, max_tokens: int, temperature: float, stop: list[str] | None = None) -> str:
        logger.debug(
            f"run() called → prompt_len={len(prompt)}, max_tokens={max_tokens}, temp={temperature}, stop={stop}"
        )

        if self.active_model is None:
            logger.debug("ERROR: No model loaded")
            raise RuntimeError("No model loaded. Call load_model() first.")

        result = self.active_model(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
            stop=stop or [],
        )

        text = result["choices"][0]["text"]
        logger.debug(f"run() complete → output_len={len(text)}")
        return text

    # Streaming inference
    def run_stream(self, prompt: str, *, max_tokens: int, temperature: float, stop: list[str] | None = None):
        logger.debug(
            f"run_stream() called → prompt_len={len(prompt)}, max_tokens={max_tokens}, temp={temperature}, stop={stop}"
        )

        if self.active_model is None:
            logger.debug("ERROR: No model loaded")
            raise RuntimeError("No model loaded. Call load_model() first.")

        for chunk in self.active_model(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            stop=stop or [],
        ):
            text = chunk["choices"][0]["text"]
            logger.debug(f"Streaming chunk → len={len(text)}")
            yield text
