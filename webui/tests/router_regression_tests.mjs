// webui/tests/router_regression_tests.mjs
//
// Regression tests for the Settings hub's routing (webui/core/router.js):
//   - Router.routes has an entry for "settings" and each of its four
//     subpages ("settings/appearance", "settings/cloud-llms",
//     "settings/modules", "settings/local-models"), each pointing at a
//     real .html file with a matching real .js file next to it (the
//     router derives the JS path from the HTML path — see
//     Router.navigate()).
//   - Every one of those .js modules can actually be imported without
//     throwing (catches a broken import/syntax error in any new page
//     before it'd only surface live, mid-navigation).
//   - Router.updateSidebarSelection() keeps the top-level "Settings"
//     sidebar button highlighted while any settings/* subpage is the
//     active panel (a subpage has no sidebar button of its own).
//
// This intentionally does not simulate fetch()-based HTML loading or
// full DOM parsing (no test file in this repo does — every other test
// here pre-registers a fixed set of elements by id instead); the
// existence/import checks below are what's actually verifiable without
// building a real HTML parser, and they cover the two concrete ways a
// misconfigured route breaks in practice: a typo'd path, or a page
// module that fails to load at all.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/frontend_regression_tests.mjs. Run directly:
//
//   node webui/tests/router_regression_tests.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webuiRoot = path.resolve(__dirname, "..");

const { Router } = await import("../core/router.js");

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

const EXPECTED_SETTINGS_ROUTES = {
  "settings": "pages/settings_home/settings_home.html",
  "settings/appearance": "pages/appearance/appearance.html",
  "settings/cloud-llms": "pages/cloud_llms/cloud_llms.html",
  "settings/modules": "pages/modules/modules.html",
  "settings/local-models": "pages/local_models/local_models.html",
};

test("Router.routes has the expected entry for every Settings hub route", () => {
  for (const [panel, htmlPath] of Object.entries(EXPECTED_SETTINGS_ROUTES)) {
    assert.equal(Router.routes[panel], htmlPath, `Router.routes["${panel}"] should be "${htmlPath}"`);
  }
});

test("every Settings route's .html file exists on disk", () => {
  for (const htmlPath of Object.values(EXPECTED_SETTINGS_ROUTES)) {
    const fullPath = path.join(webuiRoot, htmlPath);
    assert.ok(fs.existsSync(fullPath), `missing HTML file for route: ${htmlPath}`);
  }
});

test("every Settings route's .js file exists on disk, matching Router.navigate()'s derived path", () => {
  for (const htmlPath of Object.values(EXPECTED_SETTINGS_ROUTES)) {
    const jsRelative = htmlPath.replace(/\.html$/, ".js");
    const fullPath = path.join(webuiRoot, jsRelative);
    assert.ok(fs.existsSync(fullPath), `missing JS module for route: ${jsRelative}`);
  }
});

test("every Settings page's JS module imports without throwing", async () => {
  // appearance.js imports webui/core/theme-loader.js, whose ThemeLoader
  // constructor runs at import time and touches `document`/`localStorage`
  // directly (not wrapped in a try/catch, unlike this codebase's *Log()
  // helpers) — same minimal stub webui/tests/appearance_regression_tests.mjs
  // uses, just enough for module evaluation to succeed.
  global.document = {
    getElementById: () => null,
    addEventListener: (evt, cb) => {
      if (evt === "DOMContentLoaded") cb();
    },
  };
  global.window = global;
  global.localStorage = {
    _store: {},
    getItem(k) { return this._store[k] ?? null; },
    setItem(k, v) { this._store[k] = v; },
  };

  for (const htmlPath of Object.values(EXPECTED_SETTINGS_ROUTES)) {
    const jsRelative = htmlPath.replace(/\.html$/, ".js");
    const fullPath = path.join(webuiRoot, jsRelative);
    const mod = await import("file://" + fullPath.replace(/\\/g, "/"));
    assert.equal(typeof mod.default?.init, "function", `${jsRelative} must export a default object with an init() function`);
  }

  delete global.document;
  delete global.window;
  delete global.localStorage;
});

test("the Models page route is untouched (still points at pages/models/models.html)", () => {
  assert.equal(Router.routes.models, "pages/models/models.html");
});

// ---- Sidebar highlighting for settings/* subpages ----
function makeSidebarBtn(panel) {
  const el = {
    tagName: "button",
    _classes: new Set(["sidebar-btn"]),
    dataset: { panel },
    classList: {
      add: (c) => el._classes.add(c),
      remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c),
    },
  };
  return el;
}

test("updateSidebarSelection() highlights the parent Settings button for any settings/* subpage", () => {
  const settingsBtn = makeSidebarBtn("settings");
  const modelsBtn = makeSidebarBtn("models");
  const allBtns = [settingsBtn, modelsBtn];

  global.document = {
    querySelectorAll: (sel) => (sel === ".sidebar-btn" ? allBtns : []),
    querySelector: (sel) => {
      const match = sel.match(/data-panel="([^"]+)"/);
      const panel = match?.[1];
      return allBtns.find((b) => b.dataset.panel === panel) || null;
    },
  };

  Router.updateSidebarSelection("settings/appearance");
  assert.ok(settingsBtn._classes.has("active"), "the Settings sidebar button must stay highlighted for a settings/* subpage");
  assert.ok(!modelsBtn._classes.has("active"));

  Router.updateSidebarSelection("models");
  assert.ok(!settingsBtn._classes.has("active"));
  assert.ok(modelsBtn._classes.has("active"));
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
