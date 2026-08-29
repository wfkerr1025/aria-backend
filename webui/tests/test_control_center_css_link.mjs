// webui/tests/test_control_center_css_link.mjs
//
// The Control Center's stylesheet is actually linked.
//
// This exists because of a specific failure. The router injects a
// panel's HTML into #panel-container and imports the module sitting next
// to it -- but it does nothing about CSS. Every page declares its own
// stylesheet in index.html, and the Control Center did not, so it
// rendered as a column of unstyled text while every test it had passed.
//
// The reason it is worth a file of its own is how it presents: unstyled
// output reads as a layout bug, and a layout bug sends you into the CSS
// looking for the rule that is wrong, when the actual fault is one
// missing line in a different file. A test that names the real cause is
// worth more than the ten minutes it saves.
//
// The spec that asked for this named the file control_center.css. The
// page is pages/workspaces/, so its stylesheet is workspaces.css -- the
// convention here is that a panel's three files sit together and share
// the panel's name, and renaming one of them to match a prose reference
// would break the pairing the router depends on. What matters is that
// the link exists and points at the file that is really there, which is
// what these assertions check.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webui = path.resolve(here, "..");

const read = (...parts) => fs.readFileSync(path.join(...parts), "utf8");
const indexHtml = read(webui, "index.html");
const routerJs = read(webui, "core/router.js");

const results = [];
function test(name, fn) {
  try {
    fn();
    results.push(["PASS", name]);
  } catch (error) {
    results.push(["FAIL", `${name}\n    ${error.message}`]);
  }
}

const ROUTE = "settings/workspaces";
const HTML = "pages/workspaces/workspaces.html";
const CSS = "pages/workspaces/workspaces.css";
const JS = "pages/workspaces/workspaces.js";

// ------------------------------------------------------
// The link
// ------------------------------------------------------
test("index.html links the Control Center stylesheet", () => {
  assert.ok(indexHtml.includes(CSS),
            `index.html does not link ${CSS}; the page will render unstyled`);
});

test("it is a stylesheet link, not just a mention", () => {
  const pattern = new RegExp(`<link[^>]*rel=["']stylesheet["'][^>]*href=["']${CSS}["']`);
  assert.ok(pattern.test(indexHtml) ||
            new RegExp(`<link[^>]*href=["']${CSS}["'][^>]*rel=["']stylesheet["']`).test(indexHtml));
});

test("the file it points at exists", () => {
  // A link to a stylesheet that is not there fails silently: the page
  // renders unstyled and the console shows a 404 nobody is watching.
  assert.ok(fs.existsSync(path.join(webui, CSS)), `${CSS} is missing`);
});

test("the stylesheet is not empty", () => {
  const css = read(webui, CSS);
  assert.ok(css.trim().length > 0);
  assert.ok(css.includes("ws-"), "the Control Center's classes are not in its stylesheet");
});

// ------------------------------------------------------
// Why the link is needed at all
// ------------------------------------------------------
test("the router loads HTML and JS but never CSS", () => {
  // The fact that makes the missing line a bug rather than an
  // optimisation. If this ever stops being true, this whole file can go.
  assert.ok(routerJs.includes("container.innerHTML = html"));
  assert.ok(routerJs.includes("await import(jsPath)"));
  assert.ok(!/rel=["']stylesheet["']/.test(routerJs),
            "the router now injects stylesheets; this test is obsolete");
  assert.ok(!routerJs.includes(".css"),
            "the router now knows about CSS; this test is obsolete");
});

// ------------------------------------------------------
// The three files agree
// ------------------------------------------------------
test("the route points at the page", () => {
  assert.ok(routerJs.includes(`"${ROUTE}": "${HTML}"`));
});

test("all three files sit together and share the panel's name", () => {
  // The router derives the module path from the HTML path, so the pair
  // must be named alike; the stylesheet follows the same convention so
  // the three are found together.
  for (const file of [HTML, CSS, JS]) {
    assert.ok(fs.existsSync(path.join(webui, file)), `${file} is missing`);
  }
});

test("something navigates to the route", () => {
  // A page linked in index.html, routed, and reachable from nowhere is
  // still invisible.
  const statusLine = read(webui, "components/working_directory/working_directory.js");
  const settingsHome = read(webui, "pages/settings_home/settings_home.html");
  assert.ok(statusLine.includes(ROUTE) || settingsHome.includes(ROUTE),
            "nothing in the UI opens the Control Center");
});

// ------------------------------------------------------
for (const [status, name] of results) console.log(`${status}  ${name}`);
const failed = results.filter(([status]) => status === "FAIL");
if (failed.length) {
  console.log(`\n${failed.length} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll tests passed.");
