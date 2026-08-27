// webui/tests/status_truth_alignment_tests.mjs
//
// Batch 2.5 — "UI Status Bar & Model Display Truth Alignment" regression
// tests. Covers:
//   - Cloud Mode -> status bar shows the cloud model AND the provider
//   - Local Mode -> status bar shows the local model
//   - Switching modes updates the status bar atomically (label/class/
//     display-name never disagree with each other mid-update)
//   - Backend disconnect -> status bar shows "Backend disconnected" and
//     the chat input/send button (this app's de facto mode-switch
//     controls — see components/connection_guard/connection_guard.js)
//     are disabled
//   - Reconnect -> the status bar re-requests mode_status_result and
//     refreshes from it, never showing anything stale
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/status_truth_alignment_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    _classes: new Set(),
    textContent: "",
    disabled: false,
    placeholder: "",
    style: {},
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    addEventListener: () => {},
  };
  return el;
}

const registry = {};
function resetDom() {
  registry["active-model-status"] = makeElement("div");
  registry["auto-balance-chip"] = makeElement("button");
  registry["auto-balance-chip"].addEventListener = () => {};
  registry["chat-input"] = makeElement("textarea");
  registry["chat-input"].placeholder = "Type a message...";
  registry["chat-send-btn"] = makeElement("button");
}
resetDom();

global.document = {
  getElementById: (id) => registry[id] || null,
};
global.window = global;

let sentPackets = [];
global.window.aria = { sendToBackend: (p) => sentPackets.push(p), log: () => {} };

const backendPacketListeners = [];
global.window.addEventListener = function (evt, cb) {
  if (evt === "backend-packet") backendPacketListeners.push(cb);
};
global.window.dispatchEvent = (evt) => {
  if (evt?.type === "backend-packet") {
    backendPacketListeners.forEach((cb) => cb(evt));
  }
};
// CustomEvent isn't global in plain Node — a minimal stand-in is enough
// for the modules under test, which only read `.detail`.
global.CustomEvent = class {
  constructor(type, init) {
    this.type = type;
    this.detail = init?.detail;
  }
};

function dispatchBackendPacket(packet) {
  backendPacketListeners.forEach((cb) => cb({ detail: packet }));
}

const { default: Statusbar } = await import("../components/statusbar/statusbar.js");
const { default: ConnectionGuard } = await import("../components/connection_guard/connection_guard.js");

function freshStatusbar() {
  sentPackets = [];
  backendPacketListeners.length = 0;
  resetDom();
  Statusbar.init();
  ConnectionGuard.init();
  sentPackets = []; // drop init()'s own mode_status_request
  return Statusbar;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

// ============================================================
// PART 1 — Cloud Mode shows the cloud model + provider
// ============================================================
test("Cloud Mode: status bar shows the real cloud model name and the provider", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "anthropic", cloud_provider_display_name: "Anthropic",
      active_model_id: "claude-3-opus", active_model_display_name: "Claude 3 Opus",
      explicit_model_override: null, location: "cloud",
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: Claude 3 Opus (Anthropic · Cloud)");
  assert.ok(el._classes.has("model-cloud"));
  assert.equal(Statusbar.getActiveModelLocation(), "cloud");
  assert.equal(Statusbar.getActiveModelDisplayName(), "Claude 3 Opus");
});

// ============================================================
// PART 2 — Local Mode shows the local model
// ============================================================
test("Local Mode: status bar shows the local model's display name", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", cloud_provider: null, cloud_provider_display_name: null,
      active_model_id: "nemo-12b-q5", active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)",
      explicit_model_override: null, location: "local",
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: Mistral Nemo 12B Instruct (Q5_K_M) (Local)");
  assert.ok(el._classes.has("model-local"));
  assert.equal(Statusbar.getActiveModelLocation(), "local");
});

