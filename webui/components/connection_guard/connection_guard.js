// components/connection_guard/connection_guard.js
//
// Batch 2.5 — "Prevent UI Lies During Backend Disconnect". Batch 1 added
// the heartbeat watchdog and made core/bridge.js's send() drop every
// outgoing packet while disconnected (so the backend can never be lied
// to about a switch that didn't actually happen); this module is the
// other half — making the UI itself visibly reflect that a send would
// be dropped, instead of leaving controls looking clickable while
// silently doing nothing.
//
// There is no dedicated "mode switch" button anywhere in this app —
// mode/model switching happens by sending a chat message ("switch to
// cloud mode", "use mistral"), so the chat input + send button ARE the
// mode-switch controls in practice. This module disables them (and
// swaps the input's placeholder to say so) for as long as the backend
// is unreachable, and restores them the instant it's back.
//
// Deliberately independent of webui/components/chat/chat.js's own
// logic — it only ever touches #chat-input/#chat-send-btn's `disabled`
// attribute and placeholder text from the OUTSIDE, via plain
// getElementById lookups made fresh on every event (never cached),
// because chat.html is a router-loaded panel: #chat-input doesn't exist
// at all until the Chat panel is mounted, and router.js replaces
// #panel-container's contents (destroying and recreating the element)
// on every navigation — see webui/core/router.js. Chat.js calls
// applyCurrentState() once from its own _completeInit() so a
// mid-disconnect navigation TO chat starts already disabled, rather
// than waiting for the next connection_status event.

import { IPC } from "../../core/ipc_schema.js";

const DISCONNECTED_PLACEHOLDER = "Backend disconnected — reconnecting…";

function connectionGuardLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ConnectionGuard", msg);
    }
  } catch (err) {
    console.error("[ConnectionGuard LOG ERROR]", err);
  }
}

const ConnectionGuard = {
  // Tri-state, same convention as statusbar.js: true (connected), false
  // (disconnected), null (no connection_status seen yet — never treated
  // as disconnected).
  _connected: null,
  _originalPlaceholder: null,

  init() {
    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet || packet.type !== IPC.CONNECTION_STATUS) return;

      const state = packet.payload?.state || "unknown";
      if (state === "disconnected") {
        this._connected = false;
      } else if (state === "connected") {
        this._connected = true;
      } else {
        return; // "reconnecting" — chat stays disabled, nothing new to apply
      }
      this.applyCurrentState();
    });

    connectionGuardLog("ConnectionGuard ready.");
  },

  // Re-applies whatever the last known connection state was to whatever
  // chat input/send button currently exist in the DOM. Safe to call any
  // time, including before any connection_status has ever arrived
  // (no-op — _connected is still null) and while the Chat panel isn't
  // even mounted (getElementById just returns null).
  applyCurrentState() {
    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send-btn");

    if (this._connected === false) {
      if (input) {
        input.disabled = true;
        if (this._originalPlaceholder === null) {
          this._originalPlaceholder = input.placeholder;
        }
        input.placeholder = DISCONNECTED_PLACEHOLDER;
      }
      if (sendBtn) sendBtn.disabled = true;
      return;
    }

    // Connected (or unknown — the default, unconstrained state).
    if (input) {
      input.disabled = false;
      if (this._originalPlaceholder !== null) {
        input.placeholder = this._originalPlaceholder;
      }
    }
    if (sendBtn) sendBtn.disabled = false;
  },

  isConnected() {
    return this._connected;
  },
};

export default ConnectionGuard;
connectionGuardLog("ConnectionGuard module loaded.");
