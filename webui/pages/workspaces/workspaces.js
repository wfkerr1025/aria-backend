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
  // A request is in flight. Drives the Refresh button's spinner, and
  // stops a second click queueing a second answer to the same question.
  busy: false,
  // Set when the user asked for a refresh, cleared when the cards flash.
  // Without it every arriving packet would flash the list, including the
  // one that renders the page, and a highlight that fires constantly
  // stops meaning "this just changed".
  flashPending: false,
  bound: false,

  init() {
    this.bind();
    // Router.navigate() re-imports this module on every visit, but an ES
    // module is cached: init() runs again on the SAME object. Without
    // this guard the window listener accumulates, and after three visits
    // one packet renders the page three times.
    if (!this.bound) {
      window.addEventListener("backend-packet", (evt) => this.onPacket(evt.detail));
      this.bound = true;
    }
    this.refresh();
  },

  bind() {
    // The elements are new on every navigation (the router replaces
    // panel-container's innerHTML), so these are re-attached each time --
    // unlike the window listener above, which is not.
    document.getElementById("ws-add")
      ?.addEventListener("click", () => this.addWorkspace());
    document.getElementById("ws-refresh")
      ?.addEventListener("click", () => { this.flashPending = true; this.refresh(); });
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
      this.markRefreshed();
      this.flashCards();
      if (this.selectedId && !this.details) this.loadDetails(this.selectedId);
    } else if (packet.type === IPC.WORKSPACE_DETAILS_RESULT) {
      this.details = payload;
      this.selectedId = payload.id;
      this.renderDetails();
      this.renderList();
      this.markRefreshed();
    } else if (packet.type === "error" && String(payload.request || "").startsWith("workspace_")) {
      // Clears the spinner too. A refused operation that left the button
      // spinning would read as "still working" forever.
      this.setBusy(false);
      this.showError(payload.message || "That workspace operation was refused.");
    }
  },

  refresh() {
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_LIST_REQUEST, {});
  },

  loadDetails(id) {
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_DETAILS_REQUEST, { id });
  },

  async addWorkspace() {
    const path = await dialog.text({
      title: "Add Workspace",
      message: "ARIA will stage its edits inside this directory and write "
             + "into it only when you commit them.",
      label: "Project directory",
      placeholder: "D:\\projects\\my-app",
    });
    if (path === null) return;      // cancelled, not cleared
    // Sent as typed. Validation belongs to the backend, which knows what
    // the file tools will accept; a check here could pass while the real
    // one fails.
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_ADD_REQUEST, { path });
  },

  async removeWorkspace(id) {
    const workspace = this.workspaces.find((w) => w.id === id);
    const staged = Number(workspace?.staged_count || 0);
    // Says what survives it. Removing is bookkeeping -- ARIA stops
    // tracking the project -- and never a delete, so a user who reads
    // "Remove" as "throw the staged work away" should be corrected before
    // they decide, not after.
    const consequence = staged
      ? `Its ${staged} staged change${staged === 1 ? "" : "s"} will be left on disk, not deleted.`
      : "Nothing is staged in it.";

    const confirmed = await dialog.confirm({
      title: `Stop tracking "${workspace?.name}"?`,
      message: consequence,
      confirmLabel: "Remove",
    });
    if (!confirmed) return;

    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_REMOVE_REQUEST, { id });
  },

  setPrimary(id) {
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_PRIMARY_REQUEST, { id });
  },

  // Commit and discard carry the user's own words, because that is what
  // the backend checks for consent. Asking here rather than sending a
  // canned phrase is the difference between the user authorising the
  // change and this page authorising it for them -- so the field starts
  // EMPTY. A pre-filled "commit the changes" would be this page writing
  // the consent and the user pressing OK.
  async commit(id, files) {
    // The second consent, and the reason it is worth having: the first
    // one was a sentence in a chat, possibly several turns ago. This one
    // lists what will actually happen, now, to this project.
    const plan = this.planSummary();
    const user_text = await dialog.text({
      title: files ? "Commit this file" : "Commit all staged changes",
      message: "This writes the staged version into the project"
             + (plan ? " and runs:\n" + plan : "")
             + "\n\nSay what you want to happen, in your own words.",
      label: "For example: commit the changes",
    });
    if (user_text === null) return;
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_COMMIT_REQUEST, { id, files, user_text });
  },

  async discard(id, files) {
    const user_text = await dialog.text({
      title: files ? "Discard this file" : "Discard all staged changes",
      message: "This throws the staged version away. The project is not touched.",
      label: "For example: discard the changes",
    });
    if (user_text === null) return;
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_DISCARD_REQUEST, { id, files, user_text });
  },

  rollback(id, files) {
    this.setBusy(true);
    bridge.send(IPC.WORKSPACE_ROLLBACK_REQUEST, { id, files });
  },

  planSummary() {
    const operations = this.details?.pending_operations || [];
    if (!operations.length) return "";
    return operations.map((op) => `  - ${op.summary}`).join("\n");
  },

  showError(message) {
    const banner = document.getElementById("ws-error");
    if (!banner) return;
    banner.textContent = message;
    banner.style.display = "block";
    setTimeout(() => { banner.style.display = "none"; }, 6000);
  },

  /* --------------------------------------------------------------
     Saying that something happened

     Refresh sends a packet and always did; what it never did was look
     like it. When the state comes back identical -- which is the normal
     case -- nothing on the page changed, so a working button and a dead
     one were indistinguishable. All three of these exist to tell those
     apart: the spinner while the request is out, the timestamp when the
     answer lands, and the flash on the cards it rendered.
     -------------------------------------------------------------- */
  setBusy(busy) {
    this.busy = busy;
    const button = document.getElementById("ws-refresh");
    if (!button) return;
    button.classList.toggle("is-busy", busy);
    // Shown, never disabled.
    //
    // The first version disabled it, and that was a trap found by
    // clicking it: a list result clears the flag and then immediately
    // arms it again for the details request behind it, so any request
    // that never gets an answer leaves Refresh permanently dead -- and
    // Refresh is the one control that would have recovered the page.
    //
    // A refresh is an idempotent read. There is nothing to protect
    // against by blocking a second one, and everything to lose by making
    // the escape hatch the thing that jams.
  },

  markRefreshed() {
    this.setBusy(false);
    const stamp = document.getElementById("ws-refreshed");
    if (stamp) stamp.textContent = `Refreshed at ${new Date().toLocaleTimeString()}`;
  },

  flashCards() {
    if (!this.flashPending) return;
    this.flashPending = false;
    document.querySelectorAll("#ws-list .ws-card").forEach((card) => {
      card.classList.add("ws-flash");

      // animationend, with a timer behind it. The event is the accurate
      // signal and the timer is the one that always arrives: under
      // prefers-reduced-motion the animation is `none` and animationend
      // never fires at all, and in a hidden tab animations do not
      // advance -- which is how this was found. Either way the class
      // would stick, and a stale .ws-flash means every card flashes at
      // once the moment animations resume.
      const clear = () => card.classList.remove("ws-flash");
      card.addEventListener("animationend", clear, { once: true });
      setTimeout(clear, 1000);
    });
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
    const operations = d.pending_operations || [];
    // A plan that only deletes stages no content, so gating the buttons
    // on pending_changes alone left Commit greyed out with a deletion
    // waiting -- the user asked twice and the second ask was disabled.
    const anything = pending.length + operations.length > 0;
    const report = d.report;

    host.innerHTML = `
      <h2>${esc(d.name)}</h2>
      <div class="ws-line"><span>Project root</span><code>${esc(d.root_path)}</code></div>
      <div class="ws-line"><span>Ghost workspace</span><code>${esc(d.ghost_directory_path)}</code></div>
      <div class="ws-line"><span>Active model</span><span>${esc(d.tool_capable_model || "chosen per turn")}</span></div>
      ${d.capability_warning ? `<p class="ws-capability">${esc(d.capability_warning)}</p>` : ""}
      ${report ? `<p class="ws-report ws-report-${esc(report.status)}">${esc(describeReport(report))}</p>` : ""}

      <div class="ws-actions">
        <button type="button" data-all="commit" ${anything ? "" : "disabled"}>Commit All</button>
        <button type="button" data-all="discard" ${anything ? "" : "disabled"}>Discard All</button>
        <button type="button" data-all="rollback" ${pending.length ? "" : "disabled"}>Rollback All</button>
      </div>

      ${renderOperations(d.pending_operations || [])}

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

/* ----------------------------------------------------------------
   The dialog

   window.prompt() throws in an Electron renderer -- it is not
   implemented and will not be. Every button on this page that asked a
   question through it therefore threw inside its own click handler,
   sent no packet, and looked from the outside like a button that was
   never wired up. That is the whole of the "Add Workspace does
   nothing" bug.

   Promise-based so the callers read the way they did before: `const
   path = await dialog.text(...)`, null for cancelled. The distinction
   between null and "" is kept deliberately -- cancelling and clearing
   are different answers, and only one of them should send a packet.
   ---------------------------------------------------------------- */
const dialog = {
  open({ title, message, label, placeholder, confirmLabel, withInput }) {
    const backdrop = document.getElementById("ws-dialog");
    const input = document.getElementById("ws-dialog-input");
    const labelEl = document.getElementById("ws-dialog-label");
    const ok = document.getElementById("ws-dialog-ok");
    const cancel = document.getElementById("ws-dialog-cancel");
    const titleEl = document.getElementById("ws-dialog-title");
    const messageEl = document.getElementById("ws-dialog-message");

    // No dialog in the DOM means the page was replaced mid-flight.
    // Resolving as cancelled is the safe direction: it sends nothing.
    if (!backdrop || !input || !ok || !cancel) return Promise.resolve(null);

    titleEl.textContent = title || "";
    messageEl.textContent = message || "";
    messageEl.hidden = !message;
    labelEl.textContent = label || "";
    labelEl.hidden = !withInput;
    input.hidden = !withInput;
    input.value = "";
    input.placeholder = placeholder || "";
    ok.textContent = confirmLabel || "OK";
    backdrop.hidden = false;

    if (withInput) input.focus();
    else ok.focus();

    return new Promise((resolve) => {
      const finish = (value) => {
        backdrop.hidden = true;
        // Removed by hand rather than left to the elements being
        // replaced: this dialog is reused, so a listener left behind
        // would resolve the NEXT dialog's promise as well.
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        backdrop.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKey);
        resolve(value);
      };
      const onOk = () => finish(withInput ? input.value : true);
      const onCancel = () => finish(withInput ? null : false);
      const onBackdrop = (event) => { if (event.target === backdrop) onCancel(); };
      const onKey = (event) => {
        if (event.key === "Escape") onCancel();
        // Enter confirms from the field only. On a confirmation there is
        // no field, and a stray Enter accepting a destructive default is
        // exactly the accident this dialog should not enable.
        else if (event.key === "Enter" && withInput && document.activeElement === input) onOk();
      };

      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      backdrop.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKey);
    });
  },

  text(options) { return this.open({ ...options, withInput: true }); },
  confirm(options) { return this.open({ ...options, withInput: false }); },
};

function renderOperations(operations) {
  if (!operations.length) return "";
  // Destructive first. They are what the user is actually deciding
  // about, and a delete listed under three folder creations is a delete
  // that gets skimmed past.
  const ordered = [...operations].sort((a, b) => (b.destructive === true) - (a.destructive === true));
  return `
    <h3>Staged operations</h3>
    <p class="ws-op-note">These change the shape of the project. Nothing runs until you commit.</p>
    <ul class="ws-ops">
      ${ordered.map((op) => `
        <li class="ws-op ${op.destructive ? "ws-op-destructive" : ""}">
          <span class="ws-tag ${op.destructive ? "ws-tag-warn" : "ws-tag-ok"}">${esc(op.op)}</span>
          <span>${esc(op.summary)}</span>
        </li>`).join("")}
    </ul>`;
}

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
