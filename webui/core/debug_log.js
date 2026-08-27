// core/debug_log.js
//
// Shared ring buffer backing the IPC Debug panel (pages/debug/). Records
// sent/received packets and connection/heartbeat events. Lives at
// module scope (not on a router-loaded panel's own object) specifically so
// history survives navigating away from and back to the Debug page — the
// same reason chat.js keeps _conversationHistory on its singleton rather
// than in per-navigation DOM state (see router.js: every panel navigation
// replaces #panel-container's innerHTML and re-runs that panel's init()).
//
// Recording listeners are registered once, at bootstrap, by core/app.js —
// not by pages/debug/debug.js — so entries keep accumulating even while the
// Debug page isn't the active panel.

const MAX_ENTRIES = 200;

const _entries = [];
const _subscribers = new Set();

function _push(entry) {
  _entries.push({ ...entry, ts: Date.now() });
  if (_entries.length > MAX_ENTRIES) {
    _entries.splice(0, _entries.length - MAX_ENTRIES);
  }
  _subscribers.forEach((fn) => {
    try {
      fn(entry);
    } catch (err) {
      console.error("[DebugLog] subscriber threw:", err);
    }
  });
}

export const DebugLog = {
  recordSent(type, payload) {
    _push({ direction: "sent", type, payload });
  },

  recordReceived(type, payload) {
    _push({ direction: "received", type, payload });
  },

  recordConnection(state, detail = {}) {
    _push({ direction: "connection", type: state, payload: detail });
  },

  // Returns a snapshot (not the live array) so callers can render it
  // without accidentally mutating the buffer.
  getAll() {
    return _entries.slice();
  },

  // Live updates — callback fires once per _push() call. Returns an
  // unsubscribe function.
  subscribe(fn) {
    _subscribers.add(fn);
    return () => _subscribers.delete(fn);
  },

  clear() {
    _entries.length = 0;
  },
};

export default DebugLog;
