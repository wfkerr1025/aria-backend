// webui/tests/local_models_regression_tests.mjs
//
// Regression tests for the Local Models Settings subpage
// (webui/pages/local_models/local_models.js):
//   - shows the models folder path, derived from an installed local
//     model's own `path` field (no dedicated backend endpoint exists for
//     this — see local_models.js's module docstring for why)
//   - lists installed local models with name/size/metadata
//   - shows an (estimated) disk usage total
//   - shows a compatibility warning when a model fails requirements
//   - filters out cloud-provider entries and anything not installed —
//     this page is local-only
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/local_models_regression_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    _children: [],
    _innerHTML: "",
    textContent: "",
    dataset: {},
    _listeners: {},
    get innerHTML() {
      return el._innerHTML;
    },
    set innerHTML(v) {
      el._innerHTML = v;
      el._children = [];
    },
    appendChild(child) {
      el._children.push(child);
      return child;
    },
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt, arg) {
      (el._listeners[evt] || []).forEach((cb) => cb(arg));
    },
  };
  return el;
}

const registry = {};
function resetLocalModelsDom() {
  registry["local-models-grid"] = makeElement("div");
  registry["local-models-folder-path"] = makeElement("code");
  registry["local-models-disk-usage"] = makeElement("div");
  registry["local-models-refresh-btn"] = makeElement("button");
}
resetLocalModelsDom();

global.document = {
  getElementById: (id) => registry[id] || null,
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

const { default: LocalModels } = await import("../pages/local_models/local_models.js");

function freshLocalModels() {
  sentPackets = [];
  // Deliberately NOT clearing backendPacketListeners here: init() now
  // binds its window-level "backend-packet" listener at most ONCE ever
  // (see local_models.js's _bridgeBound guard) — the real bug that
  // guard fixes is router.js destroying/recreating this page's DOM on
  // every navigation while init() kept re-adding ANOTHER permanent
  // window listener each time, so every packet fired the handler once
  // per past visit. Clearing the array here would hide exactly that
  // regression by always re-registering a "fresh" listener per test.
  // cache()-equivalent element lookups (this.grid, etc.) DO need to
  // re-run every call — resetLocalModelsDom() + init() below still
  // does that; only the listener bind is one-time.
  resetLocalModelsDom();
  LocalModels.init();
  return LocalModels;
}

const WINDOWS_PATH = "C:\\Users\\test\\.aria-lite\\models\\Mistral-Nemo-12B-Instruct-2407-Q5_K_M.gguf";

function localModelEntry(overrides = {}) {
  const { model_cfg: modelCfgOverrides, ...restOverrides } = overrides;
  return {
    model_cfg: {
      id: "nemo-12b-q5",
      name: "Mistral Nemo 12B Instruct (Q5_K_M)",
      provider: "local",
      path: WINDOWS_PATH,
      quant: "Q5_K_M",
      params: 12_000_000_000,
      maxContext: 16384,
      arch: "llama",
      ...modelCfgOverrides,
    },
    installed: true,
    is_active: false,
    is_fallback: false,
    is_emergency: false,
    compat: { meets_minimum: true, meets_recommended: true },
    projected_speed_toksec: 5,
    ...restOverrides,
  };
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("LocalModels.init() requests the models list on load", () => {
  freshLocalModels();
  assert.deepEqual(sentPackets, [{ type: "models_list_request", payload: {} }]);
});

test("with no models installed, the folder path and disk usage show honest empty states", () => {
  freshLocalModels();
  dispatchBackendPacket({ type: "models_list_result", payload: { models: [] } });

  assert.match(registry["local-models-folder-path"].textContent, /No local models installed/);
  assert.equal(registry["local-models-disk-usage"].textContent, "Disk usage: —");
  assert.match(registry["local-models-grid"].innerHTML, /No local models installed/);
});

test("cloud-provider and not-installed entries are filtered out — this page is local-only", () => {
  freshLocalModels();
  dispatchBackendPacket({
    type: "models_list_result",
    payload: { models: [
      localModelEntry(),
      { model_cfg: { id: "gpt-4", provider: "openai" }, installed: true, compat: {} },
      { model_cfg: { id: "not-installed-local", provider: "local" }, installed: false, compat: {} },
    ]},
  });

  assert.equal(registry["local-models-grid"]._children.length, 1);
});

test("the folder path is derived from an installed local model's own path", () => {
  freshLocalModels();
  dispatchBackendPacket({ type: "models_list_result", payload: { models: [localModelEntry()] } });

  assert.equal(registry["local-models-folder-path"].textContent, "C:\\Users\\test\\.aria-lite\\models");
});

test("installed local models render with name, metadata, and an estimated size", () => {
  freshLocalModels();
  dispatchBackendPacket({ type: "models_list_result", payload: { models: [localModelEntry()] } });

  const card = registry["local-models-grid"]._children[0];
  assert.match(card.innerHTML, /Mistral Nemo 12B Instruct \(Q5_K_M\)/);
  assert.match(card.innerHTML, /Q5_K_M/);
  assert.match(card.innerHTML, /12\.0B params/);
  assert.match(card.innerHTML, /Size \(estimated\):/);
});

test("disk usage totals across all installed local models and is clearly labeled as estimated", () => {
  freshLocalModels();
  dispatchBackendPacket({
    type: "models_list_result",
    payload: { models: [localModelEntry(), localModelEntry({ model_cfg: { id: "phi-3-mini", params: 3_800_000_000, quant: "Q4_K_M" } })] },
  });

  assert.match(registry["local-models-disk-usage"].textContent, /Disk usage \(estimated\)/);
  assert.match(registry["local-models-disk-usage"].textContent, /2 model\(s\)/);
});

test("a model that fails minimum requirements shows a compatibility warning", () => {
  freshLocalModels();
  dispatchBackendPacket({
    type: "models_list_result",
    payload: { models: [localModelEntry({ compat: { meets_minimum: false, meets_recommended: false } })] },
  });

  const card = registry["local-models-grid"]._children[0];
  assert.match(card.innerHTML, /model-card-warning/);
  assert.match(card.innerHTML, /Does not meet minimum requirements/);
});

test("a model that meets minimum but not recommended requirements shows a softer warning", () => {
  freshLocalModels();
  dispatchBackendPacket({
    type: "models_list_result",
    payload: { models: [localModelEntry({ compat: { meets_minimum: true, meets_recommended: false } })] },
  });

  const card = registry["local-models-grid"]._children[0];
  assert.match(card.innerHTML, /Below recommended requirements/);
});

test("a fully compatible model shows no warning at all", () => {
  freshLocalModels();
  dispatchBackendPacket({ type: "models_list_result", payload: { models: [localModelEntry()] } });

  const card = registry["local-models-grid"]._children[0];
  assert.doesNotMatch(card.innerHTML, /model-card-warning/);
});

test("the active model badge is shown when is_active is true", () => {
  freshLocalModels();
  dispatchBackendPacket({ type: "models_list_result", payload: { models: [localModelEntry({ is_active: true })] } });

  const card = registry["local-models-grid"]._children[0];
  assert.match(card.innerHTML, /★ Main/);
});

test("the manual refresh button re-requests the models list", () => {
  const m = freshLocalModels();
  sentPackets = [];
  registry["local-models-refresh-btn"].dispatch("click");
  assert.deepEqual(sentPackets, [{ type: "models_list_request", payload: {} }]);
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
