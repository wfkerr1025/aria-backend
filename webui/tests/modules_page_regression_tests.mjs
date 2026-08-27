// webui/tests/modules_page_regression_tests.mjs
//
// Regression tests for the Modules Settings subpage
// (webui/pages/modules/modules.js) — built from the RealModuleRow /
// AddNewModuleRow split (webui/components/module_row/module_row.js,
// see webui/tests/module_row_regression_tests.mjs for that component's
// own unit tests). This page's logic is the exact same module-key
// management the old single-page Settings panel
// (webui/components/settings/settings.js) had; it moved here unchanged
// as part of splitting Settings into a Steam-style hub with dedicated
// subpages (see webui/pages/settings_home/). Not to be confused with
// webui/tests/models_page_regression_tests.mjs, which covers the
// separate Models page (webui/pages/models/models.js) — that page no
// longer manages module keys at all.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/frontend_regression_tests.mjs. Run directly:
//
//   node webui/tests/modules_page_regression_tests.mjs

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
function resetModulesDom() {
  registry["module-keys-list"] = makeElement("div");
  registry["module-add-container"] = makeElement("div");
}
resetModulesDom();

// AddNewModuleRow is mounted once into #module-add-container as a single
// child (see modules.js's init()); its own children are, in order:
// [header, nameInput, keyInput, addBtn] — see
// webui/components/module_row/module_row.js.
function addModuleRowFields() {
  const mounted = registry["module-add-container"]._children[0];
  const [, nameInput, keyInput, addBtn] = mounted._children;
  return { mounted, nameInput, keyInput, addBtn };
}

// A RealModuleRow has two buttons (Save, Remove) — find by text rather
// than relying on querySelector("button")'s "first match" behavior.
function findButton(row, text) {
  return row._children.find((c) => c.tagName === "button" && c.textContent === text) || null;
}

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

const { default: ModulesPage } = await import("../pages/modules/modules.js");

function freshModulesPage() {
  sentPackets = [];
  backendPacketListeners.length = 0;
  resetModulesDom();
  ModulesPage.init();
  return ModulesPage;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("ModulesPage.init() requests the modules list on load", () => {
  freshModulesPage();
  const types = sentPackets.map((p) => p.type);
  assert.ok(types.includes("modules_list_request"));
});

test("no static Weather row exists — the module list starts empty and stays empty until the backend responds", () => {
  // Direct regression for the exact bug this page's split fixed: a
  // hard-wired "Weather — Not configured" row rendered above
  // AddNewModuleRow, present even before/without a real
  // modules_list_result. init() must never populate #module-keys-list
  // on its own — only renderModuleKeys(), driven exclusively by a real
  // modules_list_result payload, ever touches it.
  freshModulesPage();
  assert.equal(registry["module-keys-list"]._children.length, 0, "the module list must be empty until the backend actually responds");
});

test("modules_list_result with zero real modules renders an empty list — no leftover/default entries", () => {
  freshModulesPage();
  dispatchBackendPacket({ type: "modules_list_result", payload: { modules: [] } });
  assert.equal(registry["module-keys-list"]._children.length, 0);
});

test("the module list contains exactly what the backend sent — nothing extra, nothing missing, nothing named Weather unless the backend actually said so", () => {
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "custom_tool", configured: false }] },
  });

  const rows = registry["module-keys-list"]._children;
  assert.equal(rows.length, 1, "must render exactly one row for exactly one backend module — no extra hardcoded row");
  assert.equal(rows[0].querySelector("label").textContent, "custom_tool");
  assert.doesNotMatch(
    registry["module-keys-list"]._children.map((r) => r.querySelector("label").textContent).join(","),
    /weather/i,
    "no module named Weather may appear when the backend never sent one"
  );
});

test("adding a module key sends module_key_set_request and clears the inputs", () => {
  freshModulesPage();
  sentPackets = [];

  const { nameInput, keyInput, addBtn } = addModuleRowFields();
  nameInput.value = "weather";
  keyInput.value = "wk-test-123";
  addBtn.dispatch("click");

  assert.deepEqual(sentPackets, [
    { type: "module_key_set_request", payload: { module: "weather", api_key: "wk-test-123" } },
  ]);
  assert.equal(nameInput.value, "");
  assert.equal(keyInput.value, "");
});

test("adding a module key with a missing field does nothing", () => {
  freshModulesPage();
  sentPackets = [];

  const { nameInput, keyInput, addBtn } = addModuleRowFields();
  nameInput.value = "weather";
  keyInput.value = "";
  addBtn.dispatch("click");

  assert.deepEqual(sentPackets, []);
});

test("modules_list_result renders module rows and removing sends module_key_delete_request", () => {
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "weather", configured: true }] },
  });

  const row = registry["module-keys-list"]._children[0];
  assert.equal(row.querySelector("label").textContent, "weather");

  sentPackets = [];
  findButton(row, "Remove").dispatch("click");
  assert.deepEqual(sentPackets, [
    { type: "module_key_delete_request", payload: { module: "weather" } },
  ]);
});

