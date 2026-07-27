import { Bridge } from "../../core/bridge.js";

export const Plugins = {
  init() {
    this.cache();
    this.bindUI();
    this.bindBridge();

    // Request initial plugin list
    Bridge.send({ type: "plugins_request" });
  },

  cache() {
    this.pluginsList = document.getElementById("plugins-list");
    this.pluginsOutput = document.getElementById("plugins-output");
    this.refreshBtn = document.getElementById("plugins-refresh-btn");
  },

  bindUI() {
    this.refreshBtn.addEventListener("click", () => {
      Bridge.send({ type: "plugins_request" });
    });
  },

  bindBridge() {
    Bridge.on("plugins_update", (packet) => {
      this.renderPlugins(packet.plugins);
    });

    Bridge.on("plugin_result", (packet) => {
      this.renderOutput(packet.output);
    });
  },

  renderPlugins(plugins) {
    this.pluginsList.innerHTML = "";

    plugins.forEach(plugin => {
      const item = document.createElement("div");
      item.className = "plugins-item";
      item.textContent = plugin.name;

      if (plugin.active) {
        item.classList.add("active");
      }

      item.addEventListener("click", () => {
        Bridge.send({
          type: "plugin_execute",
          plugin: plugin.id
        });
      });

      this.pluginsList.appendChild(item);
    });
  },

  renderOutput(output) {
    this.pluginsOutput.textContent = output || "(No output)";
  }
};
