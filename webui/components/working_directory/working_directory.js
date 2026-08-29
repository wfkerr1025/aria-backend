// components/working_directory/working_directory.js
//
// One line: which project ARIA is in, how many changes are waiting, and
// whether the model in front of you can be asked to make one. Clicking
// it opens the Control Center.
//
// This began as an expanding overlay carrying every field, because it
// was the only place any of it lived. The Control Center
// (pages/workspaces) now owns all of it -- the roots, the staged files,
// the diffs, commit, discard and rollback -- and a floating panel
// repeating that would be a second copy to keep in step, sitting in
// front of the transcript. Two places showing the same staged count is
// one place too many, and the one that goes stale is the one nobody is
// looking at.
//
// What survives is the part that has to be answerable without going to
// look for it. Everything it shows still comes from one packet; it
// computes no paths of its own, because a path derived here could
// disagree with the boundary the file tools actually enforce.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

const WorkingDirectory = {
  el: null,
  state: null,

  init() {
    this.el = document.getElementById("working-directory-panel");
    if (!this.el) return;

    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (packet?.type === IPC.WORKSPACE_STATUS_RESULT) {
        this.state = packet.payload || packet;
        this.render();
      }
    });

    this.refresh();
  },

  refresh() {
    bridge.send(IPC.WORKSPACE_STATUS_REQUEST, {});
  },

  openControlCenter() {
    // The router, not an href: the page is a routed panel, and a link
    // would reload the app out from under the websocket.
    //
    // navigate(), not load(). The first version called Router.load,
    // which does not exist -- the click did nothing, and the test that
    // was meant to catch that asserted the same wrong name, so it
    // agreed with the bug instead of finding it.
    if (window.Router?.navigate) window.Router.navigate("settings/workspaces");
  },

  summary() {
    const s = this.state || {};
    // Three counts, always all three, in a fixed order. The previous
    // version dropped "0 staged" and dropped the capability note when it
    // was fine, which read as tidier and was worse: a line whose fields
    // come and go has to be re-read to be understood, and the reason to
    // glance at it is to confirm nothing changed.
    //
    // workspace_count comes from the packet rather than being counted
    // here -- the Control Center holds the list and is usually closed.
    const workspaces = Number(s.workspace_count || 0);
    const staged = Number(s.staged_count || 0);
    const capable = s.tool_capable === false ? "No" : "Yes";
    return `Workspaces: ${workspaces} · Staged: ${staged} · Tool-capable: ${capable}`;
  },

  projectName() {
    // Both separators: these paths come from a Windows backend, so
    // splitting on "/" alone returns the whole path as one segment and
    // the line reads as the full directory instead of its name.
    const root = String(this.state?.project_root || "");
    return root.split(/[\\/]/).filter(Boolean).pop() || "—";
  },

  render() {
    if (!this.state || !this.el) return;

    this.el.className = "wd-collapsed";
    this.el.title = "Open the ARIA Control Center";
    this.el.innerHTML = `
      <div class="wd-title" data-wd="open" role="button" tabindex="0">
        <span>${escapeHtml(this.projectName())}</span>
        <span class="wd-summary">${escapeHtml(this.summary())}</span>
      </div>`;

    const opener = this.el.querySelector('[data-wd="open"]');
    opener?.addEventListener("click", () => this.openControlCenter());
    // Reachable without a mouse: it is a button in everything but tag.
    opener?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        this.openControlCenter();
      }
    });
  },
};

function escapeHtml(value) {
  // The project name comes off a filesystem, and this builds its DOM
  // with innerHTML.
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

export default WorkingDirectory;
