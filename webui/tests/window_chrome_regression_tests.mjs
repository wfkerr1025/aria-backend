// webui/tests/window_chrome_regression_tests.mjs
//
// The window chrome, themed.
//
// The default Windows frame paints a white title bar and a white
// File/Edit/View strip above a dark app -- the first thing anyone sees.
// Electron's titleBarOverlay replaces it with caption buttons whose
// colours the app chooses.
//
// Two things this has to get right beyond the colour, and both are worse
// than the bug if missed: a hidden title bar leaves the window with
// nothing to drag by, and the caption overlay is painted on top of the
// page rather than laid out in it, so content underneath it is
// unclickable.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webui = path.resolve(here, "..");
const desktop = path.resolve(webui, "../../ARIA-Lite Desktop");

const read = (p) => fs.readFileSync(p, "utf8");
const mainJs = read(path.join(desktop, "main.js"));
const preloadJs = read(path.join(desktop, "preload.js"));
const themeLoader = read(path.join(webui, "core/theme-loader.js"));
const topbarCss = read(path.join(webui, "components/topbar/topbar.css"));
const variablesCss = read(path.join(webui, "themes/variables.css"));

const results = [];
const test = (name, fn) => {
  try { fn(); results.push(["PASS", name]); }
  catch (e) { results.push(["FAIL", `${name}\n    ${e.message}`]); }
};

test("the native frame is replaced, not merely recoloured", () => {
  assert.match(mainJs, /titleBarStyle:\s*"hidden"/);
  assert.match(mainJs, /titleBarOverlay:/);
});

test("the window paints in the theme colour before the first frame", () => {
  // Electron fills white by default; "show: false until ready" hides the
  // load but not the fill, so a launch flashes white without this.
  assert.match(mainJs, /backgroundColor:/);
});

test("the defaults match the stylesheet's own dark values", () => {
  // Picked from variables.css rather than invented, so a fresh launch
  // does not flash a colour the app never uses.
  const bg = /--color-gray-050:\s*(#[0-9a-fA-F]{6})/.exec(variablesCss)?.[1];
  const fg = /--color-blue-100:\s*(#[0-9a-fA-F]{6})/.exec(variablesCss)?.[1];
  assert.ok(bg && fg);
  assert.ok(mainJs.includes(bg), `main.js default colour is not ${bg}`);
  assert.ok(mainJs.includes(fg), `main.js default symbol colour is not ${fg}`);
});

test("the chrome follows the theme at runtime", () => {
  // Seven base themes, one of them light. A palette copied into the main
  // process goes stale the first time a theme is edited.
  assert.match(mainJs, /ipcMain\.on\("set-window-chrome"/);
  assert.match(preloadJs, /setWindowChrome/);
  assert.match(themeLoader, /syncWindowChrome/);
  assert.match(themeLoader, /setWindowChrome\(/);
});

test("the colours are read off the live stylesheet", () => {
  assert.match(themeLoader, /getPropertyValue\("--bg-app"\)/);
  assert.match(themeLoader, /getPropertyValue\("--text-primary"\)/);
});

test("the read is deferred past the stylesheet swap", () => {
  // Reading a custom property in the same tick as the swap returns the
  // OLD theme's value, so the chrome would always lag one theme behind.
  assert.match(themeLoader, /requestAnimationFrame/);
});

test("an unresolved variable is not sent", () => {
  // setTitleBarOverlay throws on "" rather than falling back.
  assert.match(themeLoader, /if \(!color \|\| !symbolColor\)/);
});

test("a platform without the overlay does not crash the window", () => {
  // setTitleBarOverlay is Windows-only.
  assert.match(mainJs, /setTitleBarOverlay[\s\S]{0,400}catch/);
});

test("the window can still be dragged", () => {
  // A hidden title bar leaves nothing to drag by, which is worse than
  // the white bar it replaced.
  assert.match(topbarCss, /#topbar\s*\{[^}]*-webkit-app-region:\s*drag/);
});

test("the top bar's controls are still clickable", () => {
  // An element inside a drag region does not receive clicks.
  assert.match(topbarCss, /#topbar button[\s\S]{0,200}-webkit-app-region:\s*no-drag/);
});

test("the top bar keeps clear of the caption buttons", () => {
  // The overlay is painted over the page, not laid out in it.
  assert.match(topbarCss, /#topbar\s*\{[^}]*padding-right:/);
});

for (const [status, name] of results) console.log(`${status}  ${name}`);
const failed = results.filter(([s]) => s === "FAIL");
if (failed.length) { console.log(`\n${failed.length} test(s) failed.`); process.exit(1); }
console.log("\nAll tests passed.");
