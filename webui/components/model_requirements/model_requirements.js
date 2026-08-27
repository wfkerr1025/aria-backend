import { bridge } from "../../core/bridge.js";
import { CompatibilitySummary } from "../compatibility-summary/compatibility-summary.js";

function reqLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ModelRequirements", msg);
    }
  } catch (err) {
    console.error("[ModelRequirements LOG ERROR]", err);
  }
}

reqLog("=== MODEL REQUIREMENTS MODULE LOADED ===");

// Labels for the compat check keys, used to render a readable list
// of what failed in the pre-install popup.
const CHECK_LABELS = {
  ram: "System RAM",
  cpu_cores: "CPU Cores",
  cpu_features: "CPU Features",
  vram: "GPU VRAM",
  avx2_tier: "AVX2 Throughput",
  storage: "Storage",
};

export const ModelRequirements = {
  currentModelCfg: null,
  currentCompat: null,

  async init() {
    reqLog("ModelRequirements.init() called.");

    this.cache();
    this.bindUI();
    // Window-level listener bound ONCE ever — see diagnostics.js's
    // Diagnostics.init() for why re-running this every visit would
    // silently stack up duplicate permanent listeners (this panel gets
    // its DOM rebuilt on every navigation, but `window` never resets).
    if (!this._bridgeBound) {
      this.bindBridge();
      this._bridgeBound = true;
    }

    // Re-fetch + re-insert the CompatibilitySummary fragment on EVERY
    // init() call, not just once — the mount point is a fresh, empty
    // element every time this panel's DOM is rebuilt after navigating
    // away and back (see model_details.js's own init()/mount() for the
    // matching fix one level up); a "did I already mount this?" flag
    // here made the fragment (and therefore the whole compat summary)
    // vanish after the first visit.
    await this.mountCompatSummary();
    CompatibilitySummary.init();

    reqLog("ModelRequirements subsystem ready.");
  },

  cache() {
    this.panel = document.getElementById("model-requirements-panel");
    this.nameEl = document.getElementById("model-requirements-name");
    this.minTable = document.getElementById("model-requirements-min-table");
    this.recTable = document.getElementById("model-requirements-rec-table");
    this.notes = document.getElementById("model-requirements-notes");
    this.difficulty = document.getElementById("model-requirements-difficulty");
    this.minBadge = document.getElementById("model-requirements-min-badge");
    this.recBadge = document.getElementById("model-requirements-rec-badge");
    this.compatMount = document.getElementById("model-requirements-compat-mount");

    this.popup = document.getElementById("preinstall-popup");
    this.popupContent = this.popup?.querySelector(".preinstall-popup-content");
    this.popupModelName = document.getElementById("preinstall-model-name");
    this.popupHint = document.getElementById("preinstall-hint");
    this.popupFailedList = document.getElementById("preinstall-failed-list");
    this.installBtn = document.getElementById("preinstall-install-btn");
    this.cancelBtn = document.getElementById("preinstall-cancel-btn");

    reqLog(
      "CACHE RESULT: " +
        JSON.stringify({
          panel: !!this.panel,
          popup: !!this.popup,
          installBtn: !!this.installBtn,
          compatMount: !!this.compatMount,
        })
    );
  },

  async mountCompatSummary() {
    if (!this.compatMount) return;

    try {
      const html = await fetch("components/compatibility-summary/compatibility-summary.html").then((r) => r.text());
      this.compatMount.innerHTML = html;
      reqLog("CompatibilitySummary fragment mounted.");
    } catch (err) {
      reqLog("ERROR mounting CompatibilitySummary fragment: " + err);
    }
  },

  bindUI() {
    this.cancelBtn?.addEventListener("click", () => {
      reqLog("Pre-install popup cancelled by user.");
      this.hidePopup();
    });

    this.installBtn?.addEventListener("click", () => {
      if (!this.currentModelCfg) return;

      if (this.currentCompat && !this.currentCompat.meets_minimum) {
        reqLog("Install click ignored → blocked by compatibility gate.");
        return;
      }

      reqLog("Install confirmed for model_id=" + this.currentModelCfg.id);

      bridge.send("model_install_request", { model_id: this.currentModelCfg.id });
      this.hidePopup();
    });
  },

  bindBridge() {
    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      if (packet.type === "model_requirements_result") {
        reqLog("Received model_requirements_result.");
        this.renderRequirements(packet.payload?.model_cfg, packet.payload?.compat);

        // models.js sends this same model_requirements_request both to
        // populate the Details panel and as the pre-install gate check —
        // there's no separate request type for "user clicked Install".
        // installed:false is the backend's signal that minimum
        // requirements were met for a model that isn't installed yet,
        // so this response doubles as "ready to install" → open the
        // confirmation popup.
        if (packet.payload?.installed === false) {
          this.showPopup(packet.payload?.model_cfg);
        }
      }

      if (packet.type === "model_install_blocked") {
        reqLog("Received model_install_blocked → " + JSON.stringify(packet.payload));
        this.renderRequirements(packet.payload?.model_cfg, packet.payload?.compat);
        this.showPopup(packet.payload?.model_cfg);
      }
    });
  },

  /**
   * Ask the backend to run check_requirements() for a given model
   * and open the Steam-style pre-install popup once the result
   * comes back via bindBridge().
   */
  requestPreInstallCheck(modelId) {
    reqLog("requestPreInstallCheck() → model_id=" + modelId);
    bridge.send("model_requirements_request", { model_id: modelId });
  },

  // -------------------------------------------------------
  // RENDERING
  // -------------------------------------------------------

  renderRequirements(modelCfg, compat) {
    this.currentModelCfg = modelCfg || null;
    this.currentCompat = compat || null;

    const req = modelCfg?.requirements || {};

    if (this.nameEl) {
      this.nameEl.textContent = modelCfg?.name || "Model Requirements";
    }
    if (this.difficulty) {
      const difficulty = req.difficulty || "Unknown";
      this.difficulty.textContent = difficulty;
      this.difficulty.className =
        "model-req-difficulty model-req-difficulty-" + difficulty.toLowerCase();
      this.difficulty.title = req.notes
        ? `${difficulty} — ${req.notes}`
        : `Difficulty: ${difficulty}`;
    }
    if (this.notes) {
      this.notes.textContent = req.notes || "";
    }

    this.renderTable(this.minTable, req, "min");
    this.renderTable(this.recTable, req, "rec");
    this.renderBadges(compat);

    CompatibilitySummary.render(compat);

    reqLog("renderRequirements() complete for model_id=" + modelCfg?.id);
  },

  renderBadges(compat) {
    this.setBadge(this.minBadge, compat ? compat.meets_minimum : null, "Meets Minimum Requirements", "Below Minimum Requirements");
    this.setBadge(this.recBadge, compat ? compat.meets_recommended : null, "Meets Recommended Requirements", "Below Recommended Requirements");
  },

  setBadge(el, passed, passText, failText) {
    if (!el) return;

    if (passed === null || passed === undefined) {
      el.textContent = "Checking…";
      el.className = "model-req-badge model-req-badge-unknown";
      return;
    }

    el.textContent = (passed ? "✔ " : "✘ ") + (passed ? passText : failText);
    el.className = "model-req-badge model-req-badge-" + (passed ? "pass" : "fail");
  },

  renderTable(tableEl, req, tier) {
    if (!tableEl) return;

    const rows = [
      ["RAM", `${req[tier + "RamGB"] ?? "—"} GB`],
      ["CPU Cores", req[tier + "CpuCores"] ?? "—"],
      ["CPU Features", (req[tier + "CpuFeatures"] || []).join(", ") || "—"],
      ["VRAM", `${req[tier + "VramGB"] ?? "—"} GB`],
      ["Speed", `${req[tier + "SpeedTokSec"] ?? "—"} tok/sec`],
      ["Context", req[tier + "Context"] ?? "—"],
    ];

    tableEl.innerHTML = rows
      .map(
        ([label, value]) => `
          <div class="model-req-row">
            <span class="model-req-label">${label}</span>
            <span class="model-req-value">${value}</span>
          </div>`
      )
      .join("");
  },

  // -------------------------------------------------------
  // PRE-INSTALL POPUP
  // -------------------------------------------------------

  showPopup(modelCfg) {
    if (!this.popup) return;
    if (this.popupModelName) {
      this.popupModelName.textContent = modelCfg?.name || modelCfg?.id || "";
    }

    this.applyGating(this.currentCompat);

    this.popup.classList.remove("hidden");
    reqLog("Pre-install popup shown for model_id=" + modelCfg?.id);
  },

  hidePopup() {
    if (!this.popup) return;
    this.popup.classList.add("hidden");
    reqLog("Pre-install popup hidden.");
  },

  /**
   * Steam-style install gating:
   *  - meets_minimum == false → blocking popup, install disabled,
   *    failed minimum requirements listed.
   *  - meets_minimum == true but meets_recommended == false →
   *    warning popup, install still allowed.
   *  - both true → normal popup, install allowed.
   */
  applyGating(compat) {
    this.popupContent?.classList.remove("preinstall-blocked", "preinstall-warning", "preinstall-ok");
    if (this.popupFailedList) this.popupFailedList.innerHTML = "";

    if (!compat) {
      reqLog("applyGating() → no compat data yet, allowing install (unknown state).");
      if (this.installBtn) this.installBtn.disabled = false;
      if (this.popupHint) this.popupHint.textContent = "";
      return;
    }

    if (!compat.meets_minimum) {
      reqLog("applyGating() → BLOCKED, missing_minimum=" + JSON.stringify(compat.missing_minimum));
      this.popupContent?.classList.add("preinstall-blocked");
      if (this.installBtn) this.installBtn.disabled = true;
      if (this.popupHint) {
        this.popupHint.textContent = "✘ Your system may experience slow performance.";
      }
      this.renderFailedList(compat.missing_minimum);
      return;
    }

    if (!compat.meets_recommended) {
      reqLog("applyGating() → WARNING, missing_recommended=" + JSON.stringify(compat.missing_recommended));
      this.popupContent?.classList.add("preinstall-warning");
      if (this.installBtn) this.installBtn.disabled = false;
      if (this.popupHint) {
        this.popupHint.textContent = "⚠ Your system meets minimum requirements.";
      }
      this.renderFailedList(compat.missing_recommended);
      return;
    }

    reqLog("applyGating() → OK, recommended requirements met.");
    this.popupContent?.classList.add("preinstall-ok");
    if (this.installBtn) this.installBtn.disabled = false;
    if (this.popupHint) {
      this.popupHint.textContent = compat.exceeds_recommended
        ? "✔ Your system exceeds recommended requirements."
        : "✔ Your system meets recommended requirements.";
    }
  },

  renderFailedList(missing) {
    if (!this.popupFailedList) return;
    this.popupFailedList.innerHTML = (missing || [])
      .map((name) => `<li>✘ ${CHECK_LABELS[name] || name}</li>`)
      .join("");
  },
};
