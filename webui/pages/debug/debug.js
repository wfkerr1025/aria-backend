// pages/debug/debug.js
// IPC Debug panel — renders webui/core/debug_log.js's shared ring buffer.
// The buffer itself is populated by listeners registered once at bootstrap
// (see core/app.js), so it already has history the first time this page is
// opened, not just events from that point forward.

import { DebugLog } from "../../core/debug_log.js";

function fmtTs(ts) {
  const d = new Date(ts);
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

function fmtPayload(payload) {
  if (payload === undefined) return "";
  try {
    const json = JSON.stringify(payload);
    return json.length > 200 ? json.slice(0, 200) + "…" : json;
  } catch (_err) {
    return String(payload);
  }
}

const Debug = {
  init() {
    this.clearBtn = document.getElementById("debug-clear-btn");
    this.copyBtn = document.getElementById("debug-copy-btn");
    this.saveBtn = document.getElementById("debug-save-btn");
    this.connectionList = document.getElementById("debug-connection-list");
    this.errorList = document.getElementById("debug-error-list");
    this.sentList = document.getElementById("debug-sent-list");
    this.receivedList = document.getElementById("debug-received-list");
    this.truthGrid = document.getElementById("debug-truth-grid");

    if (this.clearBtn) {
      this.clearBtn.addEventListener("click", () => {
        DebugLog.clear();
        this._renderAll();
      });
    }

    if (this.copyBtn) {
      this.copyBtn.addEventListener("click", () => {
        const all = DebugLog.getAll();
        const text = all.map(e => `${fmtTs(e.ts)} ${e.type} ${fmtPayload(e.payload)}`).join("\n");
        navigator.clipboard.writeText(text);
      });
    }

    if (this.saveBtn) {
      this.saveBtn.addEventListener("click", () => {
        const all = DebugLog.getAll();
        const text = all.map(e => `${fmtTs(e.ts)} ${e.type} ${fmtPayload(e.payload)}`).join("\n");
        const blob = new Blob([text], { type: "text/plain" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "aria_debug_log.txt";
        a.click();
        URL.revokeObjectURL(url);
      });
    }

    // Router.navigate() re-runs init() on every visit to this panel
    // (fresh DOM each time — see core/router.js), so any previous
    // subscription must be torn down first or entries would render twice.
    if (this._unsubscribe) {
      this._unsubscribe();
    }
    this._unsubscribe = DebugLog.subscribe(() => this._renderAll());

    this._renderAll();
  },

  _renderAll() {
    const all = DebugLog.getAll();

    this._renderList(this.connectionList, all.filter(e => e.direction === "connection"), "No connection events yet.");
    this._renderList(this.errorList, all.filter(e => e.type === "error"), "No errors yet.");
    this._renderList(this.sentList, all.filter(e => e.direction === "sent"), "Nothing sent yet.");
    this._renderList(this.receivedList, all.filter(e => e.direction === "received" && e.type !== "error"), "Nothing received yet.");
    this._renderTruthSnapshot(all);
  },

  // -------------------------------------------------------
  // Batch 2.5 — "Routing Truth Snapshot". Reads directly from the
  // shared DebugLog ring buffer (already populated by listeners
  // registered once at bootstrap — see core/app.js) rather than
  // maintaining its own separate cache or issuing its own requests:
  // whatever mode_status_result/providers_list_result/heartbeat every
  // other panel has already triggered is what this reflects, exactly as
  // asked ("This panel should read directly from the last
  // mode_status_result").
  // -------------------------------------------------------
  _findLast(entries, predicate) {
    for (let i = entries.length - 1; i >= 0; i -= 1) {
      if (predicate(entries[i])) return entries[i];
    }
    return null;
  },

  _renderTruthSnapshot(all) {
    if (!this.truthGrid) return;

    const lastModeStatus = this._findLast(all, (e) => e.direction === "received" && e.type === "mode_status_result");
    const lastProviders = this._findLast(all, (e) => e.direction === "received" && e.type === "providers_list_result");
    const lastHeartbeat = this._findLast(all, (e) => e.direction === "received" && e.type === "heartbeat");
    const lastConnection = this._findLast(all, (e) => e.direction === "connection");

    if (!lastModeStatus) {
      this.truthGrid.innerHTML = `<p class="debug-empty">No mode_status_result received yet this session.</p>`;
      return;
    }

    const status = lastModeStatus.payload || {};
    const provider = status.cloud_provider || null;
    const providerEntry = provider
      ? (lastProviders?.payload?.providers || []).find((p) => p.name === provider)
      : null;

    const heartbeatAgeMs = lastHeartbeat ? Date.now() - lastHeartbeat.ts : null;
    const heartbeatStale = heartbeatAgeMs === null || heartbeatAgeMs > 30000; // mirrors bridge.js's HEARTBEAT_TIMEOUT_MS
    const isDisconnected = lastConnection?.type === "disconnected";

    const rows = [
      ["routing_mode", status.routing_mode ?? "—", "ok"],
      ["cloud_provider", status.cloud_provider ?? "—", provider ? "ok" : "warn"],
      ["active_model_id", status.active_model_id ?? "—", status.active_model_id ? "ok" : "warn"],
      ["location", status.location ?? "—", "ok"],
      [
        "hasApiKey",
        provider ? String(!!providerEntry?.configured) : "n/a",
        provider && !providerEntry?.configured ? "bad" : "ok",
      ],
      [
        "lastHeartbeat",
        lastHeartbeat ? `${fmtTs(lastHeartbeat.ts)} (${Math.round(heartbeatAgeMs / 1000)}s ago)` : "never",
        heartbeatStale ? "warn" : "ok",
      ],
      ["connection", isDisconnected ? "disconnected" : (lastConnection?.type || "unknown"), isDisconnected ? "bad" : "ok"],
    ];

    this.truthGrid.innerHTML = rows.map(([label, value, tone]) => `
      <div class="debug-truth-item">
        <div class="debug-truth-label">${label}</div>
        <div class="debug-truth-value debug-truth-${tone}">${value}</div>
      </div>
    `).join("");
  },

  _renderList(container, entries, emptyText) {
    if (!container) return;

    if (!entries.length) {
      container.innerHTML = `<p class="debug-empty">${emptyText}</p>`;
      return;
    }

    container.innerHTML = "";
    // Most-recent-first — column-reverse in CSS handles the visual order,
    // so entries are appended in chronological order and the newest ends
    // up nearest the top of the (reversed) flex container.
    entries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = `debug-entry debug-entry-${entry.type}`;

      const ts = document.createElement("span");
      ts.className = "debug-entry-ts";
      ts.textContent = fmtTs(entry.ts);

      const type = document.createElement("span");
      type.className = "debug-entry-type";
      type.textContent = entry.type;

      const payload = document.createElement("span");
      payload.className = "debug-entry-payload";
      payload.textContent = fmtPayload(entry.payload);

      row.appendChild(ts);
      row.appendChild(type);
      row.appendChild(payload);
      container.appendChild(row);
    });
  },
};

export default Debug;
