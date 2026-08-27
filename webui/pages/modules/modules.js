// pages/modules/modules.js
//
// Module key management — moved here from the old single-page Settings
// panel (webui/components/settings/settings.js) as part of splitting
// Settings into a Steam-style hub with dedicated subpages (see
// webui/pages/settings_home/). Still built from the RealModuleRow /
// AddNewModuleRow split (webui/components/module_row/module_row.js) —
// that component split is unchanged by this move; only the page it's
// mounted on moved.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import { RealModuleRow, AddNewModuleRow } from "../../components/module_row/module_row.js";

function modulesPageLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ModulesPage", msg);
    }
  } catch (err) {
    console.error("[ModulesPage LOG ERROR]", err);
  }
}

modulesPageLog("=== MODULES PAGE MODULE LOADED ===");

export default {
  init() {
    modulesPageLog("Modules.init() called.");
    console.log("[ModulesPage] Initializing modules panel");

    this.moduleKeysList = document.getElementById("module-keys-list");
    this.moduleAddContainer = document.getElementById("module-add-container");

    if (!this.moduleKeysList) {
      modulesPageLog("ERROR: #module-keys-list not found.");
      return;
    }

    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      if (packet.type === IPC.MODULES_LIST_RESULT) {
        modulesPageLog("modules_list_result received.");
        this.renderModuleKeys(packet.payload?.modules || []);
      }

      if (packet.type === IPC.MODULE_KEY_SET_RESULT || packet.type === IPC.MODULE_KEY_DELETE_RESULT) {
        const ok = !!packet.payload?.ok;
        const isSet = packet.type === IPC.MODULE_KEY_SET_RESULT;
        const okMessage = isSet ? "API key saved successfully." : "API key removed.";
        const text = ok ? okMessage : (packet.payload?.reason || "Could not update module key.");
        modulesPageLog(`Key action result → ok=${ok}, message="${text}"`);
        if (typeof window.showToast === "function") {
          window.showToast(text, ok ? "success" : "error");
        }
        bridge.send(IPC.MODULES_LIST_REQUEST, {});
      }
    });

    // AddNewModuleRow is its own component, mounted once into its own
    // dedicated container — never appended to #module-keys-list. See
    // module_row.js's module docstring for why (the exact "second
    // Weather — Not configured row" bug this split fixed).
    if (this.moduleAddContainer) {
      this.moduleAddContainer.innerHTML = "";
      this.moduleAddContainer.appendChild(
        AddNewModuleRow({
          onAdd: (name, key) => {
            modulesPageLog("Adding module key: " + name);
            bridge.send(IPC.MODULE_KEY_SET_REQUEST, { module: name, api_key: key });
          },
        })
      );
    } else {
      modulesPageLog("ERROR: #module-add-container not found.");
    }

    bridge.send(IPC.MODULES_LIST_REQUEST, {});

    console.log("[ModulesPage] Modules panel initialized");
    modulesPageLog("Modules panel initialized.");
  },

  renderModuleKeys(modules) {
    this.moduleKeysList.innerHTML = "";

    modules.forEach((m) => {
      this.moduleKeysList.appendChild(
        RealModuleRow(m, {
          onSave: (name, apiKey) => {
            modulesPageLog("Saving module key: " + name);
            bridge.send(IPC.MODULE_KEY_SET_REQUEST, { module: name, api_key: apiKey });
          },
          onDelete: (name) => {
            modulesPageLog("Removing module key: " + name);
            bridge.send(IPC.MODULE_KEY_DELETE_REQUEST, { module: name });
          },
        })
      );
    });
  },
};

modulesPageLog("Modules page exported.");
