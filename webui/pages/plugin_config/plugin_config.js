// pages/plugin_config/plugin_config.js
//
// The configuration page for one plugin.
//
// ONE PAGE, THREE ROUTES
// ----------------------
// plugins/unity-config, plugins/blender-config and plugins/ludo-config
// all load this. They are three routes and one implementation, because
// they do the same four things -- read the plugin, edit its fields,
// turn it on or off, remove it -- and the only difference between them
// is which fields exist.
//
// Three files would be three copies of the save logic, and the third
// one would be the one that stopped redacting the API key.
//
// The fields themselves come from the plugin's own record, so the form
// and aria_config/plugins.json cannot hold different opinions about
// what a plugin has.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import { Router } from "../../core/router.js";

function configLog(message) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("PluginConfig", message);
    }
  } catch (error) {
    console.error("[PluginConfig LOG ERROR]", error);
  }
}

// How each field is presented. A field not named here still renders --
// as text, which is the safe default -- so adding one to plugins.json
// does not require editing this page.
const FIELD_LABELS = {
  unity_path: { label: "Unity executable", type: "text",
                hint: "The full path to Unity.exe or the Unity binary." },
  project_path: { label: "Unity project folder", type: "text", optional: true,
                  hint: "Optional. Defaults to ARIA's current working directory." },
  blender_path: { label: "Blender executable", type: "text",
                  hint: "The full path to blender.exe or the Blender binary." },
  executable_path: { label: "Program", type: "text",
                     hint: "The full path to the program ARIA should run." },
  unity_cli_path: { label: "Unity CLI executable", type: "text",
                    hint: "The full path to unity.cmd, unity.exe or unity." },
  unity_cli_project: { label: "Default project", type: "text", optional: true,
                       hint: "Passed as --project to every command that runs." },
  unity_cli_mode: { label: "Default mode", type: "select", optional: true,
                    hint: "Passed as --mode. Leave empty to pass no mode at all." },
  api_key: { label: "API key", type: "password",
             hint: "Stored in aria_config/plugins.json." },
  model: { label: "Model", type: "select", optional: true,
           hint: "Which Ludo.ai model to use. Leave empty for their default." },
};

// Never editable on this page. id and configPage are identity -- a form
// that could rewrite them could rename a plugin into another's slot --
// and the rest is metadata rather than settings.
const NOT_EDITABLE = new Set(["id", "name", "version", "logo", "configPage", "enabled",
                              "discovered", "dismissed"]);

// What the test button says. Anything not named here connects to
// something, and "Test connection" is right for it.
const TEST_LABELS = {
  unity_cli: "Test CLI",
  unity: "Test Unity",
  blender: "Test Blender",
};

