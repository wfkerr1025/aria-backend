// components/working_directory/working_directory.js
//
// The working-directory panel: which project ARIA is in, where it
// stages edits, and whether the model in front of you can be asked for
// one.
//
// The working directory was implicit until now -- backend/core/
// file_tools.py reads ARIA_TOOL_WORKSPACE or falls back to the process's
// cwd, and nothing displayed it. On a normal install that means the
// workspace is the ARIA-Lite source tree, which is a surprising thing to
// discover by watching a commit land in it.
//
// A view, and only a view. Every value here comes from one packet
// (workspace_status_result), and changing the directory sends one packet
// back (workspace_set_request) whose reply is that same shape -- so the
// panel never computes a path of its own, and never has to ask twice for
// the state its own change produced. A second opinion about where the
// project is would be a second thing to disagree with the boundary the
// file tools actually enforce.
//
// Fixed and page-global, like the model indicator beside it: staging is
// per-project, so "which project am I in" and "do I have changes
// waiting" are things a user needs while doing something else, not
// things to go and look up.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

const WorkingDirectory = {
  el: null,
  state: null,
  // Collapsed until asked. Persistent does not have to mean large, and
  // the first version was a fixed panel over the sidebar's New Chat
  // button -- a panel that hides working controls is worse than none.
  expanded: false,

  init() {
    this.el = document.getElementById("working-directory-panel");
    if (!this.el) return;

    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet) return;

      if (packet.type === IPC.WORKSPACE_STATUS_RESULT) {
        this.state = packet.payload || packet;
        this.render();
      } else if (packet.type === "error" && packet.payload?.request === IPC.WORKSPACE_SET_REQUEST) {
        // Shown rather than swallowed. The whole point of a visible
        // working directory is that it says where ARIA actually is, so
        // a rejected change must not leave the old path on screen
        // looking like it was accepted.
        this.showError(packet.payload.message || "That directory could not be used.");
      }
    });

    this.refresh();
  },

  refresh() {
    bridge.send(IPC.WORKSPACE_STATUS_REQUEST, {});
  },

  changeDirectory() {
    const current = this.state?.project_root || "";
    const next = window.prompt("Working directory:", current);
    if (next === null) return;          // cancelled, not cleared

    // Sent as typed. Validation belongs to the backend, which is the
    // side that knows what the file tools will accept -- a check here
    // would be a second opinion that could pass while the real one
    // fails.
    bridge.send(IPC.WORKSPACE_SET_REQUEST, { path: next });
  },

  viewGhostWorkspace() {
    const ghost = this.state?.ghost_root;
    if (!ghost) return;

    // Copied rather than opened: the UI has no privilege to open a
    // native file browser, and pretending to would be a button that
    // silently does nothing.
    navigator.clipboard?.writeText(ghost);
    this.flash(`Ghost workspace path copied:\n${ghost}`);
  },

  showError(message) {
    const banner = this.el.querySelector(".wd-error");
    if (!banner) return;
    banner.textContent = message;
    banner.style.display = "block";
  },

  flash(message) {
    const banner = this.el.querySelector(".wd-error");
    if (!banner) return;
    banner.textContent = message;
    banner.style.display = "block";
    setTimeout(() => { banner.style.display = "none"; }, 4000);
  },

  summary() {
    // The one line worth seeing without opening anything: which project,
    // how many changes are waiting, and whether this model can act.
    const s = this.state || {};
    // Both separators: these paths come from a Windows backend, so a
    // split on "/" alone returns the whole path as one segment and the
    // summary reads as the full directory instead of its name.
    const project = String(s.project_root || "").split(/[\\/]/).filter(Boolean).pop() || "—";
    const staged = Number(s.staged_count || 0);
    const bits = [project];
    if (staged) bits.push(`${staged} staged`);
    if (s.tool_capable === false) bits.push("not tool-capable");
    return bits.join(" · ");
  },

  render() {
    const s = this.state;
    if (!s || !this.el) return;

    const staged = Number(s.staged_count || 0);
    const capable = s.tool_capable !== false;

    this.el.className = this.expanded ? "" : "wd-collapsed";

    this.el.innerHTML = `
      <div class="wd-title" data-wd="toggle">
        <span>ARIA Working Directory</span>
        <span class="wd-summary">${escapeHtml(this.summary())}</span>
      </div>
      <div class="wd-row"><span class="wd-label">Project Root</span>
        <span class="wd-value" title="${escapeHtml(s.project_root || "")}">${escapeHtml(s.project_root || "—")}</span></div>
      <div class="wd-row"><span class="wd-label">Ghost Workspace</span>
        <span class="wd-value" title="${escapeHtml(s.ghost_root || "")}">${escapeHtml(s.ghost_root || "—")}</span></div>
      <div class="wd-row"><span class="wd-label">Active Model</span>
        <span class="wd-value">${escapeHtml(s.active_model || "chosen per turn")}
          <span class="wd-badge ${capable ? "wd-ok" : "wd-warn"}">Tool-capable: ${capable ? "Yes" : "No"}</span></span></div>
      <div class="wd-row"><span class="wd-label">Staged changes</span>
        <span class="wd-value">${staged === 0 ? "none" : `${staged} file${staged === 1 ? "" : "s"} waiting to commit`}</span></div>
      ${s.capability_warning ? `<div class="wd-capability">${escapeHtml(s.capability_warning)}</div>` : ""}
      <div class="wd-error" style="display:none"></div>
      <div class="wd-actions">
        <button type="button" data-wd="change">Change Directory</button>
        <button type="button" data-wd="refresh">Refresh</button>
        <button type="button" data-wd="ghost">View Ghost Workspace</button>
      </div>
    `;

    this.el.querySelector('[data-wd="toggle"]')
      ?.addEventListener("click", () => {
        this.expanded = !this.expanded;
        this.render();
      });
    this.el.querySelector('[data-wd="change"]')
      ?.addEventListener("click", () => this.changeDirectory());
    this.el.querySelector('[data-wd="refresh"]')
      ?.addEventListener("click", () => this.refresh());
    this.el.querySelector('[data-wd="ghost"]')
      ?.addEventListener("click", () => this.viewGhostWorkspace());
  },
};

function escapeHtml(value) {
  // Paths come from the filesystem and the capability warning names a
  // model id. Neither is trusted markup, and this panel builds its DOM
  // with innerHTML.
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

export default WorkingDirectory;
