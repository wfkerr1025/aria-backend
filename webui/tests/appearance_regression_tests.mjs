// webui/tests/appearance_regression_tests.mjs
//
// Regression tests for the Appearance Settings subpage
// (webui/pages/appearance/appearance.js):
//   - all six controls are present and initialized: Theme
//     (light/dark/system), Base Theme, Accent Color, Font Size, UI
//     Density, Animations toggle, Chat Bubble Style
//   - Base Theme / Accent Color are the exact same webui/core/
//     theme-loader.js-backed feature the old single-page Settings panel
//     already had — moved here unchanged.
//   - the newer preferences (color scheme, font size, density,
//     animations, chat bubble style) persist to localStorage and apply
//     as a `data-*` attribute on <html>, restored correctly on re-init.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/frontend_regression_tests.mjs. Run directly:
//
//   node webui/tests/appearance_regression_tests.mjs

import assert from "node:assert/strict";

function makeSelect(id) {
  const el = {
    tagName: "select",
    id,
    _children: [],
    _listeners: {},
    value: "",
    appendChild(child) {
      el._children.push(child);
      return child;
    },
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt) {
      (el._listeners[evt] || []).forEach((cb) => cb());
    },
  };
  return el;
}

function makeCheckbox(id) {
  const el = {
    tagName: "input",
    id,
    checked: false,
    _listeners: {},
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt) {
      (el._listeners[evt] || []).forEach((cb) => cb());
    },
  };
  return el;
}

const SELECT_IDS = [
  "appearance-color-scheme-select",
  "appearance-base-theme-select",
  "appearance-accent-select",
  "appearance-font-size-select",
  "appearance-density-select",
  "appearance-chat-bubble-select",
];

function makeThemeLink(id) {
  return {
    tagName: "link",
    id,
    href: "",
    _listeners: {},
    addEventListener(evt, cb) {
      (this._listeners[evt] ||= []).push(cb);
    },
  };
}

const registry = {};
function resetAppearanceDom() {
  for (const id of SELECT_IDS) registry[id] = makeSelect(id);
  registry["appearance-animations-toggle"] = makeCheckbox("appearance-animations-toggle");
  // Theme.applyBase()/applyAccent() (webui/core/theme-loader.js) set
  // .href directly on these two real <link> elements — not part of this
  // page's own markup, but always present in webui/index.html.
  registry["aria-base-theme"] = makeThemeLink("aria-base-theme");
  registry["aria-accent-theme"] = makeThemeLink("aria-accent-theme");
}
resetAppearanceDom();

const rootAttributes = {};
global.document = {
  getElementById: (id) => registry[id] || null,
  createElement: (tag) => (tag === "option" ? { value: "", textContent: "" } : makeSelect("")),
  documentElement: {
    setAttribute: (name, value) => { rootAttributes[name] = value; },
    getAttribute: (name) => rootAttributes[name] ?? null,
  },
  addEventListener: (evt, cb) => {
    // theme-loader.js's ThemeLoader constructor waits for
    // DOMContentLoaded before touching the <link> elements — fire
    // immediately, there's no real load sequence in this harness.
    if (evt === "DOMContentLoaded") cb();
  },
};
global.window = global;

let localStorageStore = {};
global.localStorage = {
  getItem: (k) => localStorageStore[k] ?? null,
  setItem: (k, v) => { localStorageStore[k] = v; },
};
global.window.aria = { log: () => {} };

const { default: Appearance } = await import("../pages/appearance/appearance.js");

