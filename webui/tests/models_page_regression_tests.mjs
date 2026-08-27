// webui/tests/models_page_regression_tests.mjs
//
// Regression tests for the Models page's provider key-management cards
// (webui/pages/models/models.js):
//   - buildProviderCards() renders one card per provider with the exact
//     structure requested: <h3>, a password <input id="{name}-key-input">,
//     Save/Delete buttons, and a <div id="{name}-status"> badge.
//   - saveProviderKey/deleteProviderKey send the right IPC packet and
//     clear the input on save.
//   - status badges update from providers_list_result.
//   - the Modules section is gone entirely — module key management moved
//     to webui/pages/modules/modules.js as part of splitting Settings
//     into a Steam-style hub (see webui/tests/modules_page_regression_tests.mjs
//     for that page's own coverage).
//
// IMPORTANT: these functions talk to backend.core.key_manager over the
// same WebSocket IPC channel every other action on this page already
// uses (bridge.send), NOT the REST /api/provider/* endpoint. That REST
// endpoint is real (backend/server.py) and was verified directly against
// a live FastAPI TestClient, but the FastAPI server itself is never
// started by the actual launcher (AriaLauncher/AriaLauncher/
// BackendManager.cs starts logging_server.py and ws_server.py only) — a
// fetch() call to it would hit ERR_CONNECTION_REFUSED in the real
// running app. IPC reaches the exact same backend function and is what's
// actually verified working end-to-end here and in a live browser
// session, so that's the transport this file tests.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/models_page_regression_tests.mjs

import assert from "node:assert/strict";

// Populated by every element's `id` setter below — real DOM auto-tracks
// every element with an id so document.getElementById() finds it the
// instant it's set, even before appendChild(); models.js's key-card
// buttons rely on exactly that (saveProviderKey() etc. look their input
// up by id rather than holding a direct reference to it).
const idRegistry = {};

function makeElement(tag) {
  const el = {
    tagName: tag,
    _children: [],
    _classes: new Set(),
    _listeners: {},
    _innerHTML: "",
    textContent: "",
    value: "",
    type: "",
    _id: "",
    get id() {
      return el._id;
    },
    set id(v) {
      if (el._id) delete idRegistry[el._id];
      el._id = v;
      if (v) idRegistry[v] = el;
    },
    placeholder: "",
    style: {},
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    classList: {
      add: (...c) => c.forEach((x) => el._classes.add(x)),
      remove: (...c) => c.forEach((x) => el._classes.delete(x)),
      toggle: (c, force) => {
        const has = el._classes.has(c);
        const want = force === undefined ? !has : force;
        want ? el._classes.add(c) : el._classes.delete(c);
        return want;
      },
      contains: (c) => el._classes.has(c),
    },
    get innerHTML() {
      return el._innerHTML;
    },
    set innerHTML(v) {
      el._innerHTML = v;
      el._children = [];
    },
    appendChild(child) {
      el._children.push(child);
      child._parent = el;
      return child;
    },
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt, arg) {
      (el._listeners[evt] || []).forEach((cb) => cb(arg));
    },
    querySelector(sel) {
      if (sel === ".key-btn.save") return el._children.find((c) => c._classes?.has("save"));
      if (sel === ".key-btn.delete") return el._children.find((c) => c._classes?.has("delete"));
      return null;
    },
    closest(sel) {
      // Only used here as `input.closest(".provider-card")`
      let node = el;
      const wantClass = sel.replace(".", "");
      while (node) {
        if (node._classes?.has(wantClass)) return node;
        node = node._parent;
      }
      return null;
    },
  };
  return el;
}

const registry = {};
function resetModelsDom() {
  for (const key of Object.keys(idRegistry)) delete idRegistry[key];
  for (const id of [
    "models-installed-grid", "models-available-grid",
    "provider-cards-container",
    "models-refresh-btn", "models-details-mount", "models-details-placeholder",
    "models-mode-chip", "models-active-model-chip", "models-override-chip",
  ]) {
    const el = makeElement("div");
    el.id = id;
    registry[id] = el;
  }
}
resetModelsDom();

