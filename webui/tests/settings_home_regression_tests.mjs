// webui/tests/settings_home_regression_tests.mjs
//
// Regression tests for the Steam-style Settings hub
// (webui/pages/settings_home/settings_home.js):
//   - renders four tiles (Appearance, Cloud LLMs, Modules, Local Models)
//   - each tile navigates to its correct subpage by dispatching the same
//     "navigatePanel" CustomEvent every sidebar button already uses (see
//     webui/components/sidebar/sidebar.js) — not a parallel navigation
//     mechanism.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/topbar_mode_indicator_tests.mjs. Run directly:
//
//   node webui/tests/settings_home_regression_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    dataset: {},
    _listeners: {},
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt, arg) {
      (el._listeners[evt] || []).forEach((cb) => cb(arg));
    },
  };
  return el;
}

// Mirrors settings_home.html's four <button class="settings-tile"
// data-panel="...">.
const TILE_PANELS = [
  "settings/appearance",
  "settings/cloud-llms",
  "settings/modules",
  "settings/local-models",
];

let tiles = [];
function resetTiles() {
  tiles = TILE_PANELS.map((panel) => {
    const el = makeElement("button");
    el.dataset.panel = panel;
    return el;
  });
}
resetTiles();

global.document = {
  querySelectorAll: (sel) => (sel === ".settings-tile" ? tiles : []),
};

const dispatchedEvents = [];
global.window = {
  dispatchEvent: (evt) => dispatchedEvents.push(evt),
  aria: { log: () => {} },
};

const { default: SettingsHome } = await import("../pages/settings_home/settings_home.js");

function freshSettingsHome() {
  resetTiles();
  global.document.querySelectorAll = (sel) => (sel === ".settings-tile" ? tiles : []);
  dispatchedEvents.length = 0;
  SettingsHome.init();
  return SettingsHome;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("the hub renders exactly four tiles", () => {
  freshSettingsHome();
  assert.equal(tiles.length, 4);
});

test("the four tiles cover Appearance, Cloud LLMs, Modules, and Local Models", () => {
  freshSettingsHome();
  const panels = tiles.map((t) => t.dataset.panel).sort();
  assert.deepEqual(panels, [
    "settings/appearance",
    "settings/cloud-llms",
    "settings/local-models",
    "settings/modules",
  ]);
});

test("clicking a tile dispatches navigatePanel with that tile's exact target panel", () => {
  freshSettingsHome();

  for (const panel of TILE_PANELS) {
    dispatchedEvents.length = 0;
    const tile = tiles.find((t) => t.dataset.panel === panel);
    tile.dispatch("click");

    assert.equal(dispatchedEvents.length, 1, `expected exactly one navigatePanel dispatch for ${panel}`);
    assert.equal(dispatchedEvents[0].type, "navigatePanel");
    assert.equal(dispatchedEvents[0].detail, panel);
  }
});

test("a tile with no data-panel does not dispatch navigation and does not throw", () => {
  resetTiles();
  const brokenTile = makeElement("button"); // dataset.panel left undefined
  tiles.push(brokenTile);
  global.document.querySelectorAll = (sel) => (sel === ".settings-tile" ? tiles : []);
  dispatchedEvents.length = 0;
  SettingsHome.init();

  assert.doesNotThrow(() => brokenTile.dispatch("click"));
  assert.equal(dispatchedEvents.length, 0);
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
