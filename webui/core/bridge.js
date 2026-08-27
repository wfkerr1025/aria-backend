// core/bridge.js
// Electron‑Safe Unified Dispatcher Bridge
// Electron IPC → Unified Event Stream → ARIA_DISPATCH

import { IPC } from "./ipc_schema.js";
import { DebugLog } from "./debug_log.js";

function bridgeLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Bridge", msg);
    }
  } catch (err) {
    console.error("[Bridge LOG ERROR]", err);
  }
}

// -----------------------------------------------------------
// Unified cross-runtime logging (backend/logging_server.py,
// http://127.0.0.1:5001/log) — distinct from bridgeLog() above, which
// goes through Electron IPC into aria_startup.log. This one ships
// straight from the browser via fetch() into the ONE shared run-log
// file every runtime in the stack writes into. Never throws — a log
// call must never be able to break the UI.
// -----------------------------------------------------------
const LOG_SERVER_URL = "http://127.0.0.1:5001/log";

// -----------------------------------------------------------
// Batch 1 stability fix — backend heartbeat tracking.
//
// backend/websocket/handlers.py now sends an unsolicited `heartbeat`
// packet every HEARTBEAT_INTERVAL_SECONDS (10s) for the life of the
// connection (see WebSocketHandler._heartbeat_loop()). Previously this
// bridge had no independent way to tell "backend process is hung but
// the socket is still open" from "backend is fine" — only a real
// Electron-level disconnect (onConnectionStatus) ever fired, and a
// stuck-but-connected backend would silently never respond to
// anything while the UI kept behaving as if it could. Missing
// HEARTBEAT_TIMEOUT_MS worth of heartbeats now synthesizes the exact
// same connection_status("disconnected") packet a real Electron
// disconnect would, through the same "backend-packet" pipe — so
// webui/components/topbar/topbar.js's existing handler (which already
// renders "Disconnected" / toasts "Backend connection lost...") picks
// it up with zero changes of its own.
// -----------------------------------------------------------
const HEARTBEAT_TIMEOUT_MS = 30000; // 3 missed 10s heartbeats
const HEARTBEAT_CHECK_INTERVAL_MS = 5000;

function unifiedLog(subsystem, level, message, context = {}) {
  try {
    fetch(LOG_SERVER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subsystem, level: level || "INFO", message, context }),
    }).catch(() => {
      // Log server may not be up yet, may be down, or the network
      // hiccuped — logging must never break the UI.
    });
  } catch (_err) {
    // Swallow — logging must never break runtime.
  }
}

bridgeLog("=== BRIDGE MODULE LOADED ===");

export class Bridge {
  constructor() {
    console.log("[Bridge] Loaded (Electron IPC mode)");
    bridgeLog("Bridge constructed (Electron IPC mode).");

    this.handlers = {};

    window.ARIA_STATE = window.ARIA_STATE || {
      modelPref: "automatic"
    };

    bridgeLog("ARIA_STATE initialized.");

    // Heartbeat tracking state — see HEARTBEAT_TIMEOUT_MS above.
    // _backendConnected gates send(): while false, ARIA must not pretend
    // a model/mode/tool request went through when the backend may not
    // even be alive to receive it.
    this._lastHeartbeatAt = Date.now();
    this._backendConnected = true;
    this._heartbeatCheckTimer = null;
  }

  // -----------------------------------------------------------
  // Start polling for missed heartbeats. Safe to call multiple times
  // (connect() is the only caller); idempotent.
  // -----------------------------------------------------------
  _startHeartbeatWatch() {
    if (this._heartbeatCheckTimer) return;
    this._lastHeartbeatAt = Date.now();
    this._heartbeatCheckTimer = setInterval(() => {
      const elapsed = Date.now() - this._lastHeartbeatAt;
      if (elapsed > HEARTBEAT_TIMEOUT_MS && this._backendConnected) {
        this._backendConnected = false;
        bridgeLog(`Backend heartbeat timeout (${elapsed}ms since last) — marking disconnected.`);
        unifiedLog("bridge", "ERROR", "Backend heartbeat timeout — marking disconnected", { elapsedMs: elapsed });
        window.dispatchEvent(
          new CustomEvent("backend-packet", {
            detail: { type: IPC.CONNECTION_STATUS, payload: { state: "disconnected", reason: "heartbeat_timeout" } },
          })
        );
      }
    }, HEARTBEAT_CHECK_INTERVAL_MS);
  }

