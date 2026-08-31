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
  // The rule is that no plugin CARD is hard-coded -- the page this
  // replaced listed two, with their names, versions and buttons typed
  // in, so removing one meant editing the page.
  //
  // This used to be a blacklist of words, which caught the section
  // heading "Unity CLI Commands" -- a static label, not a plugin. The
  // structural check is what the word list was reaching for anyway,
  // and it is stronger: a card cannot exist in markup at all.
  const html = read("pages", "plugins", "plugins.html");

  assert.ok(!html.includes("settings-tile "), "a tile is built in JS, never typed");
  assert.ok(!html.includes("plugin-tile"), "a tile is built in JS, never typed");
  assert.ok(!/v\d+\.\d+\.\d+/.test(html), "no plugin's version is written here");

  // Both grids ship empty. Anything between the tags would be a plugin
  // that keeps appearing after it has been removed.
  for (const id of ["plugins-grid", "unity-commands-grid"]) {
    const grid = new RegExp(`id="${id}"[^>]*>([\\s\\S]*?)</div>`).exec(html);
    assert.ok(grid, `${id} is missing`);
    assert.equal(grid[1].trim(), "", `${id} has content typed into it`);
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
  // Membership, not the exact literal: the set legitimately grows
  // (discovered, dismissed), and a test that pins the whole line fails
  // for the correct change as loudly as for the wrong one.
  const js = read("pages", "plugin_config", "plugin_config.js");
  const declaration = js.slice(js.indexOf("NOT_EDITABLE = new Set("));
  const listed = declaration.slice(0, declaration.indexOf("]"));

  for (const field of ["id", "name", "version", "logo", "configPage", "enabled",
                       "discovered", "dismissed"]) {
    assert.ok(listed.includes(`"${field}"`), `${field} must not be editable`);
  }
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

// --- discovered plugins --------------------------------------------

test("a discovered plugin gets a badge, and only while it is off", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes('plugin.discovered === true && !enabled'),
            "an adopted plugin is an ordinary plugin, whatever found it");
  assert.ok(js.includes('badge.textContent = "Discovered"'));
  assert.ok(js.includes("plugin-tile-badge"));
});

test("the three card states are three different colours", () => {
  const css = read("pages", "plugins", "plugins.css");

  // Enabled is the accent, disabled is the muted text colour, and
  // discovered is amber -- a third state has to read as neither of the
  // other two.
  assert.ok(css.includes(".plugin-tile-status.is-enabled"));
  assert.ok(/\.plugin-tile-status::before\s*{[^}]*var\(--text-secondary\)/.test(css));
  assert.ok(css.includes(".plugin-tile-badge"));
  assert.ok(/\.plugin-tile-badge\s*{[^}]*#e0a415/.test(css),
            "the discovered badge must not borrow the accent, which means on");
});

test("a discovered tile carries an Enable button that does not navigate", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes("enableButton"));
  assert.ok(js.includes('control.textContent = "Enable Plugin"'));
  assert.ok(js.includes("event.stopPropagation()"),
            "pressing Enable must not also open the config page");
});

test("the Enable control is not a button inside a button", () => {
  // The tile is a <button>. A nested <button> is invalid HTML that
  // browsers repair by hoisting it out of the tile.
  const js = read("pages", "plugins", "plugins.js");
  // Anchored on the method DEFINITION, brace and all. Slicing from the
  // first mention of the name would start at the call site above it and
  // read the doc comment, which is a test that checks its own prose.
  const block = js.slice(js.indexOf("  enableButton(plugin) {"));
  const body = block.slice(0, block.indexOf("\n  },"));

  assert.ok(body.includes('createElement("span")'));
  assert.ok(body.includes('setAttribute("role", "button")'));
  assert.ok(body.includes('setAttribute("tabindex", "0")'), "it must be reachable by keyboard");
});

test("the page scans when it opens and when Rescan is pressed", () => {
  const js = read("pages", "plugins", "plugins.js");
  const html = read("pages", "plugins", "plugins.html");

  assert.ok(js.includes("this.discover(false)"), "an automatic scan on open");
  assert.ok(js.includes("this.discover(true)"), "and a forced one on Rescan");
  assert.ok(html.includes('id="plugins-rescan"'));
});

test("only the Rescan button reconsiders removed plugins", () => {
  // A removal that undoes itself every time you open a page is not a
  // removal.
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes("force: Boolean(force)"));
  // Anchored on the method definitions: "bind()" on its own also
  // matches the this.bind() call inside init, which would slice to
  // almost nothing and pass vacuously.
  const opening = js.slice(js.indexOf("  init() {"), js.indexOf("  bind() {"));
  assert.ok(opening.includes("discover(false)"));
  assert.ok(!opening.includes("discover(true)"));
});

