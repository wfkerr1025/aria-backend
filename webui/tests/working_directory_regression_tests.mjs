// webui/tests/working_directory_regression_tests.mjs
//
// The working-directory panel: which project ARIA is in, where it
// stages, and whether the model can be asked for an action.
//
// Mostly wiring assertions, deliberately. The failure this session kept
// producing was not a component that renders wrongly -- it was a
// component that is never reached: a search classifier wired to a
// parameter no caller passes, a flag nothing supplies. A panel that
// exists in a file, is never linked from index.html and is never
// mounted in app.js looks exactly like a finished feature from the
// outside, and its tests pass.
//
// The rendering itself was verified in a browser against the real
// index.html: collapsed to one line bottom-right, expanded to the
// top-right, with the sidebar's New Chat button and the composer's Send
// button both left clickable.
//
// Self-contained, plain-assert, no test framework -- same convention as
// the other files here.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webui = path.resolve(here, "..");
const repo = path.resolve(webui, "..");

const read = (...parts) => fs.readFileSync(path.join(...parts), "utf8");

const componentJs = read(webui, "components/working_directory/working_directory.js");
const componentCss = read(webui, "components/working_directory/working_directory.css");
const indexHtml = read(webui, "index.html");
const appJs = read(webui, "core/app.js");
const uiSchema = read(webui, "core/ipc_schema.js");
const backendSchema = read(repo, "backend/ipc_schema.py");

const results = [];
function test(name, fn) {
  try {
    fn();
    results.push(["PASS", name]);
  } catch (error) {
    results.push(["FAIL", `${name}\n    ${error.message}`]);
  }
}

// ------------------------------------------------------
// It is actually reachable
// ------------------------------------------------------
test("index.html has the container the panel mounts into", () => {
  assert.ok(indexHtml.includes('id="working-directory-panel"'));
});

test("index.html loads the stylesheet and the module", () => {
  assert.ok(indexHtml.includes("components/working_directory/working_directory.css"));
  assert.ok(indexHtml.includes("components/working_directory/working_directory.js"));
});

test("app.js imports and initialises it", () => {
  assert.ok(appJs.includes("working_directory/working_directory.js"));
  assert.ok(/WorkingDirectory\.init\(\)/.test(appJs));
});

test("the container is page-global, beside the model indicator", () => {
  // Staging is per-project, so "which project am I in" has to survive
  // navigation -- which means living outside the router-loaded panel,
  // where #active-model-container already lives for the same reason.
  //
  // Asserted as adjacency rather than by parsing the document: the
  // earlier version of this test called a helper that always returned
  // false, so it passed without checking anything.
  const panel = indexHtml.indexOf('id="working-directory-panel"');
  const modelIndicator = indexHtml.indexOf('id="active-model-container"');

  assert.ok(panel !== -1 && modelIndicator !== -1);
  const between = indexHtml.slice(panel, modelIndicator);
  assert.ok(between.length < 400 && !between.includes("panel-container"),
            "the panel is no longer next to the other page-global overlay");
});

// ------------------------------------------------------
// Both ends agree on the packet names
// ------------------------------------------------------
test("the UI and the backend name the same packets", () => {
  for (const name of ["workspace_status_request", "workspace_status_result",
                      "workspace_set_request"]) {
    assert.ok(uiSchema.includes(`"${name}"`), `webui schema is missing ${name}`);
    assert.ok(backendSchema.includes(`"${name}"`), `backend schema is missing ${name}`);
  }
});

test("the panel asks for its state and can change the directory", () => {
  assert.ok(componentJs.includes("IPC.WORKSPACE_STATUS_REQUEST"));
  assert.ok(componentJs.includes("IPC.WORKSPACE_SET_REQUEST"));
});

// ------------------------------------------------------
// It is a view, not a second opinion
// ------------------------------------------------------
test("the panel computes no path of its own", () => {
  // Every value comes from the packet. A path derived here would be a
  // second opinion able to disagree with the boundary the file tools
  // actually enforce.
  assert.ok(!/aria_staging/.test(componentJs),
            "the panel builds a staging path itself instead of reading ghost_root");
});

test("a rejected directory change is shown, not swallowed", () => {
  // The whole point of a visible working directory is that it says
  // where ARIA actually is; a refused change must not leave the old
  // path on screen looking accepted.
  assert.ok(componentJs.includes("showError"));
  assert.ok(componentJs.includes('packet.type === "error"'));
});

test("paths and warnings are escaped before they reach innerHTML", () => {
  assert.ok(componentJs.includes("function escapeHtml"));
  assert.ok(componentJs.includes("escapeHtml(s.project_root"));
  assert.ok(componentJs.includes("escapeHtml(s.ghost_root"));
});

// ------------------------------------------------------
// It does not cover the controls
// ------------------------------------------------------
test("collapsed by default", () => {
  assert.ok(/expanded:\s*false/.test(componentJs));
});

test("expanded, it moves away from the composer", () => {
  // Measured at 1280x720: bottom-right and expanded, the panel spanned
  // y 425-664 over a Send button at y 585-615, so opening it took the
  // Send button away.
  assert.ok(componentCss.includes(":not(.wd-collapsed)"));
  assert.ok(/:not\(\.wd-collapsed\)\s*\{[^}]*top:/.test(componentCss));
  assert.ok(/:not\(\.wd-collapsed\)\s*\{[^}]*bottom:\s*auto/.test(componentCss));
});

test("it sits clear of the full-height sidebar", () => {
  // The first version was anchored left and covered the New Chat
  // button and the chat list.
  assert.ok(/#working-directory-panel\s*\{[^}]*right:/.test(componentCss));
  assert.ok(!/#working-directory-panel\s*\{[^}]*[^-]left:/.test(componentCss));
});

// ------------------------------------------------------
for (const [status, name] of results) console.log(`${status}  ${name}`);
const failed = results.filter(([status]) => status === "FAIL");
if (failed.length) {
  console.log(`\n${failed.length} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll tests passed.");