  // -----------------------------------------------------------
  // Called on every incoming `heartbeat` packet. Reconnection is
  // announced the same way disconnection was — through connection_status
  // — so topbar.js's existing "Reconnected to backend." toast fires
  // exactly once, on the real transition, same as a real Electron
  // reconnect.
  // -----------------------------------------------------------
  _onHeartbeat() {
    this._lastHeartbeatAt = Date.now();
    if (!this._backendConnected) {
      this._backendConnected = true;
      bridgeLog("Backend heartbeat resumed — marking connected.");
      window.dispatchEvent(
        new CustomEvent("backend-packet", {
          detail: { type: IPC.CONNECTION_STATUS, payload: { state: "connected" } },
        })
      );
      this._requestFreshTruthOnReconnect();
    }
  }

  // -----------------------------------------------------------
  // Batch 4 — "Frontend reconnection pipeline". Fired on every
  // disconnected -> connected transition, from BOTH reconnect paths
  // this bridge detects (a resumed heartbeat here, and a real
  // Electron-level transport reconnect in connect()'s onConnectionStatus
  // handler below) — a genuinely new WebSocket connection already gets
  // mode_status_result/diagnostics_backend_result pushed unsolicited by
  // backend/websocket/handlers.py's handle() (Batch 4), but a heartbeat-
  // only gap (the SAME underlying connection, just briefly silent) does
  // NOT re-run that bootstrap — so the frontend must proactively ask for
  // fresh truth on its own, every time, rather than assuming the
  // backend already pushed it. Every listener already re-renders
  // atomically from whichever *_result arrives (statusbar.js's
  // refreshStatusFromModeStatusResult(), webui/pages/models/models.js's
  // renderModeStatus()/renderDiagnostics(), webui/pages/debug/debug.js's
  // read-only DebugLog view) — this only needs to ASK, never touch the
  // DOM itself, so no stale/partial state or ghost messages can result
  // from this function specifically.
  // -----------------------------------------------------------
  _requestFreshTruthOnReconnect() {
    bridgeLog("Reconnected — requesting fresh truth packets.");
    unifiedLog("bridge", "INFO", "Reconnected — requesting fresh truth packets");
    for (const type of [
      IPC.MODE_STATUS_REQUEST,
      IPC.DIAGNOSTICS_BACKEND_REQUEST,
      IPC.DIAGNOSTICS_PROVIDERS_REQUEST,
      IPC.DIAGNOSTICS_WEATHER_REQUEST,
      IPC.DIAGNOSTICS_TOOLS_REQUEST,
      IPC.DIAGNOSTICS_MODELS_REQUEST,
    ]) {
      this.send(type, {});
    }
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
      unifiedLog("bridge", "ERROR", "window.aria missing — preload.js not loaded");
      return;
    }

    unifiedLog("bridge", "INFO", "Bridge connected (Electron IPC)");
    this._startHeartbeatWatch();