test("a quiet scan says nothing, a fruitful one says what it found", () => {
  const js = read("pages", "plugins", "plugins.js");
  const block = js.slice(js.indexOf("reportScan(payload)"));

  assert.ok(block.includes("payload.error"), "a failed scan is admitted");
  assert.ok(block.includes("Found ${names}"));
  assert.ok(block.includes('this.say("")'),
            "an uneventful scan must not announce itself every page load");
});

test("scan news is not styled as an error", () => {
  const css = read("pages", "plugins", "plugins.css");
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(css.includes(".plugins-error.is-error"));
  assert.ok(js.includes('banner.classList.add("is-error")'));
  assert.ok(js.includes('banner.classList.remove("is-error")'));
});

// --- the config page for a discovered plugin -----------------------

test("a discovered plugin says where it came from", () => {
  const html = read("pages", "plugin_config", "plugin_config.html");
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(html.includes("This plugin was discovered on your system."));
  assert.ok(js.includes("plugin-config-banner"));
  assert.ok(js.includes("banner.hidden = !awaiting"));
});

test("the banner and the Enable button go away once it is on", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("const awaiting = plugin.discovered === true && !enabled"));
  assert.ok(js.includes("enable.hidden = !awaiting"));
});

test("the executable path is a labelled, prefilled field", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("  executable_path: {"),
            "without a FIELD_LABELS entry it renders unlabelled");
  // Non-secret fields are populated from the record, which is what
  // "pre-populated" means here.
  assert.ok(js.includes("input.value = value == null ? \"\" : String(value)"));
});

test("saving a discovered plugin adopts it", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("this.plugin?.discovered === true && !this.plugin?.enabled"));
  assert.ok(js.includes("fields.enabled = true"));
});

test("the discovered flags cannot be edited from the form", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");
  const declaration = js.slice(js.indexOf("NOT_EDITABLE = new Set("));

  assert.ok(declaration.slice(0, declaration.indexOf("]")).includes('"discovered"'));
});

// --- routing for plugins nobody had heard of ------------------------

test("any plugin config route resolves, not just the three listed", () => {
  // A discovered plugin's id is whatever was on the machine. A
  // hard-coded table cannot list a route for godot before godot was
  // found.
  const js = read("core", "router.js");

  assert.ok(js.includes("resolve(panelName)"));
  assert.ok(/plugins\\\/\[\\w\.-\]\+-config/.test(js)
            || js.includes("^plugins\\/[\\w.-]+-config$"),
            "the fallback must match plugins/<id>-config");
  assert.ok(js.includes('return "pages/plugin_config/plugin_config.html"'));
});

test("the route fallback cannot be talked into fetching anything else", () => {
  const js = read("core", "router.js");
  const pattern = js.match(/\/\^plugins\\\/([^/]+)\$\//);

  assert.ok(pattern, "the fallback must be anchored at both ends");
  assert.ok(!pattern[1].includes(".*"), "and must not be a wildcard");
});

test("navigate uses the resolved route, not a raw table lookup", () => {
  const js = read("core", "router.js");

  assert.ok(js.includes("const route = this.resolve(panelName)"));
  assert.ok(js.includes("const htmlPath = route"));
});

// --- the Unity CLI plugin ------------------------------------------

test("the Unity CLI's fields are labelled, and the mode is a dropdown", () => {
  const js = read("pages", "plugin_config", "plugin_config.js");

  for (const field of ["unity_cli_path", "unity_cli_project", "unity_cli_mode"]) {
    assert.ok(js.includes(`  ${field}: {`), `${field} renders unlabelled`);
  }
  const mode = js.slice(js.indexOf("  unity_cli_mode: {"));
  assert.ok(mode.slice(0, 200).includes('type: "select"'));
});

test("the mode dropdown's options come from the backend", () => {
  // Same rule as Ludo's model list: a page that held its own copy would
  // eventually offer a value the validator refuses.
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(!/EditMode/.test(js), "the modes must not be hard-coded in the page");
  assert.ok(js.includes("payload.choices"));
});

test("the test button says what it actually does", () => {
  // "Test connection" is wrong for a program on this machine; nothing
  // is being connected to.
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("TEST_LABELS"));
  assert.ok(js.includes('unity_cli: "Test CLI"'));
});

test("unity and unity_cli are separate plugins with separate paths", () => {
  // One runs the Editor in batchmode, the other runs the `unity`
  // command. Sharing a field would mean configuring one broke the other.
  const js = read("pages", "plugin_config", "plugin_config.js");

  assert.ok(js.includes("  unity_path: {"));
  assert.ok(js.includes("  unity_cli_path: {"));
});

// --- Unity CLI command tiles ---------------------------------------

test("commands render as tiles with name, label and state", () => {
  const js = read("pages", "plugins", "plugins.js");
  const block = js.slice(js.indexOf("  commandTile(command) {"));
  const body = block.slice(0, block.indexOf("\n  commandButton"));

  assert.ok(body.includes("command.name"));
  assert.ok(body.includes("command.label"));
  assert.ok(body.includes("plugin-tile-status"));
  assert.ok(body.includes('badge.textContent = "Discovered"'));
});

