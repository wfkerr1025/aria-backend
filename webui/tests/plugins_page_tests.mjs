// webui/tests/plugins_page_tests.mjs
//
// The Plugins page, and the promise that it matches the Settings page.
//
// "The card layout must visually match the Settings Page exactly." The
// only way that stays true is by reusing .settings-tile rather than
// copying its rules -- a copy is identical the day it is written and
// drifts on the first change to either page. So this asserts the reuse,
// which is the thing that cannot silently stop being true.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webui = path.join(here, "..");
const read = (...parts) => fs.readFileSync(path.join(webui, ...parts), "utf8");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --- the layout promise -------------------------------------------

test("plugin cards use the Settings page's own tile class", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes('"settings-tile plugin-tile"'),
            "a tile must carry .settings-tile, not a copy of its rules");
  assert.ok(js.includes("settings-tile-title"));
  assert.ok(js.includes("settings-tile-subtitle"));
});

test("the grid is the Settings page's grid", () => {
  assert.ok(read("pages", "plugins", "plugins.html").includes("settings-tile-grid"));
});

test("the plugins stylesheet does not redefine the tile", () => {
  // Redefining .settings-tile here would restyle the Settings page too.
  const css = read("pages", "plugins", "plugins.css");
  assert.ok(!/^\.settings-tile\s*\{/m.test(css),
            "plugins.css must not redefine .settings-tile");
});

test("both stylesheets are actually loaded", () => {
  const html = read("index.html");
  assert.ok(html.includes("pages/settings_home/settings_home.css"));
  assert.ok(html.includes("pages/plugins/plugins.css"));
  assert.ok(html.includes("pages/plugin_config/plugin_config.css"));
});

// --- nothing hard-coded -------------------------------------------

test("no plugin is written into the markup", () => {
  const html = read("pages", "plugins", "plugins.html");

  for (const word of ["Unity", "Blender", "Ludo", "Self", "Diagnostics"]) {
    assert.ok(!html.includes(word), `${word} is hard-coded into the page`);
  }
});

test("the removed plugins are gone from the UI", () => {
  const files = [
    read("pages", "plugins", "plugins.html"),
    read("pages", "plugins", "plugins.css"),
    read("index.html"),
    read("core", "router.js"),
  ].join("\n").toLowerCase();

  assert.ok(!files.includes("self-improvement"));
  assert.ok(!files.includes("self\u2011improvement"));
  assert.ok(!files.includes("diagnostics enhancer"));
});

test("the old plugins component no longer exists", () => {
  assert.ok(!fs.existsSync(path.join(webui, "components", "plugins")));
});

// --- routes --------------------------------------------------------

test("every config route is registered and points at the shared page", () => {
  const router = read("core", "router.js");

  for (const id of ["unity", "blender", "ludo"]) {
    assert.ok(router.includes(`"plugins/${id}-config": "pages/plugin_config/plugin_config.html"`),
              `plugins/${id}-config is not routed`);
  }
  assert.ok(router.includes('plugins: "pages/plugins/plugins.html"'));
});

test("a config subpage keeps the Plugins sidebar button lit", () => {
  assert.ok(read("core", "router.js").includes('panelName.startsWith("plugins/")'));
});

// --- what the pages send ------------------------------------------

test("the page reads the registry rather than a second list", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes("PLUGIN_REGISTRY_LIST_REQUEST"));
  // plugins_list_request belongs to the OTHER plugin system, which
  // loads plugin code. Sending it here would list the wrong things.
  assert.ok(!js.includes("PLUGINS_LIST_REQUEST"));
});

test("every packet the pages use exists in the schema", () => {
  const schema = read("core", "ipc_schema.js");
  const used = new Set();

  for (const file of [read("pages", "plugins", "plugins.js"),
                      read("pages", "plugin_config", "plugin_config.js")]) {
    for (const match of file.matchAll(/IPC\.([A-Z_]+)/g)) used.add(match[1]);
  }

  assert.ok(used.size > 0, "no IPC constants found; this test would pass vacuously");
  for (const name of used) {
    assert.ok(schema.includes(name + ":"), `IPC.${name} is not defined`);
  }
});

// --- the config page ----------------------------------------------

test("an untouched key field is not sent, so Save cannot wipe a key", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes('input.dataset.secret === "true" && input.value === ""'));
});

test("the key box starts empty whatever the backend said", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes('input.value = "";'),
            "a password field must never be populated from a response");
});

test("removing asks first, and says what it takes with it", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("window.confirm"));
  assert.ok(js.includes("settings are deleted with it"));
});

test("identity fields are not editable", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes('NOT_EDITABLE = new Set(["id", "name", "version", "logo", "configPage", "enabled"])'));
});

// --- the logos -----------------------------------------------------

test("every logo the registry names is a real PNG", () => {
  const registry = JSON.parse(
    fs.readFileSync(path.join(webui, "..", "aria_config", "plugins.json"), "utf8"));

  for (const plugin of Object.values(registry)) {
    const file = path.join(webui, plugin.logo);
    assert.ok(fs.existsSync(file), `${plugin.logo} is missing`);
    const header = fs.readFileSync(file).subarray(0, 8);
    assert.deepEqual([...header], [137, 80, 78, 71, 13, 10, 26, 10], `${plugin.logo} is not a PNG`);
  }
});

test("a logo that fails to load leaves a labelled square", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes('image.addEventListener("error"'));
  assert.ok(js.includes("logoFallback"));
});

// --- injection -----------------------------------------------------

test("plugin names are set as text, never as markup", () => {
  // A name comes out of a JSON file a user can edit.
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes("title.textContent = plugin.name"));
  assert.ok(!/grid\.innerHTML\s*=\s*[`'"].*\$\{/.test(js),
            "tiles must not be built by interpolating into innerHTML");
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`  FAIL  ${name}\n        ${error.message}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
