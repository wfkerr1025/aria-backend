// pages/cloud_llms/cloud_llms.js
//
// Cloud provider API key management — moved here from the old single-page
// Settings panel (webui/components/settings/settings.js) as part of
// splitting Settings into a Steam-style hub with dedicated subpages (see
// webui/pages/settings_home/). Logic and IPC packets are unchanged from
// the original implementation; only the page it lives on moved.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

function cloudLlmsLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("CloudLLMs", msg);
    }
  } catch (err) {
    console.error("[CloudLLMs LOG ERROR]", err);
  }
}

cloudLlmsLog("=== CLOUD LLMS MODULE LOADED ===");

export default {
  init() {
    cloudLlmsLog("CloudLLMs.init() called.");
    console.log("[CloudLLMs] Initializing cloud LLMs panel");

    this.apiKeysList = document.getElementById("api-keys-list");
    if (!this.apiKeysList) {
      cloudLlmsLog("ERROR: #api-keys-list not found.");
      return;
    }

    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      if (packet.type === IPC.PROVIDERS_LIST_RESULT) {
        cloudLlmsLog("providers_list_result received.");
        this.renderApiKeys(packet.payload?.providers || []);
      }

      if (packet.type === IPC.PROVIDER_KEY_SET_RESULT || packet.type === IPC.PROVIDER_KEY_DELETE_RESULT) {
        // Re-fetch rather than patch one row in place — keeps this in
        // lockstep with the Models page's own "Cloud Providers" status
        // grid, which listens for the same providers_list_result.
        bridge.send(IPC.PROVIDERS_LIST_REQUEST, {});
      }
    });

    bridge.send(IPC.PROVIDERS_LIST_REQUEST, {});

    console.log("[CloudLLMs] Cloud LLMs panel initialized");
    cloudLlmsLog("Cloud LLMs panel initialized.");
  },

  renderApiKeys(providers) {
    this.apiKeysList.innerHTML = "";

    providers.forEach((p) => {
      const row = document.createElement("div");
      row.className = "settings-group api-key-row";

      const label = document.createElement("label");
      label.textContent = p.name;
      label.className = "api-key-row-label" + (p.configured ? " configured" : "");

      const input = document.createElement("input");
      input.type = "password";
      input.placeholder = p.configured ? "•••••••• (key set)" : "Enter API key";

      const saveBtn = document.createElement("button");
      saveBtn.className = "btn btn-secondary";
      saveBtn.textContent = "Save";
      saveBtn.addEventListener("click", () => {
        const value = input.value.trim();
        if (!value) return;
        cloudLlmsLog("Saving API key for provider: " + p.name);
        bridge.send(IPC.PROVIDER_KEY_SET_REQUEST, { provider: p.name, api_key: value });
        input.value = "";
      });

      const deleteBtn = document.createElement("button");
      deleteBtn.className = "btn btn-secondary";
      deleteBtn.textContent = "Remove";
      deleteBtn.disabled = !p.configured;
      deleteBtn.addEventListener("click", () => {
        cloudLlmsLog("Removing API key for provider: " + p.name);
        bridge.send(IPC.PROVIDER_KEY_DELETE_REQUEST, { provider: p.name });
      });

      row.appendChild(label);
      row.appendChild(input);
      row.appendChild(saveBtn);
      row.appendChild(deleteBtn);
      this.apiKeysList.appendChild(row);
    });
  },
};

cloudLlmsLog("CloudLLMs exported.");