test("saving a key directly from a module's own row sends module_key_set_request", () => {
  // Section 4: a self-discovered module (weather) must be directly
  // configurable from its own row's real, editable API-key field — not
  // only via the generic AddNewModuleRow form.
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "weather", configured: false }] },
  });

  const row = registry["module-keys-list"]._children[0];
  sentPackets = [];
  row.querySelector("input").value = "wk-inline-save-123";
  findButton(row, "Save").dispatch("click");

  assert.deepEqual(sentPackets, [
    { type: "module_key_set_request", payload: { module: "weather", api_key: "wk-inline-save-123" } },
  ]);
});

test("real modules render correctly: name, status, and an enabled/disabled Remove button", () => {
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [
      { name: "weather", configured: true },
      { name: "custom_tool", configured: false },
    ]},
  });

  const rows = registry["module-keys-list"]._children;
  assert.equal(rows.length, 2, "only real modules must appear in the module list");

  const weatherRow = rows.find((r) => r.querySelector("label").textContent === "weather");
  assert.equal(weatherRow.querySelector("label")._classes.has("configured"), true);
  assert.equal(weatherRow._children[1].textContent, "Key set");
  assert.equal(findButton(weatherRow, "Remove").disabled, false, "Weather must have a working (enabled) Remove button once configured");

  const customRow = rows.find((r) => r.querySelector("label").textContent === "custom_tool");
  assert.equal(customRow.querySelector("label")._classes.has("configured"), false);
  assert.equal(customRow._children[1].textContent, "Not configured");
  assert.equal(findButton(customRow, "Remove").disabled, true);
});

test("weather (self-discovered, unconfigured) renders via RealModuleRow with a real editable API-key field", () => {
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "weather", configured: false }] },
  });

  const row = registry["module-keys-list"]._children[0];
  const input = row.querySelector("input");
  assert.ok(input, "weather's row must have a real API-key input, not just a status badge");
  assert.equal(input.placeholder, "Enter API key");
  assert.ok(findButton(row, "Save"), "weather's row must have a working Save action");
  assert.ok(row._classes.has("module-row"), "weather must render via RealModuleRow, not a special/phantom component");
});

test("the Add New Module placeholder renders its own form, never as a module row", () => {
  freshModulesPage();
  const { mounted, nameInput, keyInput, addBtn } = addModuleRowFields();

  const header = mounted._children[0];
  assert.equal(header.tagName, "h3");
  assert.equal(header.textContent, "Add New Module");

  assert.equal(mounted.querySelector("label"), null, "placeholder must not show a module name label");
  const hasStatusText = mounted._children.some((c) => c.textContent === "Key set" || c.textContent === "Not configured");
  assert.equal(hasStatusText, false, "placeholder must not show a module status");
  const hasRemoveButton = mounted._children.some((c) => c.tagName === "button" && c.textContent === "Remove");
  assert.equal(hasRemoveButton, false, "placeholder must not show a Remove button");

  assert.equal(nameInput.tagName, "input");
  assert.equal(keyInput.tagName, "input");
  assert.equal(addBtn.textContent, "Add");

  const realModuleRowTokens = new Set([
    "module-row", "module-row-name", "module-row-status",
    "module-row-key-input", "module-row-save-btn", "module-row-remove-btn",
  ]);
  const allTokensInTree = [mounted, ...mounted._children].flatMap((el) => [...el._classes]);
  for (const token of allTokensInTree) {
    assert.ok(!realModuleRowTokens.has(token), `placeholder must not inherit RealModuleRow's class token "${token}"`);
  }
});

test("the Add New Module placeholder never appears inside #module-keys-list", () => {
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "weather", configured: true }] },
  });

  const listChildren = registry["module-keys-list"]._children;
  for (const row of listChildren) {
    assert.notEqual(row.className, "add-module-row", "the add-form must never be appended to the module list");
    assert.equal(row.querySelector("label") !== null, true, "every row actually in the list must be a real module row with a name");
  }
});

test("adding a new module creates a real module row (via the module_key_set_result → re-fetch → modules_list_result flow)", () => {
  freshModulesPage();

  dispatchBackendPacket({ type: "module_key_set_result", payload: { ok: true, module: "custom_tool" } });
  assert.ok(sentPackets.some((p) => p.type === "modules_list_request"), "saving a module key must trigger a re-fetch");

  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "custom_tool", configured: true }] },
  });

  const rows = registry["module-keys-list"]._children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].querySelector("label").textContent, "custom_tool");
  assert.equal(rows[0]._children[1].textContent, "Key set");
});

test("removing a module only affects real module rows, never the Add New Module placeholder", () => {
  freshModulesPage();
  dispatchBackendPacket({
    type: "modules_list_result",
    payload: { modules: [{ name: "weather", configured: true }] },
  });

  const before = addModuleRowFields();

  const row = registry["module-keys-list"]._children[0];
  sentPackets = [];
  findButton(row, "Remove").dispatch("click");

  assert.deepEqual(sentPackets, [
    { type: "module_key_delete_request", payload: { module: "weather" } },
  ]);

  const after = addModuleRowFields();
  assert.equal(after.mounted, before.mounted, "removing a module must not re-mount or touch the Add New Module form");

  dispatchBackendPacket({ type: "modules_list_result", payload: { modules: [] } });
  assert.equal(registry["module-keys-list"]._children.length, 0);
  assert.equal(registry["module-add-container"]._children.length, 1, "the add-form must still be present after the list empties");
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
