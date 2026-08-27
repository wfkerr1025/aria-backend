// webui/tests/bridge_reconnect_truth_tests.mjs
//
// Batch 4 — "Frontend reconnection pipeline" regression tests for
// core/bridge.js: when the backend transitions from disconnected back
// to connected (either a resumed heartbeat, or a real Electron-level
// transport reconnect), the bridge must proactively request fresh
// mode_status_result / diagnostics_backend_result / diagnostics_
// providers_result / diagnostics_weather_result / diagnostics_tools_
// result / diagnostics_models_result — never assume the backend already
// pushed them (it only does that for a genuinely NEW WebSocket
// connection, not a same-connection heartbeat gap — see backend/
// websocket/handlers.py's handle()).
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/status_truth_alignment_tests.mjs. Run directly:
//
//   node webui/tests/bridge_reconnect_truth_tests.mjs

import assert from "node:assert/strict";

const backendPacketListeners = [];
global.window = global;
global.window.addEventListener = function (evt, cb) {
  if (evt === "backend-packet") backendPacketListeners.push(cb);
};
global.window.dispatchEvent = (evt) => {
  if (evt?.type === "backend-packet" || evt instanceof CustomEventShim) {
    backendPacketListeners.forEach((cb) => cb(evt));
  }
};

// Minimal CustomEvent stand-in — Node has no DOM globals.
class CustomEventShim {
  constructor(type, init) {
    this.type = type;
    this.detail = init?.detail;
  }
}
global.CustomEvent = CustomEventShim;

let sentPackets = [];
global.window.aria = {
  sendToBackend: (p) => sentPackets.push(p),
  log: () => {},
  onConnectionStatus: (cb) => {
    global.window.aria._connectionStatusCb = cb;
  },
  onBackendMessage: () => {},
};

const { bridge } = await import("../core/bridge.js");
const { IPC } = await import("../core/ipc_schema.js");

bridge.connect();
// connect() starts a real setInterval (_startHeartbeatWatch) that would
// otherwise keep this Node process alive forever — none of these tests
// rely on it actually firing (they drive _onHeartbeat()/_backendConnected
// directly), so it's cleared immediately after wiring onConnectionStatus.
if (bridge._heartbeatCheckTimer) {
  clearInterval(bridge._heartbeatCheckTimer);
  bridge._heartbeatCheckTimer = null;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

function reset() {
  sentPackets = [];
  bridge._backendConnected = true;
  bridge._lastHeartbeatAt = Date.now();
}

test("heartbeat resuming after a disconnect requests every fresh-truth packet type", () => {
  reset();
  bridge._backendConnected = false; // simulate the prior heartbeat-timeout state

  bridge._onHeartbeat();

  const types = sentPackets.map((p) => p.type);
  for (const expected of [
    IPC.MODE_STATUS_REQUEST,
    IPC.DIAGNOSTICS_BACKEND_REQUEST,
    IPC.DIAGNOSTICS_PROVIDERS_REQUEST,
    IPC.DIAGNOSTICS_WEATHER_REQUEST,
    IPC.DIAGNOSTICS_TOOLS_REQUEST,
    IPC.DIAGNOSTICS_MODELS_REQUEST,
  ]) {
    assert.ok(types.includes(expected), `expected ${expected} to be requested on reconnect, got ${JSON.stringify(types)}`);
  }
});

test("an ordinary heartbeat while already connected does not re-request truth (no spam)", () => {
  reset();
  bridge._onHeartbeat();
  assert.equal(sentPackets.length, 0, "a heartbeat that isn't a reconnect must not trigger any requests");
});

test("real Electron-level reconnect (onConnectionStatus) also requests fresh truth", () => {
  reset();
  bridge._backendConnected = false;

  global.window.aria._connectionStatusCb({ state: "connected" });

  const types = sentPackets.map((p) => p.type);
  assert.ok(types.includes(IPC.MODE_STATUS_REQUEST), types);
  assert.ok(types.includes(IPC.DIAGNOSTICS_BACKEND_REQUEST), types);
});

test("real Electron-level 'connected' with no prior disconnect does not spam requests", () => {
  reset();
  global.window.aria._connectionStatusCb({ state: "connected" });
  assert.equal(sentPackets.length, 0);
});

test("send() is blocked while disconnected, and unblocked again the instant reconnect fires", () => {
  reset();
  bridge._backendConnected = false;

  bridge.send(IPC.CHAT_REQUEST, { messages: [] });
  assert.equal(sentPackets.length, 0, "must not send while disconnected");

  bridge._onHeartbeat(); // reconnect
  sentPackets = []; // drop the reconnect truth-refresh packets for this assertion
  bridge.send(IPC.CHAT_REQUEST, { messages: [] });
  assert.equal(sentPackets.length, 1, "must send normally again once reconnected");
});

// ============================================================
let failures = 0;
for (const { name, fn } of tests) {
  try {
    fn();
    console.log(`PASS  ${name}`);
  } catch (err) {
    failures += 1;
    console.log(`FAIL  ${name}: ${err.message}`);
  }
}

console.log("");
if (failures) {
  console.log(`${failures} FAILED`);
  process.exit(1);
}
console.log("All tests passed.");
