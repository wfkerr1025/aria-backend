// core/model_truth.js
//
// Batch 2.5 — single source of truth for turning a mode_status_result
// payload into a human-readable "what model is ARIA using right now"
// description. Both webui/components/statusbar/statusbar.js (the
// bottom-left indicator) and webui/pages/models/models.js (the
// Diagnostics "Current Model" row) call this exact function so the two
// surfaces can never drift into showing different things for the same
// backend state — see backend/ipc_router.py's _handle_mode_status() for
// the payload shape this consumes: {routing_mode, cloud_provider,
// cloud_provider_display_name, active_model_id,
// active_model_display_name, location}.
//
// Deliberately pure (no DOM, no bridge calls) so it's trivial to unit
// test and to reuse from any future surface without pulling in either
// component's own rendering machinery.

// Pulls a trailing "(Q5_K_M)"-style quantization tag off a local
// model's display name, e.g. "Mistral Nemo 12B Instruct (Q5_K_M)" ->
// "Q5_K_M" — backend/core/model_registry.py's local model names already
// carry this, so no separate backend field is needed for it.
function extractQuantization(displayName) {
  if (!displayName) return null;
  const m = /\(([^()]+)\)\s*$/.exec(displayName);
  return m ? m[1] : null;
}

// Returns a plain object describing the active model — never throws,
// never returns a field that would render as "undefined"/"null" text;
// callers get an explicit `unknown: true` instead.
//
//   { unknown: false, location: "cloud"|"local"|"automatic"|null,
//     icon: "☁"|"💻"|"⚙"|"❔", modelName: string|null,
//     provider: string|null, quantization: string|null,
//     modelId: string|null, label: string }
export function describeActiveModel(status) {
  status = status || {};
  const mode = status.routing_mode || "automatic";

  if (mode === "cloud") {
    const provider = status.cloud_provider_display_name || null;
    const modelName = status.active_model_display_name || null;
    const modelId = status.active_model_id || null;

    if (!provider) {
      // Cloud Mode, but no provider chosen yet — see statusbar.js's
      // existing rationale: honest "don't know yet" beats guessing.
      return {
        unknown: true, location: "cloud", icon: "❔",
        modelName: null, provider: null, quantization: null, modelId: null,
        label: "—",
      };
    }

    if (modelName) {
      return {
        unknown: false, location: "cloud", icon: "☁",
        modelName, provider, quantization: null, modelId,
        label: `${modelName} (${provider})`,
      };
    }

    // Provider chosen, but no specific model resolved yet (e.g. no key
    // configured for it) — show the provider alone, never the bare word
    // "cloud" and never a guessed model name.
    return {
      unknown: false, location: "cloud", icon: "☁",
      modelName: null, provider, quantization: null, modelId,
      label: provider,
    };
  }

  // Local or Automatic — Automatic Model Routing's registry "active"
  // role is always a local model (see backend/core/model_registry.py),
  // it just isn't guaranteed to be what the NEXT message actually uses.
  const name = status.active_model_display_name || status.active_model_id || null;
  const modelId = status.active_model_id || null;

  if (!name) {
    return {
      unknown: true, location: mode === "automatic" ? "automatic" : "local", icon: "❔",
      modelName: null, provider: null, quantization: null, modelId: null,
      label: "—",
    };
  }

  const quantization = extractQuantization(status.active_model_display_name);

  return {
    unknown: false,
    location: mode === "automatic" ? "automatic" : "local",
    icon: mode === "automatic" ? "⚙" : "💻",
    modelName: name, provider: null, quantization, modelId,
    label: name,
  };
}