    // Connection-status IPC (see ARIA-Lite Desktop's main.js/preload.js) —
    // not a backend WebSocket packet, but re-dispatched through the exact
    // same "backend-packet" pipe real ones use, so every existing consumer
    // of that event (ARIA_DISPATCH's fallback fan-out → Topbar.handlePacket,
    // in particular) picks it up with zero changes elsewhere. Feature-
    // detected: a preload.js built before this addition simply won't have
    // onConnectionStatus, and the rest of the bridge works exactly as before.
    if (typeof window.aria.onConnectionStatus === "function") {
      window.aria.onConnectionStatus((status) => {
        bridgeLog("Connection status: " + JSON.stringify(status));
        DebugLog.recordConnection(status?.state || "unknown", status);

        // Real Electron-level transport state takes effect immediately —
        // don't wait out the heartbeat-timeout window (HEARTBEAT_TIMEOUT_MS)
        // to start blocking sends when the socket itself already dropped.
        if (status?.state === "disconnected") {
          this._backendConnected = false;
        } else if (status?.state === "connected") {
          const wasDisconnected = !this._backendConnected;
          this._backendConnected = true;
          this._lastHeartbeatAt = Date.now();
          if (wasDisconnected) {
            this._requestFreshTruthOnReconnect();
          }
        }

        window.dispatchEvent(
          new CustomEvent("backend-packet", {
            detail: { type: IPC.CONNECTION_STATUS, payload: status },
          })
        );
      });
      bridgeLog("onConnectionStatus wired.");
    } else {
      bridgeLog("window.aria.onConnectionStatus not available — connection-status UI will not update.");
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
          unifiedLog("bridge", "ERROR", "Invalid backend JSON: " + err);
          return;
        }
      }

      try {
        const { type, payload } = packet;
        bridgeLog("Dispatching backend packet type: " + type);

        if (type === IPC.HEARTBEAT) {
          this._onHeartbeat();
        }

        // Routing visibility for every packet type this bridge knows
        // about — stream_token is intentionally excluded here (it's
        // logged per-token, at higher detail, by chat.js itself; logging
        // it again here would double every token in the unified log).
        if (type !== IPC.STREAM_TOKEN) {
          unifiedLog("bridge", "DEBUG", "Incoming " + type, { packetType: type });
        }
        // Error packets are flat ({type, message, code, ...}, no
        // "payload" key — see backend/ipc_packet_formats.py) — record the
        // whole packet for those so the Debug panel shows the message,
        // not an empty payload.
        DebugLog.recordReceived(type, payload !== undefined ? payload : packet);

        // -----------------------------------------------------------
        // Copilot-style system_result handling
        // -----------------------------------------------------------
        if (type === "system_result") {
          const mode =
            payload?.result?.mode ||
            payload?.result?.value ||
            "automatic";

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
        unifiedLog("bridge", "ERROR", "Packet error: " + err);
      }
    });
  }

  // -----------------------------------------------------------
  // SEND PACKETS TO BACKEND (Electron IPC)
  // -----------------------------------------------------------
  // Guarantees every outgoing packet has a string "type" — the backend's
  // dispatch key must always be a plain string (see backend/websocket/
  // handlers.py::_dispatch()); an object accidentally passed here first
  // (bridge.send({type: "...", ...}) instead of bridge.send("...", {...}))
  // used to reach it as {"type": {"type": "...", ...}}, an unhashable
  // dict where a string was required. Caught here now, at the one place
  // every send() call funnels through, rather than depending on every
  // call site getting the two-argument shape right on its own.
  send(type, payload = {}) {
    if (typeof type !== "string" || !type) {
      const badType = type;
      console.error("[Bridge] send() called with a non-string type — this is always a caller bug:", badType);
      bridgeLog("ERROR: send() called with non-string type: " + JSON.stringify(badType));
      unifiedLog("bridge", "ERROR", "send() called with non-string type — dropped, not sent", {
        typeValue: badType,
      });
      return;
    }

    // Batch 1 stability fix — the backend has missed its heartbeat
    // window (see HEARTBEAT_TIMEOUT_MS above): it may be hung, crashed,
    // or mid-restart. Sending a chat/model/mode/tool request into that
    // silence would either be lost outright or, worse, get a stale
    // response back once the backend recovers that no longer matches
    // current UI state — either way ARIA must not pretend the request
    // went through. connection_status("disconnected") already told the
    // topbar to show this; blocking here is what actually prevents the
    // "pretend to switch models/modes while backend is down" failure
    // mode instead of just displaying it.
    if (!this._backendConnected) {
      console.warn(`[Bridge] Backend disconnected — dropping ${type} request.`);
      bridgeLog(`BLOCKED (backend disconnected): ${type}`);
      unifiedLog("bridge", "WARNING", "Dropped outgoing request — backend disconnected", { packetType: type });
      if (typeof window.showToast === "function") {
        window.showToast("Backend disconnected — please wait for reconnection.", "error");
      }
      return;
    }

    const packet = { type, payload };
    console.log("[Bridge] SEND (IPC):", packet);
    bridgeLog("SEND → " + JSON.stringify(packet));
    DebugLog.recordSent(type, payload);

    if (type === IPC.CHAT_REQUEST) {
      unifiedLog("bridge", "INFO", "Outgoing chat_request", { payload });
    } else {
      unifiedLog("bridge", "DEBUG", "Outgoing " + type, { packetType: type });
    }

    if (!window.aria) {
      console.error("[Bridge] window.aria missing — cannot send");
      bridgeLog("ERROR: window.aria missing — cannot send.");
      unifiedLog("bridge", "ERROR", "window.aria missing — cannot send " + type);
      return;
    }

    window.aria.sendToBackend(packet);
  }

  // -----------------------------------------------------------
  // BATCHED SEND (opt-in) — queues calls made within one short window
  // into a single batch_request instead of one packet per call. Nothing
  // in this codebase calls this yet; it exists for future rapid-fire UI
  // events (e.g. several model_performance_request calls back to back)
  // that would rather round-trip once. Deliberately separate from send()
  // so every existing call site (including chat_request, which must
  // never be batched) is completely unaffected.
  // -----------------------------------------------------------
  sendBatched(type, payload = {}) {
    if (typeof type !== "string" || !type) {
      console.error("[Bridge] sendBatched() called with a non-string type:", type);
      return;
    }

    if (!this._batchQueue) {
      this._batchQueue = [];
    }
    this._batchQueue.push({ type, payload });

    if (this._batchTimer) {
      return;
    }

    this._batchTimer = setTimeout(() => {
      const requests = this._batchQueue;
      this._batchQueue = [];
      this._batchTimer = null;
      this.send(IPC.BATCH_REQUEST, { requests });
    }, 25);
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