// ============================================================
// PART 3 — Switching modes updates the status bar atomically
// ============================================================
test("switching from Local to Cloud never leaves a mixed label/class pair", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", active_model_id: "nemo-12b-q5",
      active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)", location: "local",
    },
  });
  assert.ok(registry["active-model-status"]._classes.has("model-local"));

  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "openai", cloud_provider_display_name: "OpenAI",
      active_model_id: "gpt-4", active_model_display_name: "GPT-4", location: "cloud",
    },
  });
  const el = registry["active-model-status"];
  // The new label and the new class must always agree — refreshStatusFromModeStatusResult()
  // computes the full next state before calling _apply() once, so there
  // is no observable point where the label says "Cloud" and the class
  // still says "model-local" (or vice versa).
  assert.equal(el.textContent, "Model: GPT-4 (OpenAI · Cloud)");
  assert.ok(el._classes.has("model-cloud"));
  assert.ok(!el._classes.has("model-local"), "stale model-local class must not survive the switch");
});

test("refreshStatusFromModeStatusResult is the single atomic entry point — direct calls behave identically to packet dispatch", () => {
  freshStatusbar();
  Statusbar.refreshStatusFromModeStatusResult({
    routing_mode: "automatic", active_model_id: "nemo-12b-q5",
    active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)", location: null,
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: Mistral Nemo 12B Instruct (Q5_K_M) (Automatic)");
  assert.ok(el._classes.has("model-automatic"));
});

// ============================================================
// PART 4 — Backend disconnect: "Backend disconnected" + disabled controls
// ============================================================
test("backend disconnect: status bar shows 'Backend disconnected' and never a stale model", () => {
  const sb = freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", active_model_id: "nemo-12b-q5",
      active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)", location: "local",
    },
  });
  assert.equal(registry["active-model-status"].textContent, "Model: Mistral Nemo 12B Instruct (Q5_K_M) (Local)");

  dispatchBackendPacket({ type: "connection_status", payload: { state: "disconnected" } });
  const el = registry["active-model-status"];
  assert.match(el.textContent, /Backend disconnected/);
  assert.match(el.textContent, /Unavailable/);
  assert.ok(el._classes.has("model-disconnected"));

  // A late-arriving (stale) mode_status_result must not overwrite the
  // disconnected display — it can only have described a state the
  // backend held before it went unreachable.
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", active_model_id: "nemo-12b-q5",
      active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)", location: "local",
    },
  });
  assert.match(el.textContent, /Backend disconnected/, "a stale mode_status_result must not overwrite the disconnected display");
});

test("backend disconnect disables the chat input and send button (this app's de facto mode-switch controls)", () => {
  freshStatusbar();
  dispatchBackendPacket({ type: "connection_status", payload: { state: "disconnected" } });

  assert.equal(registry["chat-input"].disabled, true);
  assert.equal(registry["chat-send-btn"].disabled, true);
  assert.match(registry["chat-input"].placeholder, /disconnected/i);
});

// ============================================================
// PART 5 — Reconnect: UI refreshes from backend truth
// ============================================================
test("reconnect: status bar re-requests mode_status_result and re-enables chat controls", () => {
  freshStatusbar();
  dispatchBackendPacket({ type: "connection_status", payload: { state: "disconnected" } });
  assert.equal(registry["chat-input"].disabled, true);

  sentPackets = [];
  dispatchBackendPacket({ type: "connection_status", payload: { state: "connected" } });

  assert.ok(
    sentPackets.some((p) => p.type === "mode_status_request"),
    "reconnecting must trigger a fresh mode_status_request rather than trusting any cached value"
  );
  assert.equal(registry["chat-input"].disabled, false);
  assert.equal(registry["chat-send-btn"].disabled, false);
  assert.equal(registry["chat-input"].placeholder, "Type a message...", "the original placeholder must be restored");
});

test("reconnect: the status bar instantly reflects the first mode_status_result that arrives", () => {
  freshStatusbar();
  dispatchBackendPacket({ type: "connection_status", payload: { state: "disconnected" } });
  dispatchBackendPacket({ type: "connection_status", payload: { state: "connected" } });

  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "openai", cloud_provider_display_name: "OpenAI",
      active_model_id: "gpt-4", active_model_display_name: "GPT-4", location: "cloud",
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: GPT-4 (OpenAI · Cloud)");
  assert.ok(el._classes.has("model-cloud"));
});

test("a 'connected' state with no prior disconnect does not spam mode_status_request", () => {
  freshStatusbar();
  sentPackets = [];
  dispatchBackendPacket({ type: "connection_status", payload: { state: "connected" } });
  assert.equal(sentPackets.length, 0, "the very first 'connected' notification is not a recovery from anything");
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
