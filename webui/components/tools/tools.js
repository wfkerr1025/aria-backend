import { Bridge } from "../../core/bridge.js";

function toolsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Tools", msg);
    }
  } catch (err) {
    console.error("[Tools LOG ERROR]", err);
  }
}

toolsLog("=== TOOLS MODULE LOADED ===");

export const Tools = {
  init() {
    toolsLog("Tools.init() called.");

    this.cache();
    this.bindUI();
    this.bindBridge();

    toolsLog("Requesting initial tool list.");
    Bridge.send({ type: "tools_request" });

    toolsLog("Tools subsystem ready.");
  },

  cache() {
    toolsLog("Caching DOM elements.");

    this.toolsList = document.getElementById("tools-list");
    this.toolsOutput = document.getElementById("tools-output");
    this.refreshBtn = document.getElementById("tools-refresh-btn");

    toolsLog(
      "CACHE RESULT: " +
        JSON.stringify({
          toolsList: !!this.toolsList,
          toolsOutput: !!this.toolsOutput,
          refreshBtn: !!this.refreshBtn
        })
    );

    if (!this.toolsList || !this.toolsOutput) {
      toolsLog("ERROR: Missing required Tools DOM elements.");
    }
  },

  bindUI() {
    toolsLog("Binding UI events.");

    this.refreshBtn.addEventListener("click", () => {
      toolsLog("Refresh clicked → tools_request");
      Bridge.send({ type: "tools_request" });
    });

    toolsLog("UI bound successfully.");
  },

  bindBridge() {
    toolsLog("Binding Bridge handlers.");

    Bridge.on("tools_update", (packet) => {
      toolsLog("Received tools_update with " + packet.tools.length + " tools.");
      this.renderTools(packet.tools);
    });

    Bridge.on("tool_result", (packet) => {
      toolsLog("Received tool_result.");
      this.renderOutput(packet.output);
    });

    toolsLog("Bridge handlers registered.");
  },

  renderTools(tools) {
    toolsLog("Rendering tool list (" + tools.length + " items).");

    this.toolsList.innerHTML = "";

    tools.forEach(tool => {
      const item = document.createElement("div");
      item.className = "tools-item";
      item.textContent = tool.name;

      item.addEventListener("click", () => {
        toolsLog("Tool clicked → execute: " + tool.name + " (id=" + tool.id + ")");
        Bridge.send({
          type: "tool_execute",
          tool: tool.id
        });
      });

      this.toolsList.appendChild(item);
    });

    toolsLog("Tool list render complete.");
  },

  renderOutput(output) {
    toolsLog("Rendering tool output. Length=" + (output?.length || 0));
    this.toolsOutput.textContent = output || "(No output)";
  }
};

toolsLog("Tools exported.");
