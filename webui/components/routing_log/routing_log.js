// components/routing_log/routing_log.js
//
// Collapsible Routing Log panel (dual-context UI spec, item 3D) —
// shows recent local-model routing decisions (complexity, context
// length, tool use, selected model, reasoning tier) from
// backend.core.complexity_router.routing_diagnostics_snapshot(), via
// the new diagnostics_routing_request/result IPC round trip (mirrors
// the existing diagnostics_autobalance_request/result pattern exactly
// — see webui/components/diagnostics/diagnostics.js).
//
// Mounted into chat.html by chat.js (fetch+inject, same pattern
// webui/components/model_details/model_details.js already uses for
// its own sub-panels) rather than being a router-level panel of its
// own — it's chat-adjacent, not a standalone page.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

function routingLogLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("RoutingLog", msg);
    }
  } catch (err) {
    console.error("[RoutingLog LOG ERROR]", err);
  }
}

const RoutingLog = {
  init() {
    this.content = document.getElementById("routing-log-content");
    if (!this.content) {
      routingLogLog("ERROR: #routing-log-content not found.");
      return;
    }

    if (!this._bound) {
      window.addEventListener("backend-packet", (evt) => {
        const packet = evt.detail;
        if (!packet) return;

        if (packet.type === IPC.DIAGNOSTICS_ROUTING_RESULT) {
          this._render(packet.payload || packet);
          return;
        }

        // A turn just finished — refresh so the log stays current
        // without polling on a timer.
        if (packet.type === IPC.STREAM_END) {
          this.request();
        }
      });
      this._bound = true;
    }

    this.request();
    routingLogLog("RoutingLog ready.");
  },

  request() {
    bridge.send(IPC.DIAGNOSTICS_ROUTING_REQUEST, {});
  },

  _render(data) {
    if (!this.content) return;

    const decisions = (data.recent_decisions || []).slice(-10).reverse();
    if (!decisions.length) {
      this.content.innerHTML = `<div class="routing-log-empty">No routing decisions yet this session.</div>`;
      return;
    }

    this.content.innerHTML = decisions.map((d) => `
      <div class="routing-log-entry">
        <span class="routing-log-model">${d.selected_model || "—"}</span>
        <span class="routing-log-tier">${d.reasoning_tier || d.complexity || "—"}</span>
        <span class="routing-log-meta">ctx ${d.context_length ?? "—"} · tools ${d.tool_use ? "yes" : "no"}</span>
      </div>
    `).join("");

    routingLogLog(`Rendered ${decisions.length} routing decision(s).`);
  },
};

export default RoutingLog;
routingLogLog("RoutingLog module loaded.");
