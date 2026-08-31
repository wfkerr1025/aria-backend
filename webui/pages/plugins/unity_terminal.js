// pages/plugins/unity_terminal.js
//
// A terminal for Unity CLI output.
//
// Opens when a command is Run, fills with stdout and stderr as they
// arrive, and shows the result when the command finishes.
//
// WHY IT IS NOT THE CHAT
// ----------------------
// ARIA already has a place where progress lines appear: the chat. This
// output does not go there. A build prints hundreds of lines, and in
// the chat those would land in the transcript, in the history the next
// turn reads, and in the text actions are parsed from. So the backend
// sends unity_cli_output packets and they end up here instead.
//
// SCROLLBACK
// ----------
// Bounded. A Unity build can print more lines than a browser wants to
// hold as DOM nodes, and the interesting ones are at the end. Older
// lines are dropped from the top, and the viewer says so rather than
// quietly showing a partial log as if it were whole.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";

// Enough to read a failure back in full; small enough that a runaway
// command cannot take the page down with it.
const MAX_LINES = 2000;

const UnityTerminal = {
  bound: false,
  commandId: null,
  lines: 0,
  dropped: 0,

  /** Cache the elements once; every method reads from here. */
  parts() {
    return {
      root: document.getElementById("unity-terminal"),
      title: document.getElementById("unity-terminal-title"),
      status: document.getElementById("unity-terminal-status"),
      body: document.getElementById("unity-terminal-body"),
      json: document.getElementById("unity-terminal-json"),
      note: document.getElementById("unity-terminal-note"),
    };
  },

  bind() {
    if (this.bound) return;
    window.addEventListener("backend-packet", (event) => this.onPacket(event.detail));
    this.bound = true;
  },

  /** Rebound on every page visit: the markup is replaced each time. */
  bindControls() {
    document.getElementById("unity-terminal-close")
      ?.addEventListener("click", () => this.close());
    document.getElementById("unity-terminal-clear")
      ?.addEventListener("click", () => this.clear());
  },

  /**
   * Show the terminal for one command and send it.
   *
   * Opening and running are one action on purpose. A terminal that
   * opened empty and waited would leave the user wondering whether
   * they had pressed the button.
   */
  run(command, args = []) {
    this.bind();
    this.commandId = command.id;

    const { root, title } = this.parts();
    if (!root) return;

    this.clear();
    if (title) title.textContent = command.name || command.id;
    this.setStatus("running", "Running…");

    root.hidden = false;
    bridge.send(IPC.UNITY_CLI_COMMAND_REQUEST, { id: command.id, args });
  },

  onPacket(packet) {
    if (!packet) return;
    const payload = packet.payload || packet;

    // Output from a command this viewer is not showing belongs to an
    // earlier run; dropping it stops two runs interleaving.
    if (packet.type === IPC.UNITY_CLI_OUTPUT) {
      if (payload.id !== this.commandId) return;
      this.write(payload.line || "", payload.stream || "stdout");
      return;
    }

    if (packet.type === IPC.UNITY_CLI_COMMAND_RESULT) {
      if (payload.id !== this.commandId) return;
      this.finish(payload);
    }
  },

  write(text, stream) {
    const { body } = this.parts();
    if (!body) return;

    const line = document.createElement("div");
    line.className = "unity-terminal-line"
      + (stream === "stderr" ? " is-stderr" : "");
    // textContent, not innerHTML: this is output from a program, and a
    // build log containing markup is a log, not markup.
    line.textContent = text;
    body.appendChild(line);
    this.lines += 1;

    while (this.lines > MAX_LINES && body.firstChild) {
      body.removeChild(body.firstChild);
      this.lines -= 1;
      this.dropped += 1;
    }
    if (this.dropped) this.setNote(`${this.dropped} earlier line(s) not shown.`);

    // Follow the tail, which is where a running command is interesting.
    body.scrollTop = body.scrollHeight;
  },

  finish(payload) {
    if (payload.error && !payload.output) this.write(payload.error, "stderr");

    if (payload.success) {
      this.setStatus("ok", "Finished");
    } else {
      const code = payload.code;
      this.setStatus("failed",
                     code == null ? "Failed" : `Failed (exit ${code})`);
      if (payload.error) this.setNote(payload.error);
    }

    const { json } = this.parts();
    if (!json) return;

    // Pretty-printed, and only when there actually was JSON. An empty
    // panel labelled "JSON" for a command that printed plain text
    // would suggest something was lost.
    if (payload.json === null || payload.json === undefined) {
      json.hidden = true;
      json.textContent = "";
      return;
    }
    json.hidden = false;
    json.textContent = JSON.stringify(payload.json, null, 2);
  },

  setStatus(state, text) {
    const { status } = this.parts();
    if (!status) return;
    status.textContent = text;
    status.className = `unity-terminal-status is-${state}`;
  },

  setNote(text) {
    const { note } = this.parts();
    if (!note) return;
    note.textContent = text || "";
    note.hidden = !text;
  },

  clear() {
    const { body, json } = this.parts();
    if (body) body.innerHTML = "";
    if (json) { json.hidden = true; json.textContent = ""; }
    this.lines = 0;
    this.dropped = 0;
    this.setNote("");
  },

  close() {
    const { root } = this.parts();
    if (root) root.hidden = true;
    // The id is kept, so late output from a command still running is
    // still recognised rather than being written into the next run.
  },
};

export default UnityTerminal;
