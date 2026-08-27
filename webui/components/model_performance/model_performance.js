import { bridge } from "../../core/bridge.js";

function perfLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ModelPerformance", msg);
    }
  } catch (err) {
    console.error("[ModelPerformance LOG ERROR]", err);
  }
}

perfLog("=== MODEL PERFORMANCE MODULE LOADED ===");

export const ModelPerformance = {
  currentModelCfg: null,
  currentEstimate: null,

  init() {
    perfLog("ModelPerformance.init() called.");

    this.cache();
    // Window-level listener bound ONCE ever — see diagnostics.js's
    // Diagnostics.init() for why re-running this every visit would
    // silently stack up duplicate permanent listeners.
    if (!this._bridgeBound) {
      this.bindBridge();
      this._bridgeBound = true;
    }

    perfLog("ModelPerformance subsystem ready.");
  },

  cache() {
    this.root = document.getElementById("model-performance-panel");
    this.indicator = document.getElementById("model-performance-indicator");
    this.estimatedEl = document.getElementById("model-performance-estimated");
    this.recommendedEl = document.getElementById("model-performance-recommended");
    this.explanationEl = document.getElementById("model-performance-explanation");

    perfLog(
      "CACHE RESULT: " +
        JSON.stringify({
          root: !!this.root,
          indicator: !!this.indicator,
          estimatedEl: !!this.estimatedEl,
        })
    );
  },

  bindBridge() {
    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      // Fired directly in response to requestPerformanceEstimate()
      if (packet.type === "model_performance_result") {
        perfLog("Received model_performance_result.");
        this.render(packet.payload?.model_cfg, packet.payload?.projected_speed_toksec);
      }

      // safety_manager.evaluate_safety() results also carry
      // projected_speed_toksec — reuse it here if the panel is
      // showing that same model, so the estimate stays in sync
      // with the safety popup without a second round-trip.
      if (packet.type === "safety_decision_result") {
        const modelCfg = packet.payload?.model_cfg;
        if (modelCfg && this.currentModelCfg && modelCfg.id === this.currentModelCfg.id) {
          perfLog("Received safety_decision_result for current model → syncing speed estimate.");
          this.render(modelCfg, packet.payload?.projected_speed_toksec);
        }
      }
    });
  },

  /**
   * Ask the backend to estimate tokens/sec for a given model on this
   * machine. Result arrives via bindBridge() as model_performance_result.
   */
  requestPerformanceEstimate(modelId) {
    perfLog("requestPerformanceEstimate() → model_id=" + modelId);
    bridge.send("model_performance_request", { model_id: modelId });
  },

  // -------------------------------------------------------
  // RENDERING
  // -------------------------------------------------------

  render(modelCfg, estimatedTokSec) {
    this.currentModelCfg = modelCfg || null;
    this.currentEstimate = typeof estimatedTokSec === "number" ? estimatedTokSec : null;

    const req = modelCfg?.requirements || {};
    const minSpeed = req.minSpeedTokSec ?? null;
    const recSpeed = req.recSpeedTokSec ?? null;

    if (this.estimatedEl) {
      this.estimatedEl.textContent =
        this.currentEstimate != null ? `${this.currentEstimate.toFixed(1)} tok/sec` : "—";
    }
    if (this.recommendedEl) {
      this.recommendedEl.textContent = recSpeed != null ? `${recSpeed} tok/sec` : "—";
    }

    const { status, explanation } = this.evaluate(this.currentEstimate, minSpeed, recSpeed);

    if (this.indicator) {
      this.indicator.className = "perf-indicator perf-indicator-" + status;
      this.indicator.setAttribute("data-status", status);
      const icons = { pass: "✔", warn: "!", fail: "✘", unknown: "?" };
      this.indicator.textContent = icons[status] || "?";
      this.indicator.title = explanation;
    }
    if (this.explanationEl) {
      this.explanationEl.textContent = explanation;
    }

    perfLog(`render() complete → model_id=${modelCfg?.id}, estimate=${this.currentEstimate}, status=${status}`);
  },

  evaluate(estimate, minSpeed, recSpeed) {
    if (estimate == null) {
      return { status: "unknown", explanation: "No performance data available yet." };
    }

    if (recSpeed != null && estimate >= recSpeed) {
      return {
        status: "pass",
        explanation: "Estimated speed meets the recommended target — generation should feel responsive.",
      };
    }

    if (minSpeed != null && estimate >= minSpeed) {
      return {
        status: "warn",
        explanation: "Estimated speed meets the minimum but falls short of recommended — expect noticeably slower generation.",
      };
    }

    if (minSpeed != null && estimate < minSpeed) {
      return {
        status: "fail",
        explanation: "Estimated speed is below the minimum target — this model may feel too slow to use comfortably on this system.",
      };
    }

    return { status: "unknown", explanation: "No speed targets defined for this model." };
  },
};