global.document = {
  getElementById: (id) => idRegistry[id] || registry[id] || null,
  createElement: (tag) => makeElement(tag),
};
global.window = global;

let sentPackets = [];
global.window.aria = { sendToBackend: (p) => sentPackets.push(p), log: () => {} };

const backendPacketListeners = [];
global.window.addEventListener = function (evt, cb) {
  if (evt === "backend-packet") backendPacketListeners.push(cb);
};
function dispatchBackendPacket(packet) {
  backendPacketListeners.forEach((cb) => cb({ detail: packet }));
}

const { default: Models } = await import("../pages/models/models.js");

function freshModels() {
  sentPackets = [];
  backendPacketListeners.length = 0;
  resetModelsDom();
  Models.cache();
  Models.bindBridge();
  Models.buildProviderCards();
  return Models;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("buildProviderCards renders one card per provider with the required structure", () => {
  const m = freshModels();
  const cards = registry["provider-cards-container"]._children;
  assert.equal(cards.length, 14);

  const openaiCard = cards.find((c) =>
    c._children.some((child) => child.tagName === "h3" && child.textContent === "openai")
  );
  assert.ok(openaiCard, "expected an openai card");

  const input = registry["openai-key-input"] === undefined
    ? openaiCard._children.find((c) => c.tagName === "input")
    : null;
  assert.ok(input);
  assert.equal(input.type, "password");
  assert.equal(input.id, "openai-key-input");
  assert.ok(input._classes.has("key-input"));

  const saveBtn = openaiCard._children.find((c) => c._classes.has("save"));
  const deleteBtn = openaiCard._children.find((c) => c._classes.has("delete"));
  assert.ok(saveBtn && saveBtn.textContent === "Save / Overwrite");
  assert.ok(deleteBtn && deleteBtn.textContent === "Delete");

  const status = openaiCard._children.find((c) => c.id === "openai-status");
  assert.ok(status && status._classes.has("status-badge"));
});

test("Models.js has no module-card building or module-key logic left", () => {
  const m = freshModels();
  assert.equal(typeof m.buildModuleCards, "undefined", "buildModuleCards must be fully removed");
  assert.equal(typeof m.saveModuleKey, "undefined", "saveModuleKey must be fully removed");
  assert.equal(typeof m.deleteModuleKey, "undefined", "deleteModuleKey must be fully removed");
  assert.equal(typeof m.refreshModuleStatus, "undefined", "refreshModuleStatus must be fully removed");
  assert.equal(typeof m.setModuleStatusBadge, "undefined", "setModuleStatusBadge must be fully removed");
  assert.equal(m.moduleCardsContainer, undefined, "moduleCardsContainer must no longer be cached");
});

test("saveProviderKey sends provider_key_set_request and clears the input", () => {
  const m = freshModels();
  const input = m.providerCardsContainer._children
    .find((c) => c._children.some((ch) => ch.tagName === "h3" && ch.textContent === "anthropic"))
    ._children.find((c) => c.tagName === "input");
  input.value = "sk-anthropic-test";

  m.saveProviderKey("anthropic");

  assert.deepEqual(sentPackets, [
    { type: "provider_key_set_request", payload: { provider: "anthropic", api_key: "sk-anthropic-test" } },
  ]);
  assert.equal(input.value, "");
});

test("saveProviderKey with an empty input does nothing", () => {
  const m = freshModels();
  m.saveProviderKey("openai");
  assert.deepEqual(sentPackets, []);
});

test("deleteProviderKey sends provider_key_delete_request", () => {
  const m = freshModels();
  m.deleteProviderKey("openai");
  assert.deepEqual(sentPackets, [{ type: "provider_key_delete_request", payload: { provider: "openai" } }]);
});

test("no modules_list_request/module_key_* packets are ever sent from the Models page", () => {
  const m = freshModels();
  dispatchBackendPacket({ type: "modules_list_result", payload: { modules: [{ name: "weather", configured: true }] } });
  dispatchBackendPacket({ type: "module_key_set_result", payload: { ok: true, module: "weather" } });
  dispatchBackendPacket({ type: "module_key_delete_result", payload: { ok: true, module: "weather" } });
  // These packet types must be silently ignored (no handler for them
  // anymore) rather than causing a crash or an unexpected side effect.
  assert.deepEqual(sentPackets, []);
});

test("providers_list_result updates only the matching status badges", () => {
  const m = freshModels();
  dispatchBackendPacket({
    type: "providers_list_result",
    payload: { providers: [{ name: "openai", configured: true }, { name: "grok", configured: false }] },
  });

  const openaiCard = registry["provider-cards-container"]._children.find((c) =>
    c._children.some((ch) => ch.tagName === "h3" && ch.textContent === "openai")
  );
  const openaiStatus = openaiCard._children.find((c) => c.id === "openai-status");
  assert.equal(openaiStatus.textContent, "Connected");
  assert.ok(openaiStatus._classes.has("connected"));

  const grokCard = registry["provider-cards-container"]._children.find((c) =>
    c._children.some((ch) => ch.tagName === "h3" && ch.textContent === "grok")
  );
  const grokStatus = grokCard._children.find((c) => c.id === "grok-status");
  assert.equal(grokStatus.textContent, "Not configured");
  assert.ok(!grokStatus._classes.has("connected"));
});

test("provider_key_set_result triggers a status re-fetch", () => {
  const m = freshModels();
  sentPackets = [];
  dispatchBackendPacket({ type: "provider_key_set_result", payload: { ok: true, provider: "openai" } });
  assert.deepEqual(sentPackets, [{ type: "providers_list_request", payload: {} }]);
});

test("mode/active-model/override chips are unaffected by the new card system", () => {
  const m = freshModels();
  m.renderModeStatus({ routing_mode: "local", active_model_id: "mistral-7b-q4km", explicit_model_override: null });

  assert.equal(registry["models-mode-chip"].textContent, "Mode: Local");
  assert.equal(registry["models-active-model-chip"].textContent, "Active: mistral-7b-q4km");
  assert.equal(registry["models-override-chip"].style.display, "none");

  m.renderModeStatus({ routing_mode: "cloud", active_model_id: null, explicit_model_override: "qwen2.5-0.5b" });
  assert.equal(registry["models-override-chip"].textContent, "Pinned: qwen2.5-0.5b");
  assert.equal(registry["models-override-chip"].style.display, "");
});

test("init() sends exactly one providers_list_request, not one per provider", async () => {
  // FIXED: init() used to ALSO fire refreshProviderStatus() once PER
  // provider on top of the one providers_list_request requestList()
  // already sends — 14 redundant, identical in-flight requests on every
  // page load, whose full-snapshot responses could arrive out of order
  // and let a stale "not configured" answer clobber a just-saved key's
  // correct status. Regression-tests the fix directly: exactly one
  // request. (Module requests no longer exist at all on this page — see
  // "no modules_list_request/module_key_* packets" above and
  // webui/tests/modules_page_regression_tests.mjs for that page's own
  // init() coverage.)
  resetModelsDom();
  backendPacketListeners.length = 0;
  sentPackets = [];
  // mountDetails() does a real fetch() this Node harness doesn't stub —
  // it's wrapped in its own try/catch in models.js and only logs on
  // failure, so init() still completes and still sends its IPC requests.
  await Models.init();

  const providerRequests = sentPackets.filter((p) => p.type === "providers_list_request");
  const moduleRequests = sentPackets.filter((p) => p.type === "modules_list_request");
  assert.equal(providerRequests.length, 1, `expected exactly 1 providers_list_request, got ${providerRequests.length}`);
  assert.equal(moduleRequests.length, 0, "Models page must never send modules_list_request");
});

// ============================================================
let failures = 0;
for (const { name, fn } of tests) {
  try {
    await fn();
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
