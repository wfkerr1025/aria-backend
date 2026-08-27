// pages/local_models/local_models.js
//
// Local Models page: installed local models, the folder they live in,
// an estimated disk usage total, and — same as webui/pages/models/
// models.js — a Details panel (requirements/compatibility/performance)
// for whichever model is selected. All derived from the SAME
// models_list_request/models_list_result IPC round-trip models.js
// already uses (see backend/core/model_manager.list_models()), just
// filtered down to installed local models and presented as its own
// dedicated Settings page.
//
// Cards are built by the shared components/model_card/model_card.js
// (buildModelCard()) — the SAME component models.js's Installed/
// Available grids use, per this app's "one unified card design"
// requirement — with the same action set models.js's installed-model
// cards offer (Details, Set as Main/Fallback/Emergency, Uninstall).
//
// The Details panel lifecycle here mirrors models.js's own fix
// exactly: DOM mounting (mountDetails()) re-fetches and re-inserts
// model_details.html's fragment on EVERY init() call (router.js
// destroys/recreates this page's whole DOM on every navigation, so the
// mount point is a fresh, empty element each time — a "did I already
// mount this?" flag here would leave the panel permanently empty from
// the second visit onward), while the window-level backend-packet
// listener is bound exactly once ever (a `window` listener re-added on
// every visit would stack up duplicates and fire every handler N
// times). selectedModelId persists on this page's own singleton across
// navigations and is used to rehydrate the panel on return.
//
// No new backend endpoint was added for the folder-path/disk-usage
// display (frontend-only) — the models folder path is derived
// client-side from an installed local model's own `path` field (its
// parent directory), and disk usage is an ESTIMATE computed from each
// model's parameter count and quantization (bits-per-weight), not a
// real filesystem stat. Both are labeled honestly as such rather than
// presented as exact facts — see this app's "never state unsure things
// as fact" rule (backend/core/conversation_manager.py's SYSTEM_PROMPT).

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import { ModelDetails } from "../../components/model_details/model_details.js";
import { buildModelCard } from "../../components/model_card/model_card.js";

function localModelsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("LocalModelsPage", msg);
    }
  } catch (err) {
    console.error("[LocalModelsPage LOG ERROR]", err);
  }
}

localModelsLog("=== LOCAL MODELS PAGE MODULE LOADED ===");

// Approximate bits-per-weight for common GGUF quantizations — enough to
// give the user a rough, clearly-labeled size estimate, not an exact
// filesystem stat (this page never claims otherwise).
const BITS_PER_WEIGHT = [
  [/^Q2/i, 2.6], [/^Q3/i, 3.9], [/^Q4/i, 4.8], [/^Q5/i, 5.7],
  [/^Q6/i, 6.6], [/^Q8/i, 8.5], [/^F16|^FP16/i, 16], [/^F32|^FP32/i, 32],
];

function estimateBytes(params, quant) {
  if (!params) return null;
  const match = BITS_PER_WEIGHT.find(([re]) => re.test(quant || ""));
  const bitsPerWeight = match ? match[1] : 4.8; // reasonable default (~Q4)
  return Math.round((params * bitsPerWeight) / 8);
}

function formatBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function dirname(path) {
  if (!path) return null;
  const lastSlash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return lastSlash === -1 ? null : path.slice(0, lastSlash);
}

