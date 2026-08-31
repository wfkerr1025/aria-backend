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

// --- the config page header ----------------------------------------

test("the config header shows the same four things the card did", () => {
  const html = read("pages", "plugin_config", "plugin_config.html");

  for (const id of ["plugin-config-logo", "plugin-config-title",
                    "plugin-config-version", "plugin-config-status"]) {
    assert.ok(html.includes(`id="${id}"`), `the header is missing ${id}`);
  }
});

test("the header's type comes from the same variables the Settings pages use", () => {
  // "Match the Settings page typography exactly" only stays true if it
  // is the same token, not the same number typed twice.
  const css = read("pages", "plugin_config", "plugin_config.css");
  const header = css.slice(css.indexOf(".plugin-config-header h1"));

  assert.ok(header.includes("var(--font-size-xl)"));
  assert.ok(css.includes(".plugin-config-subtitle") && css.includes("var(--font-size-sm)"));
  assert.ok(!/font-size:\s*\d/.test(css), "no literal font sizes on this page");
});

test("the enabled dot is the same dot the tiles use", () => {
  const css = read("pages", "plugin_config", "plugin_config.css");

  assert.ok(css.includes(".plugin-config-status.is-enabled"));
  assert.ok(css.includes("var(--accent-primary)"));
  assert.ok(css.includes("border-radius: 50%"), "it is a dot, as on the tile");
});

test("the header logo has the same fallback the tiles have", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("renderLogo"));
  assert.ok(js.includes("image.onerror"));
  assert.ok(read("pages", "plugin_config", "plugin_config.html")
              .includes("plugin-config-logo-fallback"));
});

test("hidden means hidden, even for the flex fallback", () => {
  // The fallback sets display:flex, and any author display beats the
  // browser's own [hidden] { display: none } -- so without this rule
  // the logo and the fallback initial both showed at once.
  const css = read("pages", "plugin_config", "plugin_config.css");

  assert.ok(/\.plugin-config \[hidden\]\s*{[^}]*display:\s*none/.test(css));
});

test("the logo is the same size on the page as on the tile", () => {
  const tile = read("pages", "plugins", "plugins.css");
  const page = read("pages", "plugin_config", "plugin_config.css");
  const size = (css, selector) => {
    const block = css.slice(css.indexOf(selector));
    return block.slice(0, block.indexOf("}")).match(/width:\s*([^;]+);/)[1];
  };

  assert.equal(size(page, ".plugin-config-logo"), size(tile, ".plugin-tile-logo"),
               "a mark that resizes on click looks like a different mark");
});

// --- the fields ----------------------------------------------------

test("the model field is a dropdown, not a free-text box", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");
  const shape = js.slice(js.indexOf("  model: {"));

  assert.ok(shape.slice(0, 200).includes('type: "select"'));
  assert.ok(js.includes('createElement(isSelect ? "select" : "input")'));
});

test("the dropdown's options come from the backend, not from this file", () => {
  // A list held here would eventually offer a value the validator
  // refuses, and the form could then only be saved by not using it.
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("choicesFor"));
  assert.ok(js.includes("payload.choices"));
  assert.ok(!/ludo-fast/.test(js), "model names must not be hard-coded in the page");
});

test("a saved value the backend no longer offers is still shown", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("(not offered)"),
            "opening the page must not silently change a saved setting");
});

test("every plugin's fields have a label and a hint", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");
  const registry = JSON.parse(
    fs.readFileSync(path.join(webui, "..", "aria_config", "plugins.json"), "utf8"));
  const known = new Set(["id", "name", "version", "logo", "configPage", "enabled"]);

  for (const plugin of Object.values(registry)) {
    for (const field of Object.keys(plugin)) {
      if (known.has(field)) continue;
      assert.ok(js.includes(`  ${field}: {`),
                `${field} has no entry in FIELD_LABELS, so it renders unlabelled`);
    }
  }
});

test("the API key field is a password field", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");
  const shape = js.slice(js.indexOf("  api_key: {"));

  assert.ok(shape.slice(0, 200).includes('type: "password"'));
});

// --- the imports actually resolve ----------------------------------

test("every import in the plugin pages names an export that exists", () => {
  // This is here because the config page shipped with
  //     import Router from "../../core/router.js"
  // against a module that only exports { Router }. The page threw on
  // load, and every other test in this file still passed -- because
  // they all read the source as text, and text does not have to run.
  //
  // So this one resolves each import against the target's exports.
  const pages = [["pages", "plugin_config", "plugin_config.js"],
                 ["pages", "plugins", "plugins.js"]];

  for (const page of pages) {
    const source = read(...page);
    const dir = path.join(webui, ...page.slice(0, -1));

    for (const line of source.split("\n")) {
      const match = line.match(/^import\s+(.+?)\s+from\s+"(\.[^"]+)"/);
      if (!match) continue;

      const [, clause, relative] = match;
      const target = path.join(dir, relative);
      assert.ok(fs.existsSync(target), `${page.join("/")} imports missing ${relative}`);

      const exported = fs.readFileSync(target, "utf8");
      const names = clause.trim().startsWith("{")
        ? clause.replace(/[{}]/g, "").split(",").map((n) => n.split(" as ")[0].trim())
        : ["default"];

      for (const name of names) {
        const found = name === "default"
          ? /export\s+default\b/.test(exported)
          : new RegExp(`export\\s+(const|function|class|let)\\s+${name}\\b`).test(exported)
            || new RegExp(`export\\s*{[^}]*\\b${name}\\b`).test(exported);

        assert.ok(found,
                  `${page.join("/")} imports ${name} from ${relative}, `
                  + `which does not export it`);
      }
    }
  }
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
