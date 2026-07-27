import { Bridge } from "../../core/bridge.js";

export const Tools = {
  init() {
    this.cache();
    this.bindUI();
    this.bindBridge();

    // Request initial tool list
    Bridge.send({ type: "tools_request" });
  },

  cache() {
    this.toolsList = document.getElementById("tools-list");
    this.toolsOutput = document.getElementById("tools-output");
    this.refreshBtn = document.getElementById("tools-refresh-btn");
  },

  bindUI() {
    this.refreshBtn.addEventListener("click", () => {
      Bridge.send({ type: "tools_request" });
    });
  },

  bindBridge() {
    Bridge.on("tools_update", (packet) => {
      this.renderTools(packet.tools);
    });

    Bridge.on("tool_result", (packet) => {
      this.renderOutput(packet.output);
    });
  },

  renderTools(tools) {
    this.toolsList.innerHTML = "";

    tools.forEach(tool => {
      const item = document.createElement("div");
      item.className = "tools-item";
      item.textContent = tool.name;

      item.addEventListener("click", () => {
        Bridge.send({
          type: "tool_execute",
          tool: tool.id
        });
      });

      this.toolsList.appendChild(item);
    });
  },

  renderOutput(output) {
    this.toolsOutput.textContent = output || "(No output)";
  }
};