export default {
  models: [],
  // Persists across navigation (module-level singleton) — see this
  // file's own header comment and models.js's identical pattern.
  selectedModelId: null,

  init() {
    localModelsLog("LocalModels.init() called.");
    console.log("[LocalModelsPage] Initializing");

    this.grid = document.getElementById("local-models-grid");
    this.folderPathEl = document.getElementById("local-models-folder-path");
    this.diskUsageEl = document.getElementById("local-models-disk-usage");
    this.refreshBtn = document.getElementById("local-models-refresh-btn");
    this.detailsMount = document.getElementById("local-models-details-mount");
    this.detailsPlaceholder = document.getElementById("local-models-details-placeholder");

    if (!this.grid) {
      localModelsLog("ERROR: #local-models-grid not found.");
      return;
    }

    this.refreshBtn?.addEventListener("click", () => {
      localModelsLog("Manual refresh requested.");
      this.requestList();
    });

    // Window-level listener bound ONCE ever, not once per visit — see
    // this file's header comment.
    if (!this._bridgeBound) {
      window.addEventListener("backend-packet", (evt) => {
        const packet = evt.detail;
        if (!packet) return;

        if (packet.type === IPC.MODELS_LIST_RESULT) {
          const all = packet.payload?.models || [];
          localModelsLog("Received models_list_result → " + all.length + " total models.");
          this.models = all.filter((entry) => entry.installed && entry.model_cfg?.provider === "local");
          this.render();
        }

        if (
          packet.type === IPC.MODEL_UNINSTALL_RESULT ||
          packet.type === IPC.MODEL_SET_ACTIVE_RESULT ||
          packet.type === IPC.MODEL_SET_FALLBACK_RESULT ||
          packet.type === IPC.MODEL_SET_EMERGENCY_RESULT
        ) {
          const ok = !!packet.payload?.ok;
          if (typeof window.showToast === "function") {
            window.showToast(ok ? "Updated." : (packet.payload?.reason || "Action failed."), ok ? "success" : "error");
          }
          this.requestList();
        }
      });
      this._bridgeBound = true;
    }

    this.mountDetails();
    this.requestList();
    localModelsLog("Local Models page ready.");
  },

  requestList() {
    bridge.send(IPC.MODELS_LIST_REQUEST, {});
  },

  // Re-fetches + re-inserts model_details.html on EVERY init() call —
  // see this file's header comment for why (mirrors models.js's
  // mountDetails() exactly).
  async mountDetails() {
    if (!this.detailsMount) return;

    try {
      const html = await fetch("components/model_details/model_details.html").then((r) => r.text());
      this.detailsMount.insertAdjacentHTML("beforeend", html);
      await ModelDetails.init();
      localModelsLog("ModelDetails mounted into Local Models page.");

      // Rehydrate: if a model was already selected on a PREVIOUS visit,
      // render it immediately instead of leaving the freshly-mounted
      // panel on its placeholder until the user clicks a card again.
      if (this.selectedModelId) {
        this.renderModelDetails(this.selectedModelId);
      }
    } catch (err) {
      localModelsLog("ERROR mounting ModelDetails: " + err);
    }
  },

  // Stable entry point for "show this model's Details panel" — the
  // ONLY place that hides the placeholder and asks ModelDetails to load
  // a model (same role as models.js's identically-named function).
  renderModelDetails(modelId) {
    localModelsLog("renderModelDetails() → " + modelId);
    this.selectedModelId = modelId;
    this.detailsPlaceholder?.classList.add("hidden");
    ModelDetails.loadModel(modelId);
  },

  selectModel(modelId) {
    localModelsLog("selectModel() → " + modelId);
    this.renderModelDetails(modelId);
    this.render();
  },

  setActive(modelId) {
    localModelsLog("setActive() → " + modelId);
    bridge.send(IPC.MODEL_SET_ACTIVE_REQUEST, { model_id: modelId });
  },

  setFallback(modelId) {
    localModelsLog("setFallback() → " + modelId);
    bridge.send(IPC.MODEL_SET_FALLBACK_REQUEST, { model_id: modelId });
  },

  setEmergency(modelId) {
    localModelsLog("setEmergency() → " + modelId);
    bridge.send(IPC.MODEL_SET_EMERGENCY_REQUEST, { model_id: modelId });
  },

  uninstall(modelId) {
    localModelsLog("uninstall() → " + modelId);
    bridge.send(IPC.MODEL_UNINSTALL_REQUEST, { model_id: modelId });
  },

  render() {
    this.renderFolderPath();
    this.renderDiskUsage();
    this.renderGrid();
  },

  renderFolderPath() {
    if (!this.folderPathEl) return;
    const withPath = this.models.find((entry) => entry.model_cfg?.path);
    const folder = withPath ? dirname(withPath.model_cfg.path) : null;
    this.folderPathEl.textContent = folder || "No local models installed yet — the default folder is ~/.aria-lite/models.";
  },

  renderDiskUsage() {
    if (!this.diskUsageEl) return;
    if (!this.models.length) {
      this.diskUsageEl.textContent = "Disk usage: —";
      return;
    }
    const totalBytes = this.models.reduce((sum, entry) => {
      const est = estimateBytes(entry.model_cfg?.params, entry.model_cfg?.quant);
      return sum + (est || 0);
    }, 0);
    this.diskUsageEl.textContent = `Disk usage (estimated): ${formatBytes(totalBytes)} across ${this.models.length} model(s)`;
  },

  renderGrid() {
    if (!this.models.length) {
      this.grid.innerHTML = `<p class="models-empty">No local models installed yet. Drag a *.gguf file into the models folder above and click Refresh.</p>`;
      return;
    }

    this.grid.innerHTML = "";
    this.models.forEach((entry) => this.grid.appendChild(this.buildCard(entry)));
  },

  // Thin wrapper around the shared card builder — same component and
  // same action set as models.js's installed-model cards (this page
  // only ever lists installed local models, so "Install" never
  // applies here). extraStats carries the params-count/quant/context
  // and estimated-on-disk-size info this page has always shown (its
  // whole point being disk management) that the Models page's own
  // cards have no use for.
  buildCard(entry) {
    const { model_cfg: cfg } = entry;
    const req = cfg.requirements || {};
    const sizeBytes = estimateBytes(cfg.params, cfg.quant);
    const paramsB = cfg.params ? (cfg.params / 1_000_000_000).toFixed(1) + "B params" : null;
    const metaLine = [paramsB, cfg.quant, (req.maxContext || cfg.maxContext) ? `${cfg.maxContext} ctx` : null]
      .filter(Boolean)
      .join(" · ");

    return buildModelCard(entry, {
      selectedModelId: this.selectedModelId,
      extraStats: [metaLine, `Size (estimated): ${formatBytes(sizeBytes)}`].filter(Boolean),
      actions: {
        onSelect: (modelId) => this.selectModel(modelId),
        onSetActive: (modelId) => this.setActive(modelId),
        onSetFallback: (modelId) => this.setFallback(modelId),
        onSetEmergency: (modelId) => this.setEmergency(modelId),
        onUninstall: (modelId) => this.uninstall(modelId),
      },
    });
  },
};

localModelsLog("Local Models page exported.");
