// components/topbar/topbar.js
// Copilot‑Style Topbar — unified dispatcher version

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

export const Topbar = {
  init() {
    console.log("[Topbar] Initialized");

    this.statusEl = document.getElementById("system-status");
    this.modeEl = document.getElementById("aria-mode-indicator");
    this.lightEl = document.getElementById("aria-mode-light");

    this.bindActions();

    // Expose global mode update API (still needed for Toast + Bridge)
    window.updateTopBarMode = (mode) => this.updateModeIndicator(mode);

    this.setStatus("ok", "System OK");

    // FIXED: the indicator used to just sit at its hardcoded HTML
    // default ("AUTOMATIC") until SOME packet happened to update it
    // during this session — on a fresh load (or after a reload), the
    // real persisted mode (backend.core.mode_manager — survives
    // restarts) could be "local" or "cloud" while this still showed
    // "Automatic", exactly the "top-right shows Automatic while Aria
    // says Cloud Mode" contradiction. Asking for the real state on init
    // closes that gap.
    bridge.send(IPC.MODE_STATUS_REQUEST, {});
  },

  /* -----------------------------------------------------------
     Unified Dispatcher Entry Point
     ----------------------------------------------------------- */
  handlePacket(packet) {
    const type = packet?.type;
    const payload = packet?.payload;

    // ---------------------------------------------------------
    // Connection-status — synthetic packet manufactured by
    // core/bridge.js from Electron main-process WebSocket lifecycle
    // events (see ARIA-Lite Desktop's main.js). Drives the topbar's
    // #system-status indicator, which existed and worked before this
    // (setStatus()) but had no caller and no matching DOM element until
    // now — see webui/index.html.
    // ---------------------------------------------------------
    if (type === "connection_status") {
      this._handleConnectionStatus(payload || {});
      return;
    }

    // ---------------------------------------------------------
    // NEW: Copilot-style mode change event (from app.js)
    // ---------------------------------------------------------
    if (type === "mode_changed") {
      const mode = packet.mode || "automatic";
      this.updateModeIndicator(mode);
      return;
    }

    // ---------------------------------------------------------
    // FIXED: real backend mode state — previously never listened for
    // here, which is exactly why "switch to local mode" via chat (which
    // sends mode_set_result, see backend/websocket/handlers.py's
    // _handle_model_switch_directly) never updated this indicator, and
    // why a fresh page load never reflected the real persisted mode
    // (mode_status_result, requested once in init() above, and already
    // requested by webui/pages/models/models.js and
    // webui/components/statusbar/statusbar.js — this listener picks up
    // whichever arrives first, from any requester, same shared
    // "backend-packet" event every other packet-driven UI piece uses).
    // ---------------------------------------------------------
    if (type === "mode_set_result") {
      this.updateModeIndicator(payload?.mode);
      return;
    }

    if (type === "mode_status_result") {
      const status = payload || packet;
      this.updateModeIndicator(status.routing_mode);
      return;
    }

    // ---------------------------------------------------------
    // NEW: Direct system_result (if app.js forwards it)
    // ---------------------------------------------------------
    if (type === "system_result") {
      const mode =
        payload?.result?.mode ||
        payload?.result?.value ||
        "automatic";

      this.updateModeIndicator(mode);
      return;
    }

    // ---------------------------------------------------------
    // LEGACY: ipc_command_response (old SAFE_TASK format)
    // ---------------------------------------------------------
    if (type === "ipc_command_response" && payload) {
      const op = payload.operation;

      // Direct mode update
      if (op === "set_model_pref") {
        this.updateModeIndicator(payload.mode);
        return;
      }

      // Tool-wrapped mode update
      if (
        op === "tool" &&
        payload.result &&
        payload.result.operation === "set_model_pref"
      ) {
        this.updateModeIndicator(payload.result.mode);
        return;
      }
    }
  },

  /* -----------------------------------------------------------
     CONNECTION STATUS (WebSocket IPC QoL)
     ----------------------------------------------------------- */
  _handleConnectionStatus(status) {
    const state = status.state || "unknown";

    if (state === "connected") {
      this.setStatus("ok", "Connected");
    } else if (state === "reconnecting") {
      this.setStatus("warn", `Reconnecting… (attempt ${status.attempt}/${status.maxAttempts})`);
    } else if (state === "disconnected") {
      this.setStatus("error", "Disconnected");
    } else {
      this.setStatus("warn", "Unknown connection state");
    }

    // Only toast on an actual state change — main.js sends one
    // connection-status packet per transition, not on a timer, but this
    // guard keeps it correct even if that ever changes.
    if (state !== this._lastConnState && typeof window.showToast === "function") {
      if (state === "reconnecting") {
        window.showToast(`Reconnecting to backend… (attempt ${status.attempt}/${status.maxAttempts})`, "error");
      } else if (state === "disconnected") {
        window.showToast("Backend connection lost. Please restart ARIA-Lite.", "error");
      } else if (state === "connected" && this._lastConnState) {
        // Skip the very first "connected" (this._lastConnState is
        // undefined on initial load) — nothing was actually lost yet, so
        // there's nothing worth telling the user about.
        window.showToast("Reconnected to backend.", "auto");
      }
    }
    this._lastConnState = state;
  },

  /* -----------------------------------------------------------
     STATUS MANAGEMENT
     ----------------------------------------------------------- */
  setStatus(level, text) {
    if (!this.statusEl) return;

    this.statusEl.textContent = text;

    this.statusEl.classList.remove("status-ok", "status-warn", "status-error");

    switch (level) {
      case "ok":
        this.statusEl.classList.add("status-ok");
        break;
      case "warn":
        this.statusEl.classList.add("status-warn");
        break;
      case "error":
        this.statusEl.classList.add("status-error");
        break;
      default:
        this.statusEl.classList.add("status-warn");
        break;
    }

    console.log(`[Topbar] Status → ${level.toUpperCase()}: ${text}`);
  },

  /* -----------------------------------------------------------
     MODE INDICATOR MANAGEMENT
     ----------------------------------------------------------- */
  updateModeIndicator(mode) {
    if (!this.modeEl || !this.lightEl) return;

    mode = mode || "automatic";

    // Update text label
    this.modeEl.textContent = mode.toUpperCase();

    // Remove previous mode classes
    this.modeEl.classList.remove("mode-automatic", "mode-local", "mode-cloud");
    this.lightEl.classList.remove("mode-automatic", "mode-local", "mode-cloud");

    // Apply new mode class
    this.modeEl.classList.add(`mode-${mode}`);
    this.lightEl.classList.add(`mode-${mode}`);

    // Pulse animation on text label
    this.modeEl.classList.remove("mode-change");
    void this.modeEl.offsetWidth; // force reflow
    this.modeEl.classList.add("mode-change");

    console.log(`[Topbar] Mode → ${mode.toUpperCase()}`);
  },

  /* -----------------------------------------------------------
     ACTION BUTTONS
     ----------------------------------------------------------- */
  bindActions() {
    const refreshBtn = document.getElementById("topbar-refresh-btn");
    const settingsBtn = document.getElementById("topbar-settings-btn");

    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        console.log("[Topbar] Refresh clicked");
        window.dispatchEvent(new Event("aria:refresh"));
      });
    }

    if (settingsBtn) {
      settingsBtn.addEventListener("click", () => {
        console.log("[Topbar] Settings clicked");
        // FIXED: this used to dispatch "aria:openSettings", an event
        // nothing in the app ever listened for — the button did
        // nothing. Route through the same navigatePanel mechanism
        // sidebar.js's nav buttons already use (router.js listens for
        // it) so Settings is actually reachable.
        window.dispatchEvent(new CustomEvent("navigatePanel", { detail: "settings" }));
      });
    }
  }
};

export default Topbar;
