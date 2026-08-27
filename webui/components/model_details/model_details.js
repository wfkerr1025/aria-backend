import { ModelRequirements } from "../model_requirements/model_requirements.js";
import { ModelPerformance } from "../model_performance/model_performance.js";

function detailsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ModelDetails", msg);
    }
  } catch (err) {
    console.error("[ModelDetails LOG ERROR]", err);
  }
}

detailsLog("=== MODEL DETAILS MODULE LOADED ===");

// ModelDetails combines the existing sub-panels into one view:
//   - ModelRequirements  → min/rec tables, difficulty, notes,
//                          CompatibilitySummary (pass/fail + badge),
//                          and the pre-install popup
//   - ModelPerformance   → estimated vs. recommended tok/sec
//
// It does not duplicate their markup or logic — it fetches each
// sub-panel's own HTML fragment into a mount point (same pattern
// core/router.js uses for top-level panels) and delegates to their
// existing init()/render() methods.
//
// mount() re-fetches and re-inserts BOTH fragments on every init() call
// — there used to be a `this.mounted` flag that skipped this after the
// first time, which was wrong: webui/core/router.js replaces the whole
// Models page's DOM (innerHTML) on every navigation away and back, so
// the mount points this fetches into are brand-new, empty elements each
// time. Skipping the re-fetch left the Details column with no content
// at all after navigating away and back — the panel looked "stuck
// empty" even though webui/pages/models/models.js's own selectedModelId
// state was still perfectly intact. See ModelRequirements.
// mountCompatSummary() for the same bug/fix, one level down.
export const ModelDetails = {
  async init() {
    detailsLog("ModelDetails.init() called.");

    this.cache();
    await this.mount();

    await ModelRequirements.init();
    ModelPerformance.init();

    detailsLog("ModelDetails subsystem ready.");
  },

  cache() {
    this.root = document.getElementById("model-details-panel");
    this.requirementsMount = document.getElementById("model-details-requirements-mount");
    this.performanceMount = document.getElementById("model-details-performance-mount");

    detailsLog(
      "CACHE RESULT: " +
        JSON.stringify({
          root: !!this.root,
          requirementsMount: !!this.requirementsMount,
          performanceMount: !!this.performanceMount,
        })
    );
  },

  async mount() {
    detailsLog("mount() → fetching sub-panel HTML fragments.");

    if (this.requirementsMount) {
      try {
        const html = await fetch("components/model_requirements/model_requirements.html").then((r) => r.text());
        this.requirementsMount.innerHTML = html;
        detailsLog("Requirements fragment mounted.");
      } catch (err) {
        detailsLog("ERROR mounting requirements fragment: " + err);
      }
    }

    if (this.performanceMount) {
      try {
        const html = await fetch("components/model_performance/model_performance.html").then((r) => r.text());
        this.performanceMount.innerHTML = html;
        detailsLog("Performance fragment mounted.");
      } catch (err) {
        detailsLog("ERROR mounting performance fragment: " + err);
      }
    }
  },

  /**
   * Load a model into the combined details view: requests both the
   * compatibility check (requirements + pass/fail) and the speed
   * estimate for the given model_id.
   */
  loadModel(modelId) {
    detailsLog("loadModel() → model_id=" + modelId);

    ModelRequirements.requestPreInstallCheck(modelId);
    ModelPerformance.requestPerformanceEstimate(modelId);
  },
};
