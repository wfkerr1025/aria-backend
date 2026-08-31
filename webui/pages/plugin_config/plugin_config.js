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
import Router from "../../core/router.js";

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
  api_key: { label: "API key", type: "password",
             hint: "Stored in aria_config/plugins.json." },
  model: { label: "Model", type: "text", optional: true,
           hint: "Which Ludo.ai model to use. Leave empty for their default." },
};

// Never editable on this page. id and configPage are identity -- a form
// that could rewrite them could rename a plugin into another's slot --
// and the rest is metadata rather than settings.
const NOT_EDITABLE = new Set(["id", "name", "version", "logo", "configPage", "enabled"]);

const PluginConfig = {
  bound: false,
  pluginId: null,
  plugin: null,

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
      this.render();
      return;
    }

    if (packet.type === IPC.PLUGIN_UPDATE_RESULT) {
      if (payload.plugin?.id !== this.pluginId) return;
      this.plugin = payload.plugin;
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

    const title = document.getElementById("plugin-config-title");
    if (title) title.textContent = plugin.name || plugin.id || "Plugin";

    const version = document.getElementById("plugin-config-version");
    if (version) version.textContent = `v${plugin.version || "0.0.0"}`;

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

  field(name, value) {
    const shape = FIELD_LABELS[name] || { label: name, type: "text" };

    const wrapper = document.createElement("label");
    wrapper.className = "plugin-config-field";

    const caption = document.createElement("span");
    caption.className = "plugin-config-label";
    caption.textContent = shape.label + (shape.optional ? " (optional)" : "");
    wrapper.appendChild(caption);

    const input = document.createElement("input");
    input.type = shape.type;
    input.className = "plugin-config-input";
    input.dataset.field = name;
    input.autocomplete = shape.type === "password" ? "off" : "on";

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

  save() {
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