const PluginConfig = {
  bound: false,
  pluginId: null,
  plugin: null,
  choices: {},

  init() {
    this.pluginId = this.idFromRoute();
    this.bind();

    if (!this.pluginId) {
      this.say("This page was opened without a plugin.", true);
      return;
    }
    bridge.send(IPC.PLUGIN_GET_REQUEST, { id: this.pluginId });
  },

  /**
   * Which plugin this route is for.
   *
   * The router keeps the active panel name, and the three routes are
   * "plugins/unity-config" and friends -- so the id is the part before
   * "-config". Read from the route rather than stored, because the
   * module is cached across navigations and a stored id would be the
   * previous page's.
   */
  idFromRoute() {
    const panel = String(Router?.currentPanel || "");
    const match = panel.match(/^plugins\/(.+)-config$/);
    return match ? match[1] : null;
  },

  bind() {
    if (!this.bound) {
      window.addEventListener("backend-packet", (event) => this.onPacket(event.detail));
      this.bound = true;
    }

    document.getElementById("plugin-config-back")
      ?.addEventListener("click", () => {
        window.dispatchEvent(new CustomEvent("navigatePanel", { detail: "plugins" }));
      });

    document.getElementById("plugin-config-save")
      ?.addEventListener("click", () => this.save());

    document.getElementById("plugin-config-test")
      ?.addEventListener("click", () => {
        this.say("Testing…");
        bridge.send(IPC.PLUGIN_TEST_REQUEST, { id: this.pluginId });
      });

    document.getElementById("plugin-config-enable")
      ?.addEventListener("click", () => this.save({ enable: true }));

    document.getElementById("plugin-config-remove")
      ?.addEventListener("click", () => this.remove());
  },

  onPacket(packet) {
    if (!packet) return;
    const payload = packet.payload || packet;

    if (packet.type === IPC.PLUGIN_GET_RESULT) {
      // Several config pages may have been visited this session; only
      // the packet for THIS plugin is ours.
      if (payload.plugin?.id !== this.pluginId) return;
      this.plugin = payload.plugin;
      this.choices = payload.choices || {};
      this.render();
      return;
    }

    if (packet.type === IPC.PLUGIN_UPDATE_RESULT) {
      if (payload.plugin?.id !== this.pluginId) return;
      this.plugin = payload.plugin;
      if (payload.choices) this.choices = payload.choices;
      this.render();
      this.say("Saved.");
      return;
    }

    if (packet.type === IPC.PLUGIN_TEST_RESULT) {
      if (payload.id !== this.pluginId) return;
      this.say(payload.message || (payload.ok ? "Connected." : "No connection."),
               !payload.ok);
      return;
    }

    if (packet.type === IPC.PLUGIN_REMOVE_RESULT) {
      if (payload.id !== this.pluginId) return;
      window.dispatchEvent(new CustomEvent("navigatePanel", { detail: "plugins" }));
      return;
    }

    if (packet.type === "error" && String(payload.request || "").startsWith("plugin_")) {
      // A validation failure arrives here. It is the form's answer, not
      // a broken connection, and it names every problem at once.
      this.say(payload.message || "That change was refused.", true);
    }
  },

  render() {
    const plugin = this.plugin || {};

    const name = plugin.name || plugin.id || "Plugin";
    const enabled = plugin.enabled === true;

    const title = document.getElementById("plugin-config-title");
    if (title) title.textContent = name;

    const version = document.getElementById("plugin-config-version");
    if (version) version.textContent = `v${plugin.version || "0.0.0"}`;

    // The header's status repeats what the checkbox below says, on
    // purpose: the checkbox is a control and reads as "what would I
    // like", the dot is a readout and reads as "what is it now".
    const status = document.getElementById("plugin-config-status");
    if (status) {
      status.textContent = enabled ? "Enabled" : "Disabled";
      status.classList.toggle("is-enabled", enabled);
    }

    // Discovered and still off: ARIA found this and the user has not
    // adopted it. Once it is on, the banner and the Enable button have
    // nothing left to say and go away.
    const awaiting = plugin.discovered === true && !enabled;

    const banner = document.getElementById("plugin-config-banner");
    if (banner) banner.hidden = !awaiting;

    const enable = document.getElementById("plugin-config-enable");
    if (enable) enable.hidden = !awaiting;

    // "Test connection" is wrong for a program on this machine: nothing
    // is being connected to. The button does the same thing either way;
    // only what it claims to be doing changes.
    const test = document.getElementById("plugin-config-test");
    if (test) test.textContent = TEST_LABELS[plugin.id] || "Test connection";

    this.renderLogo(plugin, name);

    const toggle = document.getElementById("plugin-config-enabled");
    if (toggle) toggle.checked = plugin.enabled === true;

    const label = document.getElementById("plugin-config-enabled-label");
    if (label) label.textContent = plugin.enabled === true ? "Enabled" : "Disabled";

    const fields = document.getElementById("plugin-config-fields");
    if (!fields) return;
    fields.innerHTML = "";

    Object.keys(plugin)
      .filter((name) => !NOT_EDITABLE.has(name))
      .forEach((name) => fields.appendChild(this.field(name, plugin[name])));
  },

  /**
   * The plugin's mark, or its initial if the file is missing.
   *
   * Same fallback as the tiles: a labelled square rather than a broken
   * image, so a missing logo costs a logo and not the layout.
   */
  renderLogo(plugin, name) {
    const image = document.getElementById("plugin-config-logo");
    const fallback = document.getElementById("plugin-config-logo-fallback");
    if (!image || !fallback) return;

    const show = (useImage) => {
      image.hidden = !useImage;
      fallback.hidden = useImage;
    };

    fallback.textContent = String(name).charAt(0) || "?";
    image.alt = `${name} logo`;

    if (!plugin.logo) {
      show(false);
      return;
    }

    image.onerror = () => show(false);
    image.onload = () => show(true);
    image.src = plugin.logo;
    show(true);
  },

  field(name, value) {
    const shape = FIELD_LABELS[name] || { label: name, type: "text" };

    const wrapper = document.createElement("label");
    wrapper.className = "plugin-config-field";

    const caption = document.createElement("span");
    caption.className = "plugin-config-label";
    caption.textContent = shape.label + (shape.optional ? " (optional)" : "");
    wrapper.appendChild(caption);

    // A dropdown is an <select>, not an <input>, but everything after
    // this point -- the class, the dataset, save() -- treats them the
    // same, so the form has one collection routine rather than two.
    const isSelect = shape.type === "select";
    const input = document.createElement(isSelect ? "select" : "input");
    if (!isSelect) input.type = shape.type;
    input.className = "plugin-config-input";
    input.dataset.field = name;
    if (!isSelect) input.autocomplete = shape.type === "password" ? "off" : "on";

    if (isSelect) {
      // The options come from the backend, which is also what validates
      // the saved value. A list held here instead would eventually let
      // the page offer a choice the validator refuses.
      const choices = this.choicesFor(name);
      choices.forEach((choice) => {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice === "" ? "Their default" : choice;
        input.appendChild(option);
      });

      const current = value == null ? "" : String(value);
      if (current !== "" && !choices.includes(current)) {
        // A value saved before this list existed still has to be
        // selectable, or opening the page would silently change it.
        const option = document.createElement("option");
        option.value = current;
        option.textContent = `${current} (not offered)`;
        input.appendChild(option);
      }
      input.value = current;

      wrapper.appendChild(input);
      if (shape.hint) {
        const hint = document.createElement("span");
        hint.className = "plugin-config-hint";
        hint.textContent = shape.hint;
        wrapper.appendChild(hint);
      }
      return wrapper;
    }

    // A secret arrives as "configured" or "", never as the key itself.
    // The box is left empty and its placeholder says which, so a user
    // can see that a key is set without the page ever holding one.
    if (shape.type === "password") {
      input.value = "";
      input.placeholder = value === "configured"
        ? "A key is set. Type a new one to replace it."
        : "No key set.";
      input.dataset.secret = "true";
    } else {
      input.value = value == null ? "" : String(value);
    }
    wrapper.appendChild(input);

    if (shape.hint) {
      const hint = document.createElement("span");
      hint.className = "plugin-config-hint";
      hint.textContent = shape.hint;
      wrapper.appendChild(hint);
    }

    return wrapper;
  },

  /** What this field may be set to, as the backend reported it. */
  choicesFor(name) {
    const table = this.choices || {};
    const list = table[name];
    return Array.isArray(list) ? list : [];
  },

  /**
   * Send the form.
   *
   * enable=true is the "Enable Plugin" button, and it is the same
   * save: a discovered plugin is switched on by saving the path that
   * was found for it, not by a separate action that could turn on an
   * integration pointing at nothing.
   */
  save({ enable = false } = {}) {
    if (!this.pluginId) return;

    const fields = {};
    document.querySelectorAll(".plugin-config-input").forEach((input) => {
      const name = input.dataset.field;
      if (!name) return;

      // An untouched secret box means "leave the key alone", not "clear
      // it". Sending "" here would delete a working key every time
      // somebody opened the page and pressed Save.
      if (input.dataset.secret === "true" && input.value === "") return;

      fields[name] = input.value;
    });

    const toggle = document.getElementById("plugin-config-enabled");
    if (toggle) fields.enabled = toggle.checked;

    // Saving a discovered plugin adopts it. Somebody who opened the
    // page ARIA offered them, checked the path and pressed Save has
    // said yes; making them also find the checkbox would be asking the
    // same question twice.
    if (enable || (this.plugin?.discovered === true && !this.plugin?.enabled)) {
      fields.enabled = true;
      if (toggle) toggle.checked = true;
    }

    configLog(`saving ${this.pluginId}: ${Object.keys(fields).join(", ")}`);
    this.say("Saving…");
    bridge.send(IPC.PLUGIN_UPDATE_REQUEST, { id: this.pluginId, fields });
  },

  async remove() {
    const name = this.plugin?.name || this.pluginId;

    // Asked before it happens, and the question says what survives it:
    // the settings go with the plugin, which is the part a user would
    // not expect from the word "remove".
    const confirmed = window.confirm(
      `Remove ${name}?\n\nIts settings are deleted with it. `
      + `ARIA will stop offering this integration until it is added again.`);
    if (!confirmed) return;

    configLog(`removing ${this.pluginId}`);
    bridge.send(IPC.PLUGIN_REMOVE_REQUEST, { id: this.pluginId });
  },

  say(message, isError = false) {
    const banner = document.getElementById("plugin-config-message");
    if (!banner) return;
    banner.textContent = message;
    banner.hidden = false;
    banner.classList.toggle("is-error", Boolean(isError));
  },
};

export default PluginConfig;
