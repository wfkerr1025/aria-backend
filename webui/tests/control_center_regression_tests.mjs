// webui/tests/control_center_regression_tests.mjs
//
// The ARIA Control Center: every project ARIA is working in.
//
// Wiring assertions, for the reason this suite keeps having to make
// them: the failure mode in this app has not been a page that renders
// wrongly, it has been one that is never reached. A page whose route is
// missing, or whose packets the backend does not know, looks finished
// from the outside and passes its own tests.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webui = path.resolve(here, "..");
const repo = path.resolve(webui, "..");
const read = (...p) => fs.readFileSync(path.join(...p), "utf8");

const pageJs = read(webui, "pages/workspaces/workspaces.js");
const pageHtml = read(webui, "pages/workspaces/workspaces.html");
const pageCss = read(webui, "pages/workspaces/workspaces.css");
const router = read(webui, "core/router.js");
const uiSchema = read(webui, "core/ipc_schema.js");
const backendSchema = read(repo, "backend/ipc_schema.py");
const backendRouter = read(repo, "backend/ipc_router.py");
const settingsHome = read(webui, "pages/settings_home/settings_home.html");
const indexHtml = read(webui, "index.html");

const results = [];
const test = (n, f) => { try { f(); results.push(["PASS", n]); }
                         catch (e) { results.push(["FAIL", `${n}\n    ${e.message}`]); } };

// ------------------------------------------------------
// Reachable
// ------------------------------------------------------
test("the page has a route", () => {
  assert.match(router, /"settings\/workspaces":\s*"pages\/workspaces\/workspaces\.html"/);
});

test("its stylesheet is linked", () => {
  // The router injects a page's HTML and imports its JS, but never its
  // CSS -- every page declares that in index.html. Without the link the
  // page renders as unstyled text, which reads as a layout bug rather
  // than a missing line.
  assert.ok(indexHtml.includes("pages/workspaces/workspaces.css"));
});

test("Settings offers a way in", () => {
  assert.match(settingsHome, /data-panel="settings\/workspaces"/);
});

test("the html and js sit together, as the router expects", () => {
  // The router derives the JS path from the HTML path.
  assert.ok(pageHtml.includes('id="workspaces-page"'));
  assert.ok(pageJs.includes("export default Workspaces"));
  assert.ok(pageJs.includes("init()"));
});

test("every packet it sends is one the backend handles", () => {
  const sent = [...pageJs.matchAll(/IPC\.(WORKSPACE_[A-Z_]+)/g)].map((m) => m[1]);
  assert.ok(sent.length >= 6, `only found ${sent.length} workspace packets`);

  for (const name of new Set(sent)) {
    assert.match(uiSchema, new RegExp(`${name}:`), `webui schema is missing ${name}`);
    assert.match(backendSchema, new RegExp(`^${name} = `, "m"), `backend schema is missing ${name}`);
    if (name.endsWith("_REQUEST")) {
      assert.match(backendRouter, new RegExp(`schema\.${name}:`), `no backend handler for ${name}`);
    }
  }
});

// ------------------------------------------------------
// A view, not a second opinion
// ------------------------------------------------------
test("the page computes no paths", () => {
  // Roots and ghost directories are rendered from the packet. A path
  // built here could disagree with the boundary the file tools enforce.
  assert.ok(!/aria_staging/.test(pageJs));
});

test("paths and diffs are escaped before innerHTML", () => {
  // Diffs are file contents; roots come off a filesystem.
  assert.ok(pageJs.includes("function esc("));
  assert.ok(pageJs.includes("esc(p.diff)"));
  assert.ok(pageJs.includes("esc(w.root_path)"));
});

test("errors are shown rather than swallowed", () => {
  assert.ok(pageHtml.includes('id="ws-error"'));
  assert.ok(pageJs.includes("showError"));
  assert.ok(pageJs.includes('packet.type === "error"'));
});

// ------------------------------------------------------
// Consent, unchanged
// ------------------------------------------------------
test("commit and discard carry the user's own words", () => {
  // The backend checks them against the same negation table action_plan
  // uses. Sending a canned phrase would be this page authorising the
  // change instead of the user.
  //
  // Asserted as "it asks, and sends what it was told" rather than
  // naming the function it asks with. The first version pinned
  // window.prompt, which then had to be replaced -- Electron's renderer
  // does not implement it -- and a test that pins the mechanism fails
  // for the fix as loudly as it would for a regression.
  assert.match(pageJs, /async commit\(id, files\)[\s\S]{0,600}dialog\.text\(/);
  assert.match(pageJs, /async discard\(id, files\)[\s\S]{0,600}dialog\.text\(/);
  assert.ok(pageJs.includes("user_text"));
  assert.ok(pageJs.includes("WORKSPACE_COMMIT_REQUEST, { id, files, user_text }"));
});

test("the consent field starts empty", () => {
  // A pre-filled "commit the changes" would be this page writing the
  // consent and the user pressing OK. The example belongs in the label,
  // where it cannot be submitted.
  assert.ok(pageJs.includes('label: "For example: commit the changes"'));
  assert.ok(pageJs.includes("input.value = \"\";"),
            "the dialog reuses one input; not clearing it carries the last answer forward");
});

test("a cancelled prompt sends nothing", () => {
  // null is cancelled and "" is a cleared box; only the first means "I
  // did not mean this". Collapsing them would make Cancel commit.
  const commits = pageJs.split("async commit(id, files)")[1] || "";
  assert.ok(commits.includes("=== null) return"));
});

test("removing a workspace says what will survive it", () => {
  // Removing is bookkeeping, never a delete, and a user about to click
  // it should know their staged work stays on disk.
  assert.ok(/staged change[\s\S]{0,80}left on disk, not deleted/.test(pageJs));
});

// ------------------------------------------------------
// Layout
// ------------------------------------------------------
test("a wide diff scrolls inside itself", () => {
  // The page body must never scroll sideways.
  assert.match(pageCss, /\.ws-diff\s*\{[^}]*overflow:\s*auto/);
});

test("it collapses to one column on a narrow window", () => {
  assert.match(pageCss, /@media[^{]*max-width[^{]*\{[^}]*grid-template-columns:\s*1fr/);
});

test("its colours come from the app's tokens", () => {
  assert.ok(pageCss.includes("var(--"));
});

for (const [s, n] of results) console.log(`${s}  ${n}`);
const failed = results.filter(([s]) => s === "FAIL");
if (failed.length) { console.log(`\n${failed.length} failed.`); process.exit(1); }
console.log("\nAll tests passed.");
