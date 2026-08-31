// pages/plugins/plugins.js
//
// The Plugins hub. Every tile comes from aria_config/plugins.json, by
// way of plugin_registry_list_request.
//
// Nothing about a plugin is written in the markup. The page this
// replaced listed two plugins as literal HTML -- "Self-Improvement
// Engine" and "Diagnostics Enhancer" -- with their names, versions and
// buttons typed in by hand, so removing one meant editing the page and
// the "Installed Plugins" heading was a claim nobody could check. A
// plugin listed in markup is a plugin that keeps appearing after it has
// been uninstalled.
//
// WHY IT REUSES .settings-tile
// ----------------------------
// The requirement is that this page matches the Settings page exactly:
// same spacing, corners, hover, typography and grid. Reusing the class
// is the only way that stays true. A copied ruleset is identical on the
// day it is written and drifts on the first change to either page.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

function pluginLog(message) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("PluginsPage", message);
    }
  } catch (error) {
    console.error("[PluginsPage LOG ERROR]", error);
  }
}

const Plugins = {
  bound: false,

  init() {
    this.bind();
    this.refresh();
  },

  bind() {
    // The window listener is attached once for the life of the page
    // module. Router.navigate() re-imports this module on every visit
    // and an ES module is cached, so init() runs again on the SAME
    // object -- without this guard the listener accumulates and one
    // packet renders the page three times.
    if (!this.bound) {
      window.addEventListener("backend-packet", (event) => this.onPacket(event.detail));
      this.bound = true;
    }
  },

  refresh() {
    pluginLog("requesting the plugin registry");
    bridge.send(IPC.PLUGIN_REGISTRY_LIST_REQUEST, {});
  },

  onPacket(packet) {
    if (!packet) return;
    const payload = packet.payload || packet;

    if (packet.type === IPC.PLUGIN_REGISTRY_LIST_RESULT) {
      this.render(payload.plugins || []);
      return;
    }

    // A removal or a change made on a config page changes what this
    // page should show, so it asks again rather than guessing.
    if (packet.type === IPC.PLUGIN_REMOVE_RESULT
        || packet.type === IPC.PLUGIN_UPDATE_RESULT) {
      this.refresh();
      return;
    }

    if (packet.type === "error" && String(payload.request || "").startsWith("plugin_")) {
      this.showError(payload.message || "That plugin operation was refused.");
    }
  },

  render(plugins) {
    const grid = document.getElementById("plugins-grid");
    const empty = document.getElementById("plugins-empty");
    if (!grid) return;

    grid.innerHTML = "";

    if (!plugins.length) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;

    plugins.forEach((plugin) => grid.appendChild(this.tile(plugin)));
    pluginLog(`rendered ${plugins.length} plugin tile(s)`);
  },

  tile(plugin) {
    const enabled = plugin.enabled === true;

    // Built with createElement rather than innerHTML. A plugin's name
    // and version come out of a JSON file a user can edit, and this
    // page would otherwise be injecting it as markup.
    const button = document.createElement("button");
    button.type = "button";
    button.className = "settings-tile plugin-tile" + (enabled ? "" : " is-disabled");
    button.dataset.pluginId = plugin.id || "";
    button.dataset.configPage = plugin.configPage || "";

    button.appendChild(this.logo(plugin));

    const title = document.createElement("span");
    title.className = "settings-tile-title";
    title.textContent = plugin.name || plugin.id || "Unnamed plugin";
    button.appendChild(title);

    const version = document.createElement("span");
    version.className = "settings-tile-subtitle";
    version.textContent = `v${plugin.version || "0.0.0"}`;
    button.appendChild(version);

    const status = document.createElement("span");
    status.className = "plugin-tile-status" + (enabled ? " is-enabled" : "");
    status.textContent = enabled ? "Enabled" : "Disabled";
    button.appendChild(status);

    button.addEventListener("click", () => this.open(plugin));
    return button;
  },

  logo(plugin) {
    const source = String(plugin.logo || "").trim();
    if (!source) return this.logoFallback(plugin);

    const image = document.createElement("img");
    image.className = "plugin-tile-logo";
    image.alt = "";
    image.src = source;

    // A missing file leaves a labelled square rather than a broken
    // image and a tile one line shorter than its neighbours.
    image.addEventListener("error", () => {
      image.replaceWith(this.logoFallback(plugin));
    });
    return image;
  },

  logoFallback(plugin) {
    const box = document.createElement("span");
    box.className = "plugin-tile-logo-fallback";
    box.setAttribute("aria-hidden", "true");
    box.textContent = String(plugin.name || plugin.id || "?").trim().charAt(0) || "?";
    return box;
  },

  open(plugin) {
    const page = String(plugin.configPage || "").trim();
    if (!page) {
      this.showError(`${plugin.name || plugin.id} has no configuration page.`);
      return;
    }
    pluginLog(`opening ${plugin.id} → plugins/${page}`);
    window.dispatchEvent(new CustomEvent("navigatePanel", { detail: `plugins/${page}` }));
  },

  showError(message) {
    const banner = document.getElementById("plugins-error");
    if (!banner) return;
    banner.textContent = message;
    banner.hidden = false;
    pluginLog(`error: ${message}`);
  },
};

export default Plugins;
