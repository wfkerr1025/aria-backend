import { Bridge } from "../../core/bridge.js";

function pluginsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Plugins", msg);
    }
  } catch (err) {
    console.error("[Plugins LOG ERROR]", err);
  }
}

pluginsLog("=== PLUGINS MODULE LOADED ===");

export const Plugins = {
  init() {
    pluginsLog("Plugins.init() called.");

    this.cache();
    this.bindUI();
    this.bindBridge();

    pluginsLog("Requesting initial plugin list.");
    Bridge.send({ type: "plugins_request" });

    pluginsLog("Plugins subsystem ready.");
  },

  cache() {
    pluginsLog("Caching DOM elements.");

    this.pluginsList = document.getElementById("plugins-list");
    this.pluginsOutput = document.getElementById("plugins-output");
    this.refreshBtn = document.getElementById("plugins-refresh-btn");

    pluginsLog(
      "CACHE RESULT: " +
        JSON.stringify({
          pluginsList: !!this.pluginsList,
          pluginsOutput: !!this.pluginsOutput,
          refreshBtn: !!this.refreshBtn
        })
    );

    if (!this.pluginsList || !this.pluginsOutput) {
      pluginsLog("ERROR: Missing required Plugins DOM elements.");
    }
  },

  bindUI() {
    pluginsLog("Binding UI events.");

    this.refreshBtn.addEventListener("click", () => {
      pluginsLog("Refresh clicked → plugins_request");
      Bridge.send({ type: "plugins_request" });
    });

    pluginsLog("UI bound successfully.");
  },

  bindBridge() {
    pluginsLog("Binding Bridge handlers.");

    Bridge.on("plugins_update", (packet) => {
      pluginsLog("Received plugins_update with " + packet.plugins.length + " plugins.");
      this.renderPlugins(packet.plugins);
    });

    Bridge.on("plugin_result", (packet) => {
      pluginsLog("Received plugin_result.");
      this.renderOutput(packet.output);
    });

    pluginsLog("Bridge handlers registered.");
  },

  renderPlugins(plugins) {
    pluginsLog("Rendering plugin list (" + plugins.length + " items).");

    this.pluginsList.innerHTML = "";

    plugins.forEach(plugin => {
      const item = document.createElement("div");
      item.className = "plugins-item";
      item.textContent = plugin.name;

      if (plugin.active) {
        item.classList.add("active");
      }

      item.addEventListener("click", () => {
        pluginsLog("Plugin clicked → execute: " + plugin.name + " (id=" + plugin.id + ")");
        Bridge.send({
          type: "plugin_execute",
          plugin: plugin.id
        });
      });

      this.pluginsList.appendChild(item);
    });

    pluginsLog("Plugin list render complete.");
  },

  renderOutput(output) {
    pluginsLog("Rendering plugin output. Length=" + (output?.length || 0));
    this.pluginsOutput.textContent = output || "(No output)";
  }
};

pluginsLog("Plugins exported.");
