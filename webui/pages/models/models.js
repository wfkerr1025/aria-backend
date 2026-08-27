import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import { ModelDetails } from "../../components/model_details/model_details.js";
import { ModelRequirements } from "../../components/model_requirements/model_requirements.js";
import { buildModelCard } from "../../components/model_card/model_card.js";
import { describeActiveModel } from "../../core/model_truth.js";

function modelsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ModelsPage", msg);
    }
  } catch (err) {
    console.error("[ModelsPage LOG ERROR]", err);
  }
}

modelsLog("=== MODELS PAGE MODULE LOADED ===");

// Full provider list for the key-management cards below. Module keys
// used to have an equivalent hardcoded list + card grid here too — moved
// to webui/pages/modules/modules.js as part of splitting Settings into a
// Steam-style hub with dedicated subpages; the Models page no longer
// manages module keys at all.
const PROVIDERS = [
  "openai", "anthropic", "gemini", "grok", "cohere", "perplexity",
  "together", "replicate", "huggingface", "mistral", "deepseek",
  "azure", "custom_http", "openrouter",
];

// Full model switcher/installer page. Lists every registered model
// (installed + available), and delegates to the systems that already
// exist rather than re-implementing them:
//   - model_manager.list_models()      → combined install/active/compat/speed
//   - ModelRequirements (pre-install popup + Steam-style gating)
//   - ModelDetails (requirements + compat + performance, in one panel)
const Models = {
  models: [],
  // Persists across navigation — Models is a module-level singleton, so
  // this survives router.js destroying and recreating the page's DOM.
  // The bug this used to enable ("Details panel goes blank after
  // navigating away and back") was never this value being lost; it was
  // mountDetails()/ModelDetails.init() refusing to re-populate the
  // fresh, empty DOM because of a one-time "already mounted" flag. See
  // mountDetails() and renderModelDetails() below.
  selectedModelId: null,

  async init() {
    modelsLog("Models.init() called.");

    this.cache();
    this.bindUI();
    // Window-level listener bound ONCE ever, not once per visit — see
    // diagnostics.js's Diagnostics.init() for the full rationale
    // (router.js rebuilds this page's own DOM every visit, but never
    // touches `window`, so re-binding here would stack up duplicate
    // permanent listeners and fire every handler N times per packet).
    if (!this._bridgeBound) {
      this.bindBridge();
      this._bridgeBound = true;
    }
    this.buildProviderCards();

    await this.mountDetails();
    this.requestList();
    this.requestModeStatus();
    this.requestDiagnosticsModels();
    this.requestCatalog();

    // FIXED: this used to ALSO fire one more providers_list_request per
    // provider (14x), on top of the ONE providers_list_request
    // requestList() already sends above — redundant, identical in-flight
    // requests on every single page load. Each returns a FULL snapshot
    // of every provider's status, so whichever response happened to
    // arrive LAST won, regardless of which was actually most recent — if
    // the user saved a key while any of those 14 earlier, now-stale
    // requests were still in flight, its late-arriving "not configured"
    // answer could overwrite the correct "configured: true" one that had
    // just arrived from the save's own refresh. This is what actually
    // produced "I entered a key and it still shows Not configured" —
    // not a persistence bug (the backend round-trip is correct and
    // instant), a stale-response race purely from firing far more
    // requests than needed. One request per resource is enough; every
    // provider's status already comes back in that single response (see
    // backend/ipc_router.py's providers_list_request handler).

    modelsLog("Models page ready.");
  },

  cache() {
    this.installedGrid = document.getElementById("models-installed-grid");
    this.availableGrid = document.getElementById("models-available-grid");
    this.providerCardsContainer = document.getElementById("provider-cards-container");
    this.refreshBtn = document.getElementById("models-refresh-btn");
    this.detailsMount = document.getElementById("models-details-mount");
    this.detailsPlaceholder = document.getElementById("models-details-placeholder");
    this.diagnosticsGrid = document.getElementById("models-diagnostics-grid");
    this.catalogGrid = document.getElementById("models-catalog-grid");

    modelsLog(
      "CACHE RESULT: " +
        JSON.stringify({
          installedGrid: !!this.installedGrid,
          availableGrid: !!this.availableGrid,
          detailsMount: !!this.detailsMount,
        })
    );
  },

  bindUI() {
    this.refreshBtn?.addEventListener("click", () => {
      modelsLog("Manual refresh requested.");
      this.requestList();
    });
  },

  bindBridge() {
    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      if (packet.type === IPC.MODELS_LIST_RESULT) {
        modelsLog("Received models_list_result → " + (packet.payload?.models?.length ?? 0) + " models.");
        this.models = packet.payload?.models || [];
        this.render();
      }

      if (packet.type === IPC.PROVIDERS_LIST_RESULT) {
        const providers = packet.payload?.providers || [];
        modelsLog("Received providers_list_result → " + providers.length + " providers.");
        providers.forEach((p) => this.setProviderStatusBadge(p.name, p.configured));
      }

      if (packet.type === IPC.PROVIDER_KEY_SET_RESULT) {
        this.handleKeyActionResult(packet.payload, "provider", "API key saved successfully.", () =>
          this.refreshProviderStatus(packet.payload?.provider)
        );
      }

      if (packet.type === IPC.PROVIDER_KEY_DELETE_RESULT) {
        this.handleKeyActionResult(packet.payload, "provider", "Key removed.", () =>
          this.refreshProviderStatus(packet.payload?.provider)
        );
      }

      if (packet.type === IPC.MODE_STATUS_RESULT) {
        modelsLog("Received mode_status_result → " + JSON.stringify(packet.payload));
        this.renderModeStatus(packet.payload || {});
      }

      if (packet.type === IPC.DIAGNOSTICS_MODELS_RESULT) {
        modelsLog("Received diagnostics_models_result.");
        this.renderDiagnostics(packet.payload || {});
      }

      if (packet.type === IPC.MODEL_CATALOG_RESULT) {
        modelsLog("Received model_catalog_result → " + (packet.payload?.entries?.length ?? 0) + " entries.");
        this.renderCatalog(packet.payload?.entries || []);
      }

      if (packet.type === IPC.MODEL_UNINSTALL_RESULT) {
        this.handleActionResult(packet.payload, "Model uninstalled.", "Uninstall failed.");
      }

      if (packet.type === IPC.MODEL_SET_ACTIVE_RESULT) {
        this.handleActionResult(packet.payload, "Active model updated.", "Could not set active model.");
      }

      // Fired by a chat-driven "switch to X mode" / "use <provider>"
      // (see backend/websocket/handlers.py's _handle_model_switch_directly)
      // — not triggered from this page's own UI, but the mode bar should
      // reflect it immediately if the Models page happens to be open.
      if (packet.type === IPC.MODE_SET_RESULT) {
        this.requestModeStatus();
      }

      if (packet.type === IPC.MODEL_SET_FALLBACK_RESULT) {
        this.handleActionResult(packet.payload, "Fallback model updated.", "Could not set fallback model.");
      }

      if (packet.type === IPC.MODEL_SET_EMERGENCY_RESULT) {
        this.handleActionResult(packet.payload, "Emergency model updated.", "Could not set emergency model.");
      }

      if (packet.type === IPC.MODEL_INSTALL_RESULT) {
        this.handleActionResult(packet.payload, "Model installed.", "Install failed.");
      }
    });
  },

  handleActionResult(payload, okMessage, failMessage) {
    const ok = !!payload?.ok;
    const text = ok ? okMessage : (payload?.reason || failMessage);

    modelsLog(`Action result → ok=${ok}, message="${text}"`);

    if (typeof window.showToast === "function") {
      window.showToast(text, ok ? "success" : "error");
    }

    this.requestList();
    this.requestModeStatus();
  },

  // Re-fetches + re-inserts model_details.html on EVERY init() call —
  // there used to be a `detailsMounted` flag that skipped this after
  // the very first page load, which was the actual cause of "Details
  // panel goes blank after navigating away and back": router.js
  // replaces #panel-container's entire innerHTML on every navigation,
  // so #models-details-mount is a brand-new, empty div each time this
  // runs — skipping the re-insert left it permanently empty from the
  // second visit onward, even though selectedModelId (above) was still
  // correctly remembered the whole time.
  async mountDetails() {
    if (!this.detailsMount) return;

    try {
      const html = await fetch("components/model_details/model_details.html").then((r) => r.text());
      this.detailsMount.insertAdjacentHTML("beforeend", html);
      await ModelDetails.init();
      modelsLog("ModelDetails mounted into Models page.");

      // Rehydrate: if a model was already selected on a PREVIOUS visit,
      // render it immediately instead of leaving the freshly-mounted
      // panel on its "Select a model…" placeholder until the user
      // clicks a card again — the whole point of remembering
      // selectedModelId across navigation.
      if (this.selectedModelId) {
        this.renderModelDetails(this.selectedModelId);
      }
    } catch (err) {
      modelsLog("ERROR mounting ModelDetails: " + err);
    }
  },

  // Stable entry point for "show this model's Details panel" — the
  // ONLY place that hides the placeholder and asks ModelDetails to load
  // a model, so the initial click path (selectModel()) and the
  // rehydrate-after-navigation path (mountDetails() above) can never
  // drift out of sync with each other.
  renderModelDetails(modelId) {
    modelsLog("renderModelDetails() → " + modelId);
    this.selectedModelId = modelId;
    this.detailsPlaceholder?.classList.add("hidden");
    ModelDetails.loadModel(modelId);
  },

  // -------------------------------------------------------
  // BACKEND REQUESTS
  // -------------------------------------------------------

  requestList() {
    modelsLog("requestList() → models_list_request");
    bridge.send(IPC.MODELS_LIST_REQUEST, {});
    bridge.send(IPC.PROVIDERS_LIST_REQUEST, {});
  },

  requestModeStatus() {
    modelsLog("requestModeStatus() → mode_status_request");
    bridge.send(IPC.MODE_STATUS_REQUEST, {});
  },

  // Mode/active-model/override indicator chips (Phase 3.3) — real
  // backend state (backend.core.mode_manager), not a static label.
  renderModeStatus(status) {
    // Batch 2.5 — cached so renderDiagnostics() below can build the
    // "Current Model" row from the SAME truth source as the status bar
    // (webui/components/statusbar/statusbar.js), regardless of which of
    // mode_status_result / diagnostics_models_result happens to arrive
    // first (diagnostics_models_request and mode_status_request are
    // both fired from init(), in that order, but responses race).
    this._lastModeStatus = status;
    this.renderDiagnostics(this._lastDiagnosticsData || {});

    const modeChip = document.getElementById("models-mode-chip");
    const activeChip = document.getElementById("models-active-model-chip");
    const overrideChip = document.getElementById("models-override-chip");
    if (!modeChip || !activeChip || !overrideChip) return;

    const mode = status.routing_mode || "automatic";
    modeChip.textContent = "Mode: " + mode.charAt(0).toUpperCase() + mode.slice(1);

    activeChip.textContent = "Active: " + (status.active_model_id || "none");

    if (status.explicit_model_override) {
      overrideChip.textContent = "Pinned: " + status.explicit_model_override;
      overrideChip.style.display = "";
    } else {
      overrideChip.style.display = "none";
    }
  },

  // -------------------------------------------------------
  // Dual-context UI's Diagnostics block (spec 3C) — Active Chat Model /
  // System Fallback Model / Last Local Escalation / Last Cloud
  // Escalation / Highest Available Local Model.
  // -------------------------------------------------------
  requestDiagnosticsModels() {
    modelsLog("requestDiagnosticsModels() → diagnostics_models_request");
    bridge.send(IPC.DIAGNOSTICS_MODELS_REQUEST, {});
  },

  renderDiagnostics(data) {
    this._lastDiagnosticsData = data;
    if (!this.diagnosticsGrid) return;

    const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "Never this session");

    // Batch 2.5 — "Current Model" panel, mode-aware and built from the
    // exact same truth source as the status bar (core/model_truth.js +
    // mode_status_result, cached in renderModeStatus() above), not
    // diagnostics_models_result's raw active_chat_model_id alone — that
    // field carries no provider/location/quantization info.
    const desc = describeActiveModel(this._lastModeStatus || {});
    let currentModelValue;
    if (desc.unknown) {
      currentModelValue = "—";
    } else if (desc.location === "cloud") {
      currentModelValue = desc.modelName
        ? `☁ ${desc.modelName} — ${desc.provider} (Cloud)`
        : `☁ ${desc.provider} (Cloud)`;
    } else if (desc.location === "automatic") {
      currentModelValue = `⚙ ${desc.modelName} (Automatic)`;
    } else {
      currentModelValue = desc.quantization
        ? `💻 ${desc.modelName} — ${desc.quantization} (Local)`
        : `💻 ${desc.modelName} (Local)`;
    }

    const rows = [
      ["Current Model", currentModelValue],
      ["Active Chat Model", data.active_chat_model_id || "—"],
      ["System Fallback Model", data.system_fallback_model_id || "—"],
      ["Emergency Model", data.emergency_model_id || "—"],
      ["Last Local Escalation", fmtTime(data.last_local_escalation)],
      ["Last Cloud Escalation", fmtTime(data.last_cloud_escalation)],
      ["Highest Available Local Model", data.highest_available_local_model_id || "—"],
    ];

    this.diagnosticsGrid.innerHTML = rows.map(([label, value]) => `
      <div class="models-diagnostics-item">
        <div class="models-diagnostics-label">${label}</div>
        <div class="models-diagnostics-value">${value}</div>
      </div>
    `).join("");

    modelsLog("renderDiagnostics() complete.");
  },

  // -------------------------------------------------------
  // Model installation safety — Tier 0-6 catalog of large models not
  // bundled with ARIA Lite (spec 4D). See backend.core.model_catalog.
  // -------------------------------------------------------
  requestCatalog() {
    modelsLog("requestCatalog() → model_catalog_request");
    bridge.send(IPC.MODEL_CATALOG_REQUEST, {});
  },

  renderCatalog(entries) {
    if (!this.catalogGrid) return;

    if (!entries.length) {
      this.catalogGrid.innerHTML = `<p class="models-empty">No catalog entries configured.</p>`;
      return;
    }

    this.catalogGrid.innerHTML = entries.map((entry) => {
      const disabledClass = entry.supported ? "" : " model-card-disabled";
      const link = entry.downloadUrl
        ? `<a href="${entry.downloadUrl}" class="btn btn-secondary" target="_blank" rel="noopener">Get model</a>`
        : `<span class="models-catalog-no-link">Download link not yet configured</span>`;

      return `
        <div class="model-card${disabledClass}">
          <div class="model-card-header">
            <span class="model-card-name">${entry.name}</span>
            <span class="model-card-badge">${entry.sizeClass}</span>
          </div>
          <div class="models-catalog-warning">${entry.compatibilityWarning || ""}</div>
          <div class="models-catalog-meta">
            Requires Tier ${entry.requiredTier}+ (this PC: Tier ${entry.pc_tier?.tier ?? "?"} — ${entry.pc_tier?.label ?? "Unknown"})
            ${entry.minVramGB ? ` · Min VRAM ${entry.minVramGB}GB` : ""}
          </div>
          <div class="model-card-actions">${link}</div>
        </div>
      `;
    }).join("");

    modelsLog("renderCatalog() complete → " + entries.length + " entries.");
  },

  // -------------------------------------------------------
  // KEY MANAGEMENT — Cloud Provider cards
  //
  // Talks to backend.core.key_manager over the same WebSocket IPC
  // channel every other action on this page already uses (bridge.send +
  // the matching *_result packet in bindBridge() above), NOT the REST
  // /api/provider/* endpoint. That endpoint is real and tested
  // (backend/server.py), but that FastAPI server is never started by the
  // actual launcher (AriaLauncher/AriaLauncher/BackendManager.cs starts
  // logging_server.py and ws_server.py only) — a fetch() call to it
  // would hit ERR_CONNECTION_REFUSED in the real running app. IPC
  // reaches the exact same backend function and is proven to work
  // end-to-end, so that's what these functions actually use; only the
  // transport differs from a literal /api/... call, not the behavior.
  // -------------------------------------------------------

  buildProviderCards() {
    if (!this.providerCardsContainer) return;
    this.providerCardsContainer.innerHTML = "";
    PROVIDERS.forEach((name) => {
      this.providerCardsContainer.appendChild(
        this.buildKeyCard(name, (n) => this.saveProviderKey(n), (n) => this.deleteProviderKey(n))
      );
    });
  },

  buildKeyCard(name, onSave, onDelete) {
    const card = document.createElement("div");
    card.className = "provider-card";

    const heading = document.createElement("h3");
    heading.textContent = name;

    const input = document.createElement("input");
    input.type = "password";
    input.id = `${name}-key-input`;
    input.className = "key-input";
    input.placeholder = "API key";

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "key-btn save";
    saveBtn.textContent = "Save / Overwrite";
    saveBtn.addEventListener("click", () => onSave(name));

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "key-btn delete";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => onDelete(name));

    const status = document.createElement("div");
    status.id = `${name}-status`;
    status.className = "status-badge";
    status.textContent = "Checking…";

    card.appendChild(heading);
    card.appendChild(input);
    card.appendChild(saveBtn);
    card.appendChild(deleteBtn);
    card.appendChild(status);
    return card;
  },

  // ---- Provider key functions ----

  saveProviderKey(provider) {
    const input = document.getElementById(`${provider}-key-input`);
    const apiKey = input?.value.trim();
    if (!apiKey) return;

    modelsLog(`saveProviderKey(${provider})`);
    bridge.send(IPC.PROVIDER_KEY_SET_REQUEST, { provider, api_key: apiKey });
    if (input) input.value = "";
  },

  deleteProviderKey(provider) {
    modelsLog(`deleteProviderKey(${provider})`);
    bridge.send(IPC.PROVIDER_KEY_DELETE_REQUEST, { provider });
  },

  refreshProviderStatus(provider) {
    modelsLog(`refreshProviderStatus(${provider})`);
    // The backend returns every provider's status in one packet (see
    // backend/ipc_router.py's providers_list_request) — cheaper than a
    // dedicated per-provider round trip, and setProviderStatusBadge()
    // below only touches this one card's badge, so calling this in a
    // loop over all 14 providers still converges to the right state.
    bridge.send(IPC.PROVIDERS_LIST_REQUEST, {});
  },

  setProviderStatusBadge(provider, configured) {
    const badge = document.getElementById(`${provider}-status`);
    if (!badge) return;
    badge.textContent = configured ? "Connected" : "Not configured";
    badge.classList.toggle("connected", !!configured);
  },

  handleKeyActionResult(payload, kind, okMessage, onSuccess) {
    const ok = !!payload?.ok;
    const text = ok ? okMessage : (payload?.reason || `Could not update ${kind} key.`);

    modelsLog(`Key action result (${kind}) → ok=${ok}, message="${text}"`);
    if (typeof window.showToast === "function") {
      window.showToast(text, ok ? "success" : "error");
    }
    onSuccess();
  },

  selectModel(modelId) {
    modelsLog("selectModel() → " + modelId);
    this.renderModelDetails(modelId);
    this.render();
  },

  install(modelId) {
    modelsLog("install() → " + modelId);
    this.selectModel(modelId);

    // Reuses the existing Steam-style gated flow: this asks the
    // backend for check_requirements(), and if meets_minimum is
    // false the backend responds with model_install_blocked, which
    // ModelRequirements renders as a blocking popup (install
    // disabled). Otherwise it's a normal/warning popup and the user
    // can confirm from there — no gating logic duplicated here.
    ModelRequirements.requestPreInstallCheck(modelId);
  },

  uninstall(modelId) {
    modelsLog("uninstall() → " + modelId);
    bridge.send(IPC.MODEL_UNINSTALL_REQUEST, { model_id: modelId });
  },

  setActive(modelId) {
    modelsLog("setActive() → " + modelId);
    bridge.send(IPC.MODEL_SET_ACTIVE_REQUEST, { model_id: modelId });
  },

  setFallback(modelId) {
    modelsLog("setFallback() → " + modelId);
    bridge.send(IPC.MODEL_SET_FALLBACK_REQUEST, { model_id: modelId });
  },

  setEmergency(modelId) {
    modelsLog("setEmergency() → " + modelId);
    bridge.send(IPC.MODEL_SET_EMERGENCY_REQUEST, { model_id: modelId });
  },

  // -------------------------------------------------------
  // RENDERING
  // -------------------------------------------------------

  render() {
    if (!this.models.length) {
      // No hardcoded model list to fall back on — model_discovery.py
      // found nothing in ~/.aria-lite/models and no curated entries
      // exist either. Say so plainly instead of two grids quietly
      // claiming "no models installed" / "none available" side by side,
      // which reads as a working page with nothing to show rather than
      // "discovery found zero models".
      const message = `<p class="models-empty">No models found. Add a *.gguf file to your models folder and click Refresh.</p>`;
      if (this.installedGrid) this.installedGrid.innerHTML = message;
      if (this.availableGrid) this.availableGrid.innerHTML = "";
      modelsLog("render() — no models at all; showing empty-state message.");
      return;
    }

    const installed = this.models.filter((m) => m.installed);
    const available = this.models.filter((m) => !m.installed);

    this.renderGrid(this.installedGrid, installed, "No models installed yet.");
    this.renderGrid(this.availableGrid, available, "All registered models are installed.");
  },

  renderGrid(gridEl, list, emptyText) {
    if (!gridEl) return;

    if (!list.length) {
      gridEl.innerHTML = `<p class="models-empty">${emptyText}</p>`;
      return;
    }

    gridEl.innerHTML = "";
    list.forEach((entry) => gridEl.appendChild(this.buildCard(entry)));
  },

  // Thin wrapper around the shared card builder (components/model_card/
  // model_card.js) — same component webui/pages/local_models/
  // local_models.js uses, so "Details"/"Set as Main"/"Set as Fallback"/
  // "Set as Emergency"/"Uninstall"/"Install" all look and behave
  // identically wherever a model card appears.
  buildCard(entry) {
    return buildModelCard(entry, {
      selectedModelId: this.selectedModelId,
      actions: {
        onSelect: (modelId) => this.selectModel(modelId),
        onSetActive: (modelId) => this.setActive(modelId),
        onSetFallback: (modelId) => this.setFallback(modelId),
        onSetEmergency: (modelId) => this.setEmergency(modelId),
        onUninstall: (modelId) => this.uninstall(modelId),
        onInstall: (modelId) => this.install(modelId),
      },
    });
  },
};

export default Models;
modelsLog("Models page exported.");
