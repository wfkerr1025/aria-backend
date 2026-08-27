// webui/tests/module_row_regression_tests.mjs
//
// Unit-level regression tests for
// webui/components/module_row/module_row.js — the two deliberately
// separate row builders behind the Settings panel's Module Keys UI:
//
//   - RealModuleRow(module, { onSave, onDelete }) — one row per real
//     backend module, whether it's user-added or self-discovered
//     (backend.core.module_manager.KNOWN_MODULES, materialized into the
//     key store as a real empty entry by key_manager.ensure_module_entry()
//     — both kinds are indistinguishable to this component, by design):
//     name, status ("Key set" / "Not configured"), an editable API-key
//     input, a Save button, and a Remove button.
//   - AddNewModuleRow({ onAdd })           — the "Add New Module" form,
//     never a module itself: no name, no status, no Remove button, no
//     shared styling with RealModuleRow.
//
// See webui/tests/modules_page_regression_tests.mjs for the
// integration-level coverage (how webui/pages/modules/modules.js wires
// these into the actual Module Keys panel — the placeholder never
// landing in #module-keys-list, adding/removing real modules end-to-end).
// This file tests the two builders directly, independent of that page.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/module_row_regression_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    _children: [],
    _classes: new Set(),
    _listeners: {},
    textContent: "",
    value: "",
    disabled: false,
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
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

// RealModuleRow now has two buttons (Save, Remove) — find by text/class
// rather than relying on querySelector("button")'s "first match" behavior.
function findButton(row, text) {
  return row._children.find((c) => c.tagName === "button" && c.textContent === text) || null;
}

global.document = { createElement: (tag) => makeElement(tag) };

const { RealModuleRow, AddNewModuleRow } = await import("../components/module_row/module_row.js");

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

// ============================================================
// RealModuleRow
// ============================================================
test("RealModuleRow (configured) shows the module name, 'Key set', a masked key placeholder, and an enabled Remove button", () => {
  const row = RealModuleRow({ name: "weather", configured: true }, { onSave: () => {}, onDelete: () => {} });
  assert.equal(row.querySelector("label").textContent, "weather");
  assert.ok(row.querySelector("label")._classes.has("configured"));
  assert.equal(row._children[1].textContent, "Key set");
  assert.match(row.querySelector("input").placeholder, /key set/i);

  const removeBtn = findButton(row, "Remove");
  assert.equal(removeBtn.disabled, false);
});

test("RealModuleRow (not configured) shows 'Not configured', an empty-key placeholder, and a disabled Remove button", () => {
  const row = RealModuleRow({ name: "custom_tool", configured: false }, { onSave: () => {}, onDelete: () => {} });
  assert.ok(!row.querySelector("label")._classes.has("configured"));
  assert.equal(row._children[1].textContent, "Not configured");
  assert.equal(row.querySelector("input").placeholder, "Enter API key");
  assert.equal(findButton(row, "Remove").disabled, true);
});

test("RealModuleRow renders for a self-discovered module (weather, unconfigured) exactly like a user-added one", () => {
  // Self-discovered modules (backend.core.module_manager.KNOWN_MODULES)
  // are materialized as real, empty key-store entries — {name:
  // "weather", configured: false} is indistinguishable, as far as this
  // component is concerned, from any freshly user-added module.
  const selfDiscovered = RealModuleRow({ name: "weather", configured: false }, { onSave: () => {}, onDelete: () => {} });
  const userAdded = RealModuleRow({ name: "custom_tool", configured: false }, { onSave: () => {}, onDelete: () => {} });

  assert.equal(selfDiscovered.className, userAdded.className);
  assert.equal(selfDiscovered.querySelector("input").placeholder, userAdded.querySelector("input").placeholder);
  assert.equal(findButton(selfDiscovered, "Remove").disabled, findButton(userAdded, "Remove").disabled);
  // Weather is directly editable right here — a real, working API-key field.
  assert.equal(selfDiscovered.querySelector("input").tagName, "input");
  assert.ok(!!findButton(selfDiscovered, "Save"), "a self-discovered module must have a working Save action");
});

test("RealModuleRow's Save button calls onSave(name, value) and clears the input", () => {
  let saved = null;
  const row = RealModuleRow({ name: "weather", configured: false }, { onSave: (name, key) => (saved = { name, key }), onDelete: () => {} });

  const input = row.querySelector("input");
  input.value = "wk-live-123";
  findButton(row, "Save").dispatch("click");

  assert.deepEqual(saved, { name: "weather", key: "wk-live-123" });
  assert.equal(input.value, "");
});

test("RealModuleRow's Save button does nothing when the input is empty", () => {
  let called = false;
  const row = RealModuleRow({ name: "weather", configured: false }, { onSave: () => (called = true), onDelete: () => {} });
  findButton(row, "Save").dispatch("click");
  assert.equal(called, false);
});

