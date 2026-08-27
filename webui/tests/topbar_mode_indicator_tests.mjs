// webui/tests/topbar_mode_indicator_tests.mjs
//
// Regression tests for the top-right mode indicator
// (webui/components/topbar/topbar.js). Covers the "remove AUTO MODE"
// pass: the indicator must show only Local / Cloud / Automatic — the
// retired "AUTO" label/class must never appear again, from any packet
// type this widget listens for (mode_changed, mode_set_result,
// mode_status_result, system_result) or from its own default fallback.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/topbar_mode_indicator_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    _classes: new Set(),
    textContent: "",
    offsetWidth: 0,
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    classList: {
      add: (...names) => names.forEach((n) => el._classes.add(n)),
      remove: (...names) => names.forEach((n) => el._classes.delete(n)),
      contains: (n) => el._classes.has(n),
    },
    addEventListener: () => {},
  };
  return el;
}

const registry = {};
function resetTopbarDom() {
  registry["system-status"] = makeElement("div");
  registry["aria-mode-indicator"] = makeElement("div");
  registry["aria-mode-light"] = makeElement("div");
  registry["topbar-refresh-btn"] = makeElement("button");
  registry["topbar-settings-btn"] = makeElement("button");
  registry["topbar-help-btn"] = makeElement("button");
}
resetTopbarDom();

global.document = {
  getElementById: (id) => registry[id] || null,
};
global.window = global;
global.window.addEventListener = () => {};
global.window.dispatchEvent = () => {};
global.window.showToast = () => {};

const sentPackets = [];
global.window.aria = { sendToBackend: (p) => sentPackets.push(p), log: () => {} };

const { default: Topbar } = await import("../components/topbar/topbar.js");

function freshTopbar() {
  resetTopbarDom();
  Topbar.init();
  return Topbar;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("default indicator (no mode ever reported) shows Automatic, never Auto", () => {
  freshTopbar();
  Topbar.updateModeIndicator(undefined);
  const modeEl = registry["aria-mode-indicator"];
  const lightEl = registry["aria-mode-light"];
  assert.equal(modeEl.textContent, "AUTOMATIC");
  assert.ok(modeEl._classes.has("mode-automatic"));
  assert.ok(lightEl._classes.has("mode-automatic"));
  assert.ok(!modeEl._classes.has("mode-auto"), "retired mode-auto class must never be applied");
  assert.notEqual(modeEl.textContent, "AUTO", "retired AUTO label must never render");
});

test("mode_changed packet with mode=automatic renders Automatic", () => {
  freshTopbar();
  Topbar.handlePacket({ type: "mode_changed", mode: "automatic" });
  const modeEl = registry["aria-mode-indicator"];
  assert.equal(modeEl.textContent, "AUTOMATIC");
  assert.ok(modeEl._classes.has("mode-automatic"));
});

test("mode_changed packet with no mode field falls back to Automatic, not Auto", () => {
  freshTopbar();
  Topbar.handlePacket({ type: "mode_changed" });
  const modeEl = registry["aria-mode-indicator"];
  assert.equal(modeEl.textContent, "AUTOMATIC");
  assert.ok(!modeEl._classes.has("mode-auto"));
});

test("mode_set_result renders Local (green class)", () => {
  freshTopbar();
  Topbar.handlePacket({ type: "mode_set_result", payload: { ok: true, mode: "local" } });
  const modeEl = registry["aria-mode-indicator"];
  assert.equal(modeEl.textContent, "LOCAL");
  assert.ok(modeEl._classes.has("mode-local"));
});

test("mode_set_result renders Cloud (yellow class)", () => {
  freshTopbar();
  Topbar.handlePacket({ type: "mode_set_result", payload: { ok: true, mode: "cloud" } });
  const modeEl = registry["aria-mode-indicator"];
  assert.equal(modeEl.textContent, "CLOUD");
  assert.ok(modeEl._classes.has("mode-cloud"));
});

test("mode_status_result (fresh page load) renders whatever real persisted mode is reported", () => {
  freshTopbar();
  Topbar.handlePacket({ type: "mode_status_result", payload: { routing_mode: "automatic" } });
  const modeEl = registry["aria-mode-indicator"];
  assert.equal(modeEl.textContent, "AUTOMATIC");
  assert.ok(modeEl._classes.has("mode-automatic"));
});

test("system_result with no mode/value falls back to Automatic, never Auto", () => {
  freshTopbar();
  Topbar.handlePacket({ type: "system_result", payload: { result: {} } });
  const modeEl = registry["aria-mode-indicator"];
  assert.equal(modeEl.textContent, "AUTOMATIC");
  assert.ok(!modeEl._classes.has("mode-auto"));
});

test("switching mode-to-mode never leaves a stale mode-auto/mode-automatic/mode-local/mode-cloud class behind", () => {
  freshTopbar();
  Topbar.updateModeIndicator("local");
  Topbar.updateModeIndicator("cloud");
  const modeEl = registry["aria-mode-indicator"];
  assert.ok(!modeEl._classes.has("mode-local"));
  assert.ok(!modeEl._classes.has("mode-auto"));
  assert.ok(!modeEl._classes.has("mode-automatic"));
  assert.ok(modeEl._classes.has("mode-cloud"));
});

test("the literal string AUTO (as opposed to AUTOMATIC/LOCAL/CLOUD) never appears in the indicator text", () => {
  freshTopbar();
  for (const mode of ["automatic", "local", "cloud", undefined]) {
    Topbar.updateModeIndicator(mode);
    const text = registry["aria-mode-indicator"].textContent;
    assert.notEqual(text, "AUTO", `mode=${JSON.stringify(mode)} must never render bare "AUTO"`);
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
