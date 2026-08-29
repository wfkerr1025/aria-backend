// pages/workspaces/workspaces.js
//
// ARIA Control Center: the projects ARIA is working in, what is staged
// in each, and whether the model can be asked to act.
//
// A view. Every value comes from workspace_list_result or
// workspace_details_result, and every button sends a packet. The page
// computes no paths and holds no second opinion about where a project
// is -- that opinion would eventually disagree with the boundary the
// file tools actually enforce, and the disagreement would only show up
// as a file written somewhere nobody expected.
//
// One page, two result shapes. A mutation answers with the state it
// produced -- add/remove/set-primary return the list, commit/discard/
// rollback return the detail -- so nothing here has to ask twice for the
// result of its own click.
//
// Commit and discard carry the words the user typed. The consent is
// checked in ghost_workspace against the same negation table
// action_plan uses, so "don't apply the changes yet" refuses a commit
// for the same reason it refuses a live run, and this page cannot talk
// its way past it.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

const Workspaces = {
  workspaces: [],
  selectedId: null,
  details: null,

  init() {
    this.bind();
    window.addEventListener("backend-packet", (evt) => this.onPacket(evt.detail));
    this.refresh();
  },

  bind() {
    document.getElementById("ws-add")
      ?.addEventListener("click", () => this.addWorkspace());
    document.getElementById("ws-refresh")
      ?.addEventListener("click", () => this.refresh());
  },

  onPacket(packet) {
    if (!packet) return;
    const payload = packet.payload || packet;

    if (packet.type === IPC.WORKSPACE_LIST_RESULT) {
      this.workspaces = payload.workspaces || [];
      if (!this.workspaces.some((w) => w.id === this.selectedId)) {
        this.selectedId = this.workspaces.find((w) => w.primary)?.id
          || this.workspaces[0]?.id
          || null;
        this.details = null;
      }
      this.renderList();
      if (this.selectedId && !this.details) this.loadDetails(this.selectedId);
    } else if (packet.type === IPC.WORKSPACE_DETAILS_RESULT) {
      this.details = payload;
      this.selectedId = payload.id;
      this.renderDetails();
      this.renderList();
    } else if (packet.type === "error" && String(payload.request || "").startsWith("workspace_")) {
      this.showError(payload.message || "That workspace operation was refused.");
    }
  },

  refresh() { bridge.send(IPC.WORKSPACE_LIST_REQUEST, {}); },
  loadDetails(id) { bridge.send(IPC.WORKSPACE_DETAILS_REQUEST, { id }); },

  addWorkspace() {
    const path = window.prompt("Project directory:");
    if (path === null) return;      // cancelled, not cleared
    // Sent as typed. Validation belongs to the backend, which knows what
    // the file tools will accept; a check here could pass while the real
    // one fails.
    bridge.send(IPC.WORKSPACE_ADD_REQUEST, { path });
  },

  removeWorkspace(id) {
    const workspace = this.workspaces.find((w) => w.id === id);
    const staged = Number(workspace?.staged_count || 0);
    const warning = staged
      ? `\n\n${staged} staged change${staged === 1 ? "" : "s"} will be left on disk, not deleted.`
      : "";
    if (!window.confirm(`Stop tracking "${workspace?.name}"?${warning}`)) return;

    bridge.send(IPC.WORKSPACE_REMOVE_REQUEST, { id });
  },

  setPrimary(id) { bridge.send(IPC.WORKSPACE_PRIMARY_REQUEST, { id }); },

  // Commit, discard and rollback all carry the user's own words, because
  // that is what the backend checks for consent. Asking here rather than
  // sending a canned phrase is the difference between the user
  // authorising the change and this page authorising it for them.
  commit(id, files) {
    const user_text = window.prompt(
      "Type what you want to happen (for example: commit the changes):", "");
    if (user_text === null) return;
    bridge.send(IPC.WORKSPACE_COMMIT_REQUEST, { id, files, user_text });
  },

  discard(id, files) {
    const user_text = window.prompt(
      "Type what you want to happen (for example: discard the changes):", "");
    if (user_text === null) return;
    bridge.send(IPC.WORKSPACE_DISCARD_REQUEST, { id, files, user_text });
  },

  rollback(id, files) { bridge.send(IPC.WORKSPACE_ROLLBACK_REQUEST, { id, files }); },

  showError(message) {
    const banner = document.getElementById("ws-error");
    if (!banner) return;
    banner.textContent = message;
    banner.style.display = "block";
    setTimeout(() => { banner.style.display = "none"; }, 6000);
  },

  /* --------------------------------------------------------------
     Rendering
     -------------------------------------------------------------- */
  renderList() {
    const host = document.getElementById("ws-list");
    if (!host) return;

    const summary = document.getElementById("ws-summary");
    if (summary) {
      const staged = this.workspaces.reduce((n, w) => n + Number(w.staged_count || 0), 0);
      summary.textContent =
        `${this.workspaces.length} workspace${this.workspaces.length === 1 ? "" : "s"}`
        + ` · ${staged} staged change${staged === 1 ? "" : "s"}`;
    }

    host.innerHTML = this.workspaces.map((w) => {
      const staged = Number(w.staged_count || 0);
      return `
      <article class="ws-card ${w.id === this.selectedId ? "ws-selected" : ""}" data-id="${esc(w.id)}">
        <div class="ws-card-title">
          ${esc(w.name)}
          ${w.primary ? '<span class="ws-tag ws-tag-primary">Primary</span>' : ""}
          <span class="ws-tag ${w.tool_capable ? "ws-tag-ok" : "ws-tag-warn"}">
            Tool-capable: ${w.tool_capable ? "Yes" : "No"}</span>
        </div>
        <div class="ws-line"><span>Root</span><code title="${esc(w.root_path)}">${esc(w.root_path)}</code></div>
        <div class="ws-line"><span>Ghost</span><code title="${esc(w.ghost_directory_path)}">${esc(w.ghost_directory_path)}</code></div>
        <div class="ws-line"><span>Staged</span><span>${staged === 0 ? "nothing waiting" : `${staged} file${staged === 1 ? "" : "s"}`}</span></div>
        <div class="ws-line"><span>Last used</span><span>${esc(when(w.last_used))}</span></div>
        <div class="ws-card-actions">
          <button type="button" data-act="select" data-id="${esc(w.id)}">View Details</button>
          <button type="button" data-act="primary" data-id="${esc(w.id)}" ${w.primary ? "disabled" : ""}>Set Primary</button>
          <button type="button" data-act="remove" data-id="${esc(w.id)}">Remove</button>
        </div>
      </article>`;
    }).join("") || '<p class="ws-empty">No workspaces yet.</p>';

    host.querySelectorAll("button[data-act]").forEach((button) => {
      const id = button.getAttribute("data-id");
      const act = button.getAttribute("data-act");
      button.addEventListener("click", () => {
        if (act === "select") this.loadDetails(id);
        else if (act === "primary") this.setPrimary(id);
        else if (act === "remove") this.removeWorkspace(id);
      });
    });
  },

  renderDetails() {
    const host = document.getElementById("ws-details");
    if (!host) return;

    const d = this.details;
    if (!d) { host.innerHTML = ""; return; }

    const pending = d.pending_changes || [];
    const report = d.report;

    host.innerHTML = `
      <h2>${esc(d.name)}</h2>
      <div class="ws-line"><span>Project root</span><code>${esc(d.root_path)}</code></div>
      <div class="ws-line"><span>Ghost workspace</span><code>${esc(d.ghost_directory_path)}</code></div>
      <div class="ws-line"><span>Active model</span><span>${esc(d.tool_capable_model || "chosen per turn")}</span></div>
      ${d.capability_warning ? `<p class="ws-capability">${esc(d.capability_warning)}</p>` : ""}
      ${report ? `<p class="ws-report ws-report-${esc(report.status)}">${esc(describeReport(report))}</p>` : ""}

      <div class="ws-actions">
        <button type="button" data-all="commit" ${pending.length ? "" : "disabled"}>Commit All</button>
        <button type="button" data-all="discard" ${pending.length ? "" : "disabled"}>Discard All</button>
        <button type="button" data-all="rollback" ${pending.length ? "" : "disabled"}>Rollback All</button>
      </div>

      <h3>Pending changes</h3>
      ${pending.length === 0 ? '<p class="ws-empty">Nothing staged.</p>' : pending.map((p) => `
        <details class="ws-change">
          <summary>
            <code>${esc(p.path)}</code>
            <span class="ws-tag ws-tag-${esc(p.status)}">${esc(p.status)}</span>
            ${p.has_snapshot ? '<span class="ws-tag">has snapshot</span>' : ""}
          </summary>
          <pre class="ws-diff">${esc(p.diff) || "(no textual difference)"}</pre>
          <div class="ws-actions">
            <button type="button" data-file="${esc(p.path)}" data-act="commit">Commit</button>
            <button type="button" data-file="${esc(p.path)}" data-act="discard">Discard</button>
            <button type="button" data-file="${esc(p.path)}" data-act="rollback">Rollback</button>
          </div>
        </details>`).join("")}
    `;

    host.querySelectorAll("button[data-all]").forEach((button) => {
      const act = button.getAttribute("data-all");
      button.addEventListener("click", () => this[act](d.id, null));
    });
    host.querySelectorAll("button[data-file]").forEach((button) => {
      const file = button.getAttribute("data-file");
      const act = button.getAttribute("data-act");
      button.addEventListener("click", () => this[act](d.id, [file]));
    });
  },
};

function describeReport(report) {
  if (report.status === "refused") {
    return "Refused: nothing was written. A commit needs you to ask for it in "
         + "words, and a negation cancels it.";
  }
  if (report.status === "empty") return "Nothing to do.";
  const files = (report.files || []).length;
  return `${report.status}: ${files} file${files === 1 ? "" : "s"}.`;
}

function when(seconds) {
  if (!seconds) return "never";
  return new Date(Number(seconds) * 1000).toLocaleString();
}

function esc(value) {
  // Paths come off a filesystem and diffs are file contents. Neither is
  // trusted markup, and this page builds its DOM with innerHTML.
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

export default Workspaces;
