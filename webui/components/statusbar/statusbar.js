// components/statusbar/statusbar.js
// Bottom-left model indicator — a fixed, page-global overlay
// (webui/index.html's #active-model-container, aligned with the
// bottom-right System OK indicator), not part of any router-loaded
// panel's own HTML, so it's always visible on every page and never
// destroyed/recreated by navigation.
//
// Shows ONLY the Active Model: "Model: <name> (Local/Cloud/Automatic)",
// bound strictly to backend/ipc_router.py's mode_status_result's
// active_model_id/active_model_display_name/routing_mode. This is
// deliberately a MODE indicator, not a routing-context dashboard —
// System Model (fallback_model_id) and Emergency Model
// (emergency_model_id) used to also render here, but those are system
// CONFIGURATION values (which model plays which registry role), not
// something about the current chat turn, and showing all three in the
// same small bottom-bar strip read as "routing context" rather than
// "what model am I talking to right now". They now live only in
// Diagnostics -> Models (webui/pages/models/models.js's
// renderDiagnostics()) and Settings -> Local Models (per-card role
// badges) — see this app's model-context audit for why those two
// pages are the right home for configuration values and the chat
// footer/status bar is not.
//
// An earlier version also let backend.core.streaming_engine.py's
// per-turn active_model_changed drive this label directly (so
// Automatic Model Routing's real per-message choice would show
// immediately). That put a genuinely different value in the "Active
// Model" slot than backend/core/model_registry.py's stable "Main"
// role: a simple prompt can legitimately route to a lighter/fallback-
// tier model under Automatic Mode, which then made this label disagree
// with the Models/Diagnostics page and Routing Log (both keyed off the
// stable registry role). active_model_changed is now consulted only to
// know which local model to query the auto-balancer for (the
// "Balanced" chip) — never to render the label text.
//
// Independent of webui/components/topbar/topbar.js's connection-status
// widget (which stays exactly as it was) — this listens for its own
// backend-packet events directly, the same pattern
// webui/pages/models/models.js and webui/pages/cloud_llms/cloud_llms.js
// already use for packets outside the ARIA_DISPATCH/dispatchToPanel
// scheme, so nothing about app.js's dispatch logic needed to change to
// add this.
//
// Bootstrapped once at app startup (see webui/core/app.js, alongside
// Toast/Topbar) — there is only ever one instance of this widget, so
// "chat view" and "models/settings view" can never show different
// values by construction.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import { describeActiveModel } from "../../core/model_truth.js";

function statusbarLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Statusbar", msg);
    }
  } catch (err) {
    console.error("[Statusbar LOG ERROR]", err);
  }
}

