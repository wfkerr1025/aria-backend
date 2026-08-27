// webui/tests/cloud_llms_regression_tests.mjs
//
// Regression tests for the Cloud LLMs Settings subpage
// (webui/pages/cloud_llms/cloud_llms.js) — set/clear a provider key and
// confirm the UI reflects providers_list_result status updates. This is
// the exact same logic the old single-page Settings panel
// (webui/components/settings/settings.js) had for API keys; it moved
// here unchanged as part of splitting Settings into a Steam-style hub
// with dedicated subpages (see webui/pages/settings_home/).
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/frontend_regression_tests.mjs. Run directly:
//
//   node webui/tests/cloud_llms_regression_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    _children: [],
    _classes: new Set(),
    _listeners: {},
    _innerHTML: "",
    textContent: "",
    value: "",
    disabled: false,
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
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
      return child;
    },
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt, arg) {
      (el._listeners[evt] || []).forEach((cb) => cb(arg));
    },
    querySelector(sel) {
      if (sel === "label") return el._children.find((c) => c.tagName === "label") || null;
      if (sel === "input") return el._children.find((c) => c.tagName === "input") || null;
      if (sel === "button") return el._children.find((c) => c.tagName === "button") || null;
      return null;
    },
  };
  return el;
}

const registry = {};
function resetCloudLlmsDom() {
  registry["api-keys-list"] = makeElement("div");
}
resetCloudLlmsDom();

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

const { default: CloudLlms } = await import("../pages/cloud_llms/cloud_llms.js");

function freshCloudLlms() {
  sentPackets = [];
  backendPacketListeners.length = 0;
  resetCloudLlmsDom();
  CloudLlms.init();
  return CloudLlms;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("CloudLlms.init() requests the providers list on load", () => {
  freshCloudLlms();
  const types = sentPackets.map((p) => p.type);
  assert.ok(types.includes("providers_list_request"));
});

test("providers_list_result renders a row per provider with correct configured state", () => {
  freshCloudLlms();
  dispatchBackendPacket({
    type: "providers_list_result",
    payload: { providers: [
      { name: "openai", configured: false },
      { name: "anthropic", configured: true },
    ]},
  });

  const rows = registry["api-keys-list"]._children;
  assert.equal(rows.length, 2);

  const anthropicRow = rows.find((r) => r.querySelector("label").textContent === "anthropic");
  assert.ok(anthropicRow.querySelector("label")._classes.has("configured"));
  assert.equal(anthropicRow.querySelector("button").disabled, false, "Remove must be enabled when configured");

  const openaiRow = rows.find((r) => r.querySelector("label").textContent === "openai");
  assert.ok(!openaiRow.querySelector("label")._classes.has("configured"));
});

test("saving a provider key sends provider_key_set_request with the entered value", () => {
  freshCloudLlms();
  dispatchBackendPacket({
    type: "providers_list_result",
    payload: { providers: [{ name: "openai", configured: false }] },
  });

  sentPackets = [];
  const row = registry["api-keys-list"]._children[0];
  row.querySelector("input").value = "sk-test-from-ui";
  row.querySelector("button").dispatch("click"); // Save is the first button

  assert.deepEqual(sentPackets, [
    { type: "provider_key_set_request", payload: { provider: "openai", api_key: "sk-test-from-ui" } },
  ]);
});

test("removing a provider key sends provider_key_delete_request", () => {
  freshCloudLlms();
  dispatchBackendPacket({
    type: "providers_list_result",
    payload: { providers: [{ name: "anthropic", configured: true }] },
  });

  sentPackets = [];
  const row = registry["api-keys-list"]._children[0];
  const removeBtn = row._children.find((c) => c.tagName === "button" && c.textContent === "Remove");
  removeBtn.dispatch("click");

  assert.deepEqual(sentPackets, [
    { type: "provider_key_delete_request", payload: { provider: "anthropic" } },
  ]);
});

test("saving/deleting a provider key triggers a re-fetch of the providers list", () => {
  freshCloudLlms();
  sentPackets = [];
  dispatchBackendPacket({ type: "provider_key_set_result", payload: { ok: true, provider: "openai" } });
  assert.deepEqual(sentPackets, [{ type: "providers_list_request", payload: {} }]);

  sentPackets = [];
  dispatchBackendPacket({ type: "provider_key_delete_result", payload: { ok: true, provider: "openai" } });
  assert.deepEqual(sentPackets, [{ type: "providers_list_request", payload: {} }]);
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