test("a command tile is not a button full of buttons", () => {
  // It holds three controls. A <button> containing buttons is invalid
  // HTML that browsers repair by hoisting them out of the tile.
  const js = read("pages", "plugins", "plugins.js");
  const block = js.slice(js.indexOf("  commandTile(command) {"));
  const body = block.slice(0, block.indexOf("\n  commandButton"));

  assert.ok(body.includes('createElement("div")'));
  assert.ok(!body.includes('createElement("button")'));
});

test("Run only appears once a command is enabled", () => {
  // Discovery lists what a CLI could do. Being listed is not permission
  // to run it, and the backend refuses a disabled command anyway.
  const js = read("pages", "plugins", "plugins.js");
  const block = js.slice(js.indexOf("  commandTile(command) {"));
  const body = block.slice(0, block.indexOf("\n  commandButton"));

  assert.ok(body.includes("if (!enabled) {"));
  assert.ok(body.indexOf('"Enable"') < body.indexOf('"Run"'),
            "Enable is the disabled branch and Run the enabled one");
});

test("every command action exists", () => {
  const js = read("pages", "plugins", "plugins.js");

  for (const action of ['"Run"', '"Edit"', '"Remove"']) {
    assert.ok(js.includes(action), `${action} is missing from the tiles`);
  }
  assert.ok(js.includes("editCommand"));
});

test("removing a command asks first and says it can come back", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes("window.confirm"));
  assert.ok(js.includes("Refresh Commands will offer it again"));
});

test("the commands section hides itself when there are none", () => {
  const js = read("pages", "plugins", "plugins.js");

  assert.ok(js.includes("section.hidden = commands.length === 0"),
            "a machine without the Unity CLI should not be asked about it");
});

test("listing commands does not run the CLI, refreshing does", () => {
  const js = read("pages", "plugins", "plugins.js");
  const opening = js.slice(js.indexOf("  init() {"), js.indexOf("  bind() {"));

  assert.ok(opening.includes("UNITY_CLI_COMMANDS_REQUEST"));
  assert.ok(!opening.includes("UNITY_CLI_REFRESH_REQUEST"),
            "opening a page must not start a program");
  assert.ok(js.includes("UNITY_CLI_REFRESH_REQUEST"));
});

// --- the terminal --------------------------------------------------

test("the terminal exists and Run opens it", () => {
  const js = read("pages", "plugins", "plugins.js");
  const html = read("pages", "plugins", "plugins.html");

  assert.ok(js.includes("UnityTerminal.run(command"));
  assert.ok(html.includes('id="unity-terminal"'));
  assert.ok(html.includes('id="unity-terminal-body"'));
});

test("the terminal has the parts the task asked for", () => {
  const html = read("pages", "plugins", "plugins.html");

  for (const id of ["unity-terminal-status", "unity-terminal-json",
                    "unity-terminal-clear", "unity-terminal-body"]) {
    assert.ok(html.includes(`id="${id}"`), `the terminal is missing ${id}`);
  }
});

test("output is written as text, never as markup", () => {
  // This is a program's output. A build log containing markup is a log.
  const js = read("pages", "plugins", "unity_terminal.js");

  assert.ok(js.includes("line.textContent = text"));
  assert.ok(!/body\.innerHTML\s*=\s*[`'"].*\$\{/.test(js));
});

test("scrollback is bounded, and says when it dropped lines", () => {
  const js = read("pages", "plugins", "unity_terminal.js");

  assert.ok(js.includes("MAX_LINES"));
  assert.ok(js.includes("earlier line(s) not shown"),
            "a truncated log must not look like a whole one");
});

test("the terminal only shows output for the command it is running", () => {
  const js = read("pages", "plugins", "unity_terminal.js");

  assert.ok(js.includes("payload.id !== this.commandId"),
            "two runs must not interleave");
});

test("JSON is pretty-printed, and only when there was JSON", () => {
  const js = read("pages", "plugins", "unity_terminal.js");

  assert.ok(js.includes("JSON.stringify(payload.json, null, 2)"));
  assert.ok(js.includes("payload.json === null || payload.json === undefined"),
            "an empty JSON panel would suggest something was lost");
});

test("failure is visible without reading", () => {
  const js = read("pages", "plugins", "unity_terminal.js");
  const css = read("pages", "plugins", "plugins.css");

  assert.ok(js.includes('this.setStatus("failed"'));
  assert.ok(js.includes("exit ${code}"));
  assert.ok(css.includes(".unity-terminal-status.is-failed"));
});

test("Unity CLI output does not go through the chat", () => {
  // progress packets land in the chat transcript, in the history the
  // next turn reads, and in the text actions are parsed from.
  const js = read("pages", "plugins", "unity_terminal.js");

  assert.ok(js.includes("IPC.UNITY_CLI_OUTPUT"));
  assert.ok(!js.includes('"progress"'));
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
                 ["pages", "plugins", "plugins.js"],
                 ["pages", "plugins", "unity_terminal.js"]];

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