const Statusbar = {
  // Set by every render path below — read by getActiveModelDisplayName()
  // (webui/components/warning_banner/warning_banner.js reuses this
  // rather than re-resolving the model name from a second IPC round
  // trip).
  _activeModelDisplayName: null,
  _activeModelLocation: "unknown", // "local" | "cloud" | "automatic" | "unknown"
  _balancerActive: false,
  // Batch 2.5 — tri-state: true (connected), false (disconnected), null
  // (no connection_status has ever been seen — most tests/environments
  // never send one, so this must NOT be treated as "disconnected").
  _connected: null,
  _lastModeStatus: null,

  init() {
    this.el = document.getElementById("active-model-status");
    this.balancedChipEl = document.getElementById("auto-balance-chip");
    this._connected = null;
    this._lastModeStatus = null;
    if (!this.el) {
      statusbarLog("ERROR: #active-model-status not found.");
      return;
    }

    if (this.balancedChipEl) {
      this.balancedChipEl.addEventListener("click", () => {
        window.dispatchEvent(new CustomEvent("navigatePanel", { detail: "diagnostics" }));
        window.dispatchEvent(new CustomEvent("openAutobalanceDiagnostics"));
      });
    }

    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      // Per-turn signal — used ONLY to know which local model to query
      // the auto-balancer for (see _refreshBalancerChip()). Deliberately
      // NEVER drives the Active Model text — see the module docstring.
      if (packet.type === IPC.ACTIVE_MODEL_CHANGED) {
        this._refreshBalancerChip(packet);
        return;
      }

      // Anything that can change WHICH model holds the active role, or
      // the routing mode itself — re-request mode_status_result so the
      // label stays in sync with the single source of truth.
      // (mode_set_result: an explicit "switch to X" chat command, see
      // backend/websocket/handlers.py's _handle_model_switch_directly.
      // model_set_active_result: the Models/Local Models page's "Set as
      // Main" button.)
      if (packet.type === IPC.MODE_SET_RESULT || packet.type === IPC.MODEL_SET_ACTIVE_RESULT) {
        bridge.send(IPC.MODE_STATUS_REQUEST, {});
        return;
      }

      // The single source of truth for the Active Model label.
      if (packet.type === IPC.MODE_STATUS_RESULT) {
        this.refreshStatusFromModeStatusResult(packet.payload || packet);
        return;
      }

      // Batch 2.5 — backend disconnect/reconnect (Batch 1's heartbeat
      // watchdog + real Electron transport state, both funneled through
      // this same synthetic packet by core/bridge.js). While
      // disconnected, the label must show that fact instead of
      // whatever model/provider was last known true — a stale value
      // here is exactly the kind of UI lie this batch exists to
      // prevent. On reconnect, re-request the real state rather than
      // trusting anything cached from before the drop.
      if (packet.type === IPC.CONNECTION_STATUS) {
        this._handleConnectionStatus(packet.payload || {});
        return;
      }

      // Balancer state for the CURRENTLY active local model — requested
      // by _refreshBalancerChip() right after each active_model_changed.
      // Additive: unrelated to any other consumer of this same result
      // type (webui/components/diagnostics/diagnostics.js has its own
      // independent listener for the same packet).
      if (packet.type === IPC.DIAGNOSTICS_AUTOBALANCE_RESULT) {
        this._applyBalancerResult(packet.payload || packet);
        return;
      }
    });

    // Ask for an initial value immediately rather than showing the
    // HTML's static placeholder until the first chat turn completes.
    bridge.send(IPC.MODE_STATUS_REQUEST, {});

    statusbarLog("Statusbar ready.");
  },

  // Public getter — see warning_banner.js, which needs "whatever the
  // statusbar is currently showing as the Active Model" without
  // re-resolving it itself.
  getActiveModelDisplayName() {
    return this._activeModelDisplayName;
  },

  getActiveModelLocation() {
    return this._activeModelLocation;
  },

  // Public getter — the raw mode_status_result payload behind the
  // current label, for any future consumer that wants the full truth
  // (routing_mode, cloud_provider, location, ...) rather than just the
  // rendered display name/location (see core/model_truth.js's
  // describeActiveModel(), which is what both this widget and
  // webui/pages/models/models.js's "Current Model" row already build
  // from this exact shape).
  getLastModeStatus() {
    return this._lastModeStatus;
  },

  _refreshBalancerChip(activeModelChangedPacket) {
    if (activeModelChangedPacket.location !== "local" || !activeModelChangedPacket.modelId) {
      this._balancerActive = false;
      this._renderBalancerChip();
      return;
    }
    this._pendingBalancerModelId = activeModelChangedPacket.modelId;
    bridge.send(IPC.DIAGNOSTICS_AUTOBALANCE_REQUEST, {});
  },

  _applyBalancerResult(data) {
    const modelId = this._pendingBalancerModelId;
    const session = modelId && data?.active_sessions ? data.active_sessions[modelId] : null;
    this._balancerActive = !!(session && session.tier > 0);
    this._renderBalancerChip();
  },

  _renderBalancerChip() {
    if (!this.balancedChipEl) return;
    this.balancedChipEl.style.display = this._balancerActive ? "inline-block" : "none";
  },

  // status: mode_status_result's payload — {routing_mode, cloud_provider,
  //         cloud_provider_display_name, active_model_id,
  //         active_model_display_name, location, explicit_model_override}
  //         — see backend/ipc_router.py's _handle_mode_status(). This is
  //         the ONLY thing that renders the Active Model label — see the
  //         module docstring for why per-turn active_model_changed no
  //         longer participates, and why fallback/emergency never did.
  //
  // Batch 2.5: the single named entry point every mode_status_result
  // goes through — computes the full next label/class/display-name via
  // core/model_truth.js's describeActiveModel() (shared with
  // webui/pages/models/models.js's Diagnostics "Current Model" row, so
  // the two surfaces can never disagree) and applies it in one atomic
  // _apply() call. Nothing here can leave the DOM showing a label built
  // from one status and a class built from another.
  refreshStatusFromModeStatusResult(status) {
    this._lastModeStatus = status;

    // A backend-disconnected state (see _handleConnectionStatus) must
    // win over any mode_status_result that might still be in flight —
    // a late-arriving truthful packet must not silently overwrite the
    // "disconnected" state the user is actually looking at.
    if (this._connected === false) {
      return;
    }

    const desc = describeActiveModel(status);
    if (desc.unknown) {
      this._apply("unknown", "Model: —", null);
      return;
    }

    if (desc.location === "cloud") {
      const label = desc.modelName
        ? `Model: ${desc.modelName} (${desc.provider} · Cloud)`
        : `Model: ${desc.provider} (Cloud)`;
      this._apply("cloud", label, desc.modelName || desc.provider);
    } else if (desc.location === "automatic") {
      this._apply("automatic", `Model: ${desc.modelName} (Automatic)`, desc.modelName);
    } else {
      this._apply("local", `Model: ${desc.modelName} (Local)`, desc.modelName);
    }

    statusbarLog("Active model updated (mode status) -> " + this.el.textContent);
    console.log("[Statusbar] " + this.el.textContent);
  },

  // Batch 2.5 — connection_status handling (see the module's packet
  // listener above). Deliberately does NOT try to show a "last known"
  // model while disconnected: Batch 1's routing invariants make a
  // stale value indistinguishable from a lie once the backend that
  // vouched for it is no longer reachable.
  _handleConnectionStatus(status) {
    const state = status.state || "unknown";

    if (state === "disconnected") {
      this._connected = false;
      this._apply("disconnected", "Model: Unavailable (Backend disconnected)", null);
      return;
    }

    if (state === "connected") {
      const wasDisconnected = this._connected === false;
      this._connected = true;
      if (wasDisconnected) {
        // Refresh from the backend's own truth the instant it's back —
        // never trust anything cached from before the drop.
        bridge.send(IPC.MODE_STATUS_REQUEST, {});
      }
    }
  },

  _apply(kind, label, displayName) {
    if (!this.el) return;
    this.el.textContent = label;
    this.el.className = `model-${kind}`;
    this._activeModelDisplayName = displayName;
    this._activeModelLocation = kind;
  },
};

export default Statusbar;
statusbarLog("Statusbar module loaded.");
