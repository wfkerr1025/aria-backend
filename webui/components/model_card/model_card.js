// components/model_card/model_card.js
//
// The ONE model-card builder used everywhere a model is listed as a
// card: webui/pages/models/models.js's Installed/Available grids, and
// webui/pages/local_models/local_models.js's Settings -> Local Models
// grid. Previously each page had its own near-duplicate card markup
// (models.js's .model-card vs local_models.js's .local-model-card),
// styled from two different, drifting stylesheets — "the same card
// component" means one builder producing one markup shape, styled by
// the one shared components/model_card/model_card.css, not just
// visually-similar CSS maintained twice.
//
// The large-model Download catalog (models.js's renderCatalog()) is
// NOT built through this — its entries have a genuinely different
// shape ({id, name, sizeClass, downloadUrl, ...}, no model_cfg/compat/
// installed) and no per-model actions beyond a download link, so
// forcing it through this builder would mean a pile of "if this is a
// catalog entry" branches here for no real benefit. It still uses the
// same .model-card/.model-card-header/.model-card-badge CSS classes
// directly, which is what actually makes it look uniform.

function modelCardLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ModelCard", msg);
    }
  } catch (err) {
    console.error("[ModelCard LOG ERROR]", err);
  }
}

function buildActionButton(label, className, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
  btn.textContent = label;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick();
  });
  return btn;
}

/**
 * Build one model card element.
 *
 * @param {object} entry - one entry from models_list_result's payload:
 *   {model_cfg, installed, is_active, is_fallback, is_emergency, compat, projected_speed_toksec}
 * @param {object} [opts]
 * @param {string|null} [opts.selectedModelId] - if this matches entry.model_cfg.id, the card gets the "selected" class.
 * @param {object} [opts.actions] - any subset of:
 *   onSelect(modelId)      - clicking the card or its "Details" button
 *   onSetActive(modelId)   - "Set as Main" button (only shown if !is_active)
 *   onSetFallback(modelId) - "Set as Fallback" button (only shown if !is_fallback)
 *   onSetEmergency(modelId)- "Set as Emergency" button (only shown if !is_emergency)
 *   onUninstall(modelId)   - "Uninstall" button (installed models only)
 *   onInstall(modelId)     - "Install" button (not-installed models only)
 *   Only actions actually provided render a button — a page that
 *   doesn't want a given action just omits it.
 * @param {string[]} [opts.extraStats] - extra lines appended to the
 *   stats block after tok/sec and RAM — e.g. webui/pages/local_models/
 *   local_models.js's estimated on-disk size, which is specific to that
 *   page's disk-management purpose and has no equivalent on the Models
 *   page. Using the same shared card for both pages must not mean
 *   losing information a page already showed; it means one component
 *   both pages configure, not a lowest-common-denominator reskin.
 */
export function buildModelCard(entry, opts = {}) {
  const { selectedModelId = null, actions = {}, extraStats = [] } = opts;
  const {
    model_cfg: cfg, installed, is_active, is_fallback, is_emergency,
    compat, projected_speed_toksec: speed,
  } = entry;

  const req = cfg.requirements || {};
  const difficulty = req.difficulty || "Unknown";
  // "Very Heavy" -> "very-heavy": a raw lowercase("Very Heavy") used
  // straight in a class attribute becomes TWO class tokens (the space
  // is a separator) — neither matches any CSS rule, so that tier
  // silently rendered with no difficulty color at all. Every
  // difficulty string this app produces (backend.core.
  // model_size_requirements, backend.core.model_discovery) is single-
  // or two-word, so a plain whitespace-to-hyphen slug is enough.
  const difficultySlug = difficulty.toLowerCase().replace(/\s+/g, "-");
  const arch = cfg.arch ? ` · ${cfg.arch}` : "";

  const card = document.createElement("div");
  card.className = "model-card" + (selectedModelId === cfg.id ? " selected" : "");
  card.dataset.modelId = cfg.id;

  const badges = [];
  if (compat) {
    badges.push(`<span class="model-card-badge ${compat.meets_minimum ? "pass" : "fail"}" title="Meets Minimum Requirements">${compat.meets_minimum ? "✔" : "✘"} Min</span>`);
    badges.push(`<span class="model-card-badge ${compat.meets_recommended ? "pass" : "warn"}" title="Meets Recommended Requirements">${compat.meets_recommended ? "✔" : "✘"} Rec</span>`);
  }
  if (is_active) badges.push('<span class="model-card-badge active">★ Main Model</span>');
  if (is_fallback) badges.push('<span class="model-card-badge fallback">◆ Fallback Model</span>');
  if (is_emergency) badges.push('<span class="model-card-badge emergency">▲ Emergency Model</span>');

  const warningLines = [];
  if (compat && compat.meets_minimum === false) {
    warningLines.push("Does not meet minimum requirements on this machine.");
  } else if (compat && compat.meets_recommended === false) {
    warningLines.push("Below recommended requirements — may run slowly.");
  }

  card.innerHTML = `
    <div class="model-card-header">
      <span class="model-card-name">${cfg.name}</span>
      <span class="model-card-difficulty model-card-difficulty-${difficultySlug}">${difficulty}</span>
    </div>

    <div class="model-card-badges">${badges.join("")}</div>

    <div class="model-card-stats">
      ${speed != null ? `<span title="Estimated tokens/sec on this machine">${speed.toFixed(1)} tok/sec</span>` : ""}
      <span title="RAM requirement">RAM min ${req.minRamGB ?? "—"} / rec ${req.recRamGB ?? "—"} GB${arch}</span>
      ${extraStats.map((line) => `<span>${line}</span>`).join("")}
    </div>

    ${warningLines.length ? `<div class="model-card-warning">⚠ ${warningLines.join(" ")}</div>` : ""}
  `;

  // Built as a real appended node rather than a second `<div
  // class="model-card-actions">` inside the innerHTML template above +
  // querySelector() to find it — one fewer round trip through string
  // parsing, and it means this builder never depends on querySelector
  // support at all (a real browser's innerHTML setter parses fine, but
  // there's no reason to require that when appendChild works
  // everywhere, including in minimal test-only DOM stubs).
  const actionsEl = document.createElement("div");
  actionsEl.className = "model-card-actions";
  card.appendChild(actionsEl);

  if (actions.onSelect) {
    actionsEl.appendChild(buildActionButton("Details", "btn btn-secondary", () => actions.onSelect(cfg.id)));
  }
  if (installed) {
    if (!is_active && actions.onSetActive) {
      actionsEl.appendChild(buildActionButton("Set as Main", "btn btn-primary", () => actions.onSetActive(cfg.id)));
    }
    if (!is_fallback && actions.onSetFallback) {
      actionsEl.appendChild(buildActionButton("Set as Fallback", "btn btn-secondary", () => actions.onSetFallback(cfg.id)));
    }
    if (!is_emergency && actions.onSetEmergency) {
      actionsEl.appendChild(buildActionButton("Set as Emergency", "btn btn-secondary", () => actions.onSetEmergency(cfg.id)));
    }
    if (actions.onUninstall) {
      actionsEl.appendChild(buildActionButton("Uninstall", "btn btn-danger", () => actions.onUninstall(cfg.id)));
    }
  } else if (actions.onInstall) {
    actionsEl.appendChild(buildActionButton("Install", "btn btn-primary", () => actions.onInstall(cfg.id)));
  }

  if (actions.onSelect) {
    card.addEventListener("click", () => actions.onSelect(cfg.id));
  }

  modelCardLog("Built card for model_id=" + cfg.id);
  return card;
}