function freshAppearance({ keepStorage = false } = {}) {
  if (!keepStorage) localStorageStore = {};
  for (const key of Object.keys(rootAttributes)) delete rootAttributes[key];
  resetAppearanceDom();
  Appearance.init();
  return Appearance;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("all six appearance controls are present and get initialized", () => {
  freshAppearance();

  // Theme (light/dark/system) defaults to "system" and is applied.
  assert.equal(registry["appearance-color-scheme-select"].value, "system");
  assert.equal(rootAttributes["data-color-scheme"], "system");

  // Base Theme / Accent Color dropdowns are populated (7 base themes, 9 accents).
  assert.equal(registry["appearance-base-theme-select"]._children.length, 7);
  assert.equal(registry["appearance-accent-select"]._children.length, 9);

  // Font Size / UI Density default to medium/comfortable.
  assert.equal(registry["appearance-font-size-select"].value, "medium");
  assert.equal(rootAttributes["data-font-size"], "medium");
  assert.equal(registry["appearance-density-select"].value, "comfortable");
  assert.equal(rootAttributes["data-density"], "comfortable");

  // Animations default to on (checked).
  assert.equal(registry["appearance-animations-toggle"].checked, true);
  assert.equal(rootAttributes["data-animations"], "on");

  // Chat Bubble Style defaults to rounded.
  assert.equal(registry["appearance-chat-bubble-select"].value, "rounded");
  assert.equal(rootAttributes["data-chat-bubble-style"], "rounded");
});

test("changing the Theme (color scheme) selector persists and applies the attribute", () => {
  freshAppearance();
  registry["appearance-color-scheme-select"].value = "dark";
  registry["appearance-color-scheme-select"].dispatch("change");

  assert.equal(rootAttributes["data-color-scheme"], "dark");
  assert.equal(localStorage.getItem("aria-color-scheme"), "dark");
});

test("changing Font Size persists and applies the attribute", () => {
  freshAppearance();
  registry["appearance-font-size-select"].value = "large";
  registry["appearance-font-size-select"].dispatch("change");

  assert.equal(rootAttributes["data-font-size"], "large");
  assert.equal(localStorage.getItem("aria-font-size"), "large");
});

test("changing UI Density persists and applies the attribute", () => {
  freshAppearance();
  registry["appearance-density-select"].value = "compact";
  registry["appearance-density-select"].dispatch("change");

  assert.equal(rootAttributes["data-density"], "compact");
  assert.equal(localStorage.getItem("aria-density"), "compact");
});

test("toggling Animations off persists and applies the attribute", () => {
  freshAppearance();
  registry["appearance-animations-toggle"].checked = false;
  registry["appearance-animations-toggle"].dispatch("change");

  assert.equal(rootAttributes["data-animations"], "off");
  assert.equal(localStorage.getItem("aria-animations"), "off");
});

test("changing Chat Bubble Style persists and applies the attribute", () => {
  freshAppearance();
  registry["appearance-chat-bubble-select"].value = "square";
  registry["appearance-chat-bubble-select"].dispatch("change");

  assert.equal(rootAttributes["data-chat-bubble-style"], "square");
  assert.equal(localStorage.getItem("aria-chat-bubble-style"), "square");
});

test("preferences saved in a previous session are restored on re-init", () => {
  freshAppearance();
  registry["appearance-font-size-select"].value = "small";
  registry["appearance-font-size-select"].dispatch("change");
  registry["appearance-animations-toggle"].checked = false;
  registry["appearance-animations-toggle"].dispatch("change");

  // Simulate a fresh page load: new DOM elements, same localStorage.
  freshAppearance({ keepStorage: true });

  assert.equal(registry["appearance-font-size-select"].value, "small");
  assert.equal(rootAttributes["data-font-size"], "small");
  assert.equal(registry["appearance-animations-toggle"].checked, false);
  assert.equal(rootAttributes["data-animations"], "off");
});

test("changing the base theme delegates to Theme.applyBase/applyThemeAttribute", () => {
  freshAppearance();
  registry["appearance-base-theme-select"].value = "cream";
  registry["appearance-base-theme-select"].dispatch("change");

  assert.equal(localStorage.getItem("aria-theme-base"), "themes/base/cream.css");
  assert.equal(localStorage.getItem("aria-theme-name"), "cream");
});

test("changing the accent color delegates to Theme.applyAccent", () => {
  freshAppearance();
  registry["appearance-accent-select"].value = "purple";
  registry["appearance-accent-select"].dispatch("change");

  assert.equal(localStorage.getItem("aria-theme-accent"), "themes/accents/purple.css");
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
