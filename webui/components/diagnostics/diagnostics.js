import { Bridge } from "../../core/bridge.js";

export const Diagnostics = {
  init() {
    this.cache();
    this.bindUI();
    this.bindBridge();
  },

  cache() {
    this.systemStatus = document.getElementById("diag-system-status");
    this.engineHealth = document.getElementById("diag-engine-health");
    this.packetLog = document.getElementById("diag-packet-log");
    this.refreshBtn = document.getElementById("diag-refresh-btn");
  },

  bindUI() {
    this.refreshBtn.addEventListener("click", () => {
      Bridge.send({ type: "diagnostics_request" });
    });
  },

  bindBridge() {
    Bridge.on("diagnostics_update", (packet) => {
      this.updateDiagnostics(packet);
    });

    Bridge.on("packet_log", (packet) => {
      this.addPacketLog(packet.entry);
    });
  },

  updateDiagnostics(packet) {
    const { system, engine } = packet;

    this.systemStatus.innerHTML = `
      <div><strong>CPU:</strong> ${system.cpu}</div>
      <div><strong>Memory:</strong> ${system.memory}</div>
      <div><strong>Uptime:</strong> ${system.uptime}</div>
    `;

    this.engineHealth.innerHTML = `
      <div><strong>Router:</strong> ${engine.router}</div>
      <div><strong>LLM Engine:</strong> ${engine.llm}</div>
      <div><strong>Tools:</strong> ${engine.tools}</div>
    `;
  },

  addPacketLog(entry) {
    const line = document.createElement("div");
    line.textContent = entry;
    this.packetLog.appendChild(line);
    this.packetLog.scrollTop = this.packetLog.scrollHeight;
  }
};