test("RealModuleRow's Remove button calls onDelete with the module's name", () => {
  let deleted = null;
  const row = RealModuleRow({ name: "weather", configured: true }, { onSave: () => {}, onDelete: (name) => (deleted = name) });
  findButton(row, "Remove").dispatch("click");
  assert.equal(deleted, "weather");
});

test("RealModuleRow uses its own dedicated class names", () => {
  const row = RealModuleRow({ name: "weather", configured: true }, { onSave: () => {}, onDelete: () => {} });
  assert.ok(row._classes.has("module-row"));
  assert.ok(row.querySelector("label")._classes.has("module-row-name"));
  assert.ok(row._children[1]._classes.has("module-row-status"));
  assert.ok(row.querySelector("input")._classes.has("module-row-key-input"));
  assert.ok(findButton(row, "Save")._classes.has("module-row-save-btn"));
  assert.ok(findButton(row, "Remove")._classes.has("module-row-remove-btn"));
});

// ============================================================
// AddNewModuleRow
// ============================================================
test("AddNewModuleRow renders an 'Add New Module' header", () => {
  const el = AddNewModuleRow({ onAdd: () => {} });
  const header = el._children[0];
  assert.equal(header.tagName, "h3");
  assert.equal(header.textContent, "Add New Module");
});

test("AddNewModuleRow has no module name, no status, and no Remove button", () => {
  const el = AddNewModuleRow({ onAdd: () => {} });
  assert.equal(el.querySelector("label"), null, "must not show a module name");
  const hasStatusText = el._children.some((c) => c.textContent === "Key set" || c.textContent === "Not configured");
  assert.equal(hasStatusText, false, "must not show a module status");
  const hasRemoveButton = el._children.some((c) => c.tagName === "button" && c.textContent === "Remove");
  assert.equal(hasRemoveButton, false, "must not show a Remove button");
});

test("AddNewModuleRow exposes exactly a name input, a key input, and an Add button", () => {
  const el = AddNewModuleRow({ onAdd: () => {} });
  const inputs = el._children.filter((c) => c.tagName === "input");
  const buttons = el._children.filter((c) => c.tagName === "button");
  assert.equal(inputs.length, 2);
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].textContent, "Add");
});

test("AddNewModuleRow's Add button calls onAdd(name, key) and clears both inputs", () => {
  let added = null;
  const el = AddNewModuleRow({ onAdd: (name, key) => (added = { name, key }) });
  const [, nameInput, keyInput, addBtn] = el._children;

  nameInput.value = "custom_tool";
  keyInput.value = "secret-key";
  addBtn.dispatch("click");

  assert.deepEqual(added, { name: "custom_tool", key: "secret-key" });
  assert.equal(nameInput.value, "");
  assert.equal(keyInput.value, "");
});

test("AddNewModuleRow's Add button does nothing when a field is empty", () => {
  let called = false;
  const el = AddNewModuleRow({ onAdd: () => (called = true) });
  const [, nameInput, keyInput, addBtn] = el._children;

  nameInput.value = "custom_tool";
  keyInput.value = "";
  addBtn.dispatch("click");

  assert.equal(called, false);
});

test("AddNewModuleRow never carries a hardcoded module name like Weather", () => {
  // Direct regression for the exact "static Weather row above
  // AddNewModuleRow" bug this component split fixed: the add-form must
  // be generic — no real or default module name/value baked in anywhere
  // in its tree, only a placeholder hint on the name input.
  const el = AddNewModuleRow({ onAdd: () => {} });
  const [, nameInput, keyInput] = el._children;

  assert.equal(nameInput.value, "", "the name input must start empty, never pre-filled with a real module name");
  assert.equal(keyInput.value, "", "the key input must start empty");
  assert.doesNotMatch(el._children[0].textContent, /weather/i, "the header must not name a specific module");

  const allText = el._children.map((c) => c.textContent || "").join(" ");
  assert.doesNotMatch(allText, /weather/i, "no static module name may appear anywhere in the add-form's rendered text");
});

test("AddNewModuleRow shares no component-specific class tokens with RealModuleRow", () => {
  // "btn"/"btn-secondary" are app-wide generic button utility classes
  // (webui/components/buttons/buttons.css), legitimately shared by every
  // button in the app — not something specific to RealModuleRow, so
  // they're excluded from this check. Everything else must be distinct.
  const SHARED_UTILITY_CLASSES = new Set(["btn", "btn-secondary"]);

  const addEl = AddNewModuleRow({ onAdd: () => {} });
  const realRow = RealModuleRow({ name: "weather", configured: true }, { onSave: () => {}, onDelete: () => {} });

  const realTokens = new Set(
    [realRow, ...realRow._children]
      .flatMap((el) => [...el._classes])
      .filter((token) => !SHARED_UTILITY_CLASSES.has(token))
  );
  const addTokens = [addEl, ...addEl._children]
    .flatMap((el) => [...el._classes])
    .filter((token) => !SHARED_UTILITY_CLASSES.has(token));

  for (const token of addTokens) {
    assert.ok(!realTokens.has(token), `AddNewModuleRow must not share class token "${token}" with RealModuleRow`);
  }
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
