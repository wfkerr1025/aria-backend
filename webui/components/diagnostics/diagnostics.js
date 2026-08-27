import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

function diagLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Diagnostics", msg);
    }
  } catch (err) {
    console.error("[Diagnostics LOG ERROR]", err);
  }
}

diagLog("=== DIAGNOSTICS MODULE LOADED ===");

export const Diagnostics = {
  async init() {
    diagLog("Diagnostics.init() called.");

    this.cache();
    this.bindUI();
    // Window-level listeners are bound ONCE ever, not once per visit —
    // router.js destroys and recreates this panel's own DOM on every
    // navigation (so cache()/bindUI() above must re-run every time to
    // pick up the fresh elements), but `window` itself never gets
    // recreated. Re-running bindBridge()'s addEventListener() on every
    // visit would silently stack up an extra permanent listener per
    // visit, so every later packet fires the handler N times — the
    // same class of bug that made the Models page's Details panel
    // unreliable (see webui/pages/models/models.js).
    if (!this._bridgeBound) {
      this.bindBridge();
      window.addEventListener("openAutobalanceDiagnostics", () => this.requestAutobalance());
      this._bridgeBound = true;
    }

    await this.mountRoutingLog();

    this.requestDiagnostics();
    this.requestAutobalance();

    diagLog("Diagnostics subsystem ready.");
  },

  // Routing History section (moved here from the chat panel) —
  // fetch+inject routing_log.html's fragment fresh on EVERY init() call
  // (unlike the listener-binding above, this DOM insertion must repeat
  // every visit: router.js wipes #panel-container's innerHTML on every
  // navigation, so the mount point is a brand-new, empty element each
  // time) and hand off to its own module.
  async mountRoutingLog() {
    const mount = document.getElementById("diagnostics-routing-log-mount");
    if (!mount) return;

    try {
      const html = await fetch("components/routing_log/routing_log.html").then((r) => r.text());
      mount.innerHTML = html;
      const { default: RoutingLog } = await import("../routing_log/routing_log.js");
      RoutingLog.init();
      diagLog("Routing log panel mounted.");
    } catch (err) {
      diagLog("ERROR mounting routing log panel: " + err);
    }
  },

  requestAutobalance() {
    diagLog("requestAutobalance() → diagnostics_autobalance_request");
    bridge.send(IPC.DIAGNOSTICS_AUTOBALANCE_REQUEST, {});
  },

  renderAutobalance(data) {
    if (!this.autobalanceValue) return;

    const sessions = data?.active_sessions || {};
    const modelIds = Object.keys(sessions);

    if (!modelIds.length) {
      this.autobalanceValue.textContent = "None";
      if (this.autobalanceDetail) this.autobalanceDetail.innerHTML = "";
      return;
    }

    this.autobalanceValue.textContent = `${modelIds.length} active`;
    if (this.autobalanceDetail) {
      this.autobalanceDetail.innerHTML = modelIds.map((modelId) => {
        const s = sessions[modelId];
        return `<div class="diagnostics-detail-row">${modelId}: tier ${s.tier} (${s.tier_name}), CPU ${s.cpu_pct}%</div>`;
      }).join("");
    }

    diagLog("renderAutobalance() complete → " + JSON.stringify(data));
  },

  cache() {
    diagLog("Caching DOM elements.");

    this.engineValue = document.querySelector("#diag-engine .diagnostics-value");
    this.runBtn = document.getElementById("diag-run-btn");
    this.autobalanceValue = document.getElementById("diag-autobalance-value");
    this.autobalanceDetail = document.getElementById("diag-autobalance-detail");

    diagLog(
      "CACHE RESULT: " +
        JSON.stringify({
          engineValue: !!this.engineValue,
          runBtn: !!this.runBtn,
          autobalanceValue: !!this.autobalanceValue,
        })
    );
  },

  bindUI() {
    if (!this.runBtn) {
      diagLog("ERROR: diag-run-btn missing — cannot bind UI.");
      return;
    }

    this.runBtn.addEventListener("click", () => {
      diagLog("Run Full Diagnostics clicked → sending diagnostics_request.");
      this.requestDiagnostics();
    });

    diagLog("UI bound successfully.");
  },

  bindBridge() {
    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      if (packet.type === "diagnostics_response") {
        diagLog("Received diagnostics_response.");
        this.render(packet.payload);
        return;
      }

      if (packet.type === IPC.DIAGNOSTICS_AUTOBALANCE_RESULT) {
        diagLog("Received diagnostics_autobalance_result.");
        this.renderAutobalance(packet.payload || packet);
        return;
      }
    });
  },

  requestDiagnostics() {
    diagLog("requestDiagnostics() → diagnostics_request");
    bridge.send("diagnostics_request", {});
  },

  render(data) {
    if (!this.engineValue || !data) return;

    const ram = `${data.ram_used_gb ?? "—"} / ${data.ram_total_gb ?? "—"} GB`;
    const vram = `${data.vram_used_gb ?? "—"} / ${data.vram_total_gb ?? "—"} GB`;
    const unity = data.unity_running ? "Running" : "Not running";

    this.engineValue.textContent =
      `CPU ${data.cpu_usage_pct ?? "—"}% · RAM ${ram} · VRAM ${vram} · Unity: ${unity}`;

    diagLog("render() complete → " + JSON.stringify(data));
  },
};

export default Diagnostics;
diagLog("Diagnostics exported.");
