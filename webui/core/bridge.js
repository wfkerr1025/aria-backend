// core/bridge.js
// Electron‑Safe Unified Dispatcher Bridge
// Electron IPC → Unified Event Stream → ARIA_DISPATCH

function bridgeLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Bridge", msg);
    }
  } catch (err) {
    console.error("[Bridge LOG ERROR]", err);
  }
}

bridgeLog("=== BRIDGE MODULE LOADED ===");

export class Bridge {
  constructor() {
    console.log("[Bridge] Loaded (Electron IPC mode)");
    bridgeLog("Bridge constructed (Electron IPC mode).");

    this.handlers = {};

    window.ARIA_STATE = window.ARIA_STATE || {
      modelPref: "auto"
    };

    bridgeLog("ARIA_STATE initialized.");
  }

  // -----------------------------------------------------------
  // ELECTRON IPC CONNECTION (no browser WebSocket)
  // -----------------------------------------------------------
  connect() {
    console.log("[Bridge] Using Electron IPC instead of WebSocket");
    bridgeLog("Connecting via Electron IPC.");

    if (!window.aria) {
      console.error("[Bridge] window.aria missing — preload.js not loaded");
      bridgeLog("ERROR: window.aria missing — preload.js not loaded.");
      return;
    }

    window.aria.onBackendMessage((rawPacket) => {
      bridgeLog("Received raw backend packet: " + JSON.stringify(rawPacket));

      let packet = rawPacket;

      // Normalize backend packets (string → object)
      if (typeof rawPacket === "string") {
        try {
          packet = JSON.parse(rawPacket);
          bridgeLog("Parsed backend JSON packet.");
        } catch (err) {
          console.error("[Bridge] Invalid backend JSON:", err);
          bridgeLog("ERROR: Invalid backend JSON: " + err);
          return;
        }
      }

      try {
        const { type, payload } = packet;
        bridgeLog("Dispatching backend packet type: " + type);

        // -----------------------------------------------------------
        // Copilot-style system_result handling
        // -----------------------------------------------------------
        if (type === "system_result") {
          const mode =
            payload?.result?.mode ||
            payload?.result?.value ||
            "auto";

          window.ARIA_STATE.modelPref = mode;
          bridgeLog("System_result → modelPref updated to: " + mode);

          window.dispatchEvent(
            new CustomEvent("aria-mode-changed", {
              detail: { mode }
            })
          );
          bridgeLog("Event dispatched: aria-mode-changed");

          window.dispatchEvent(
            new CustomEvent("aria-toast", {
              detail: { text: payload?.summary || `Mode updated: ${mode}` }
            })
          );
          bridgeLog("Event dispatched: aria-toast");
        }

        // -----------------------------------------------------------
        // Forward ALL backend packets into unified event stream
        // -----------------------------------------------------------
        window.dispatchEvent(
          new CustomEvent("backend-packet", { detail: packet })
        );
        bridgeLog("Event dispatched: backend-packet");

        // Internal handlers
        if (this.handlers[type]) {
          bridgeLog("Executing " + this.handlers[type].length + " internal handlers for type: " + type);
          this.handlers[type].forEach(cb => cb(payload));
        }

      } catch (err) {
        console.error("[Bridge] Packet error:", err);
        bridgeLog("ERROR: Packet error: " + err);
      }
    });
  }

  // -----------------------------------------------------------
  // SEND PACKETS TO BACKEND (Electron IPC)
  // -----------------------------------------------------------
  send(type, payload = {}) {
    const packet = { type, payload };
    console.log("[Bridge] SEND (IPC):", packet);
    bridgeLog("SEND → " + JSON.stringify(packet));

    if (!window.aria) {
      console.error("[Bridge] window.aria missing — cannot send");
      bridgeLog("ERROR: window.aria missing — cannot send.");
      return;
    }

    window.aria.sendToBackend(packet);
  }

  // -----------------------------------------------------------
  // REGISTER PACKET HANDLERS
  // -----------------------------------------------------------
  on(type, callback) {
    bridgeLog("Registering handler for type: " + type);

    if (!this.handlers[type]) {
      this.handlers[type] = [];
    }
    this.handlers[type].push(callback);

    bridgeLog("Handler registered. Total handlers for " + type + ": " + this.handlers[type].length);
  }

  // -----------------------------------------------------------
  // ELECTRON → BACKEND IPC COMMANDS
  // -----------------------------------------------------------
  ipcSend(module, command, args = {}) {
    const payload = { module, command, args };
    console.log("[Bridge] IPC SEND:", payload);
    bridgeLog("IPC SEND → " + JSON.stringify(payload));

    if (!window.aria) {
      console.error("[Bridge] window.aria missing — cannot send IPC command");
      bridgeLog("ERROR: window.aria missing — cannot send IPC command.");
      return;
    }

    window.aria.sendToBackend({
      type: "ipc_command",
      payload
    });
  }
}

export const bridge = new Bridge();
bridgeLog("Bridge instance created.");
