// pages/unity_commands/unity_commands.js
//
// The Unity Editor's tools, as things you can run.
//
// WHY THIS IS NOT ON THE PLUGINS PAGE
// -----------------------------------
// It was, as a section. The Plugins hub lists six integrations, and a
// connected Editor reports 142 commands -- so the section was twenty
// times the size of the page it lived on, and the six things the page
// exists for were pushed off the top of it.
//
// It is also not on the Unity CLI config page. That page's job is
// settings: where the CLI is, which project, which mode. Running things
// is a different job, and a settings form you scroll past 142 tiles to
// reach is a settings form nobody edits.
//
// So: its own page, reached from the Unity CLI plugin. At 142 items it
// needs a filter, and a filter needs room.

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import UnityTerminal from "./unity_terminal.js";

function commandLog(message) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("UnityCommands", message);
    }
  } catch (error) {
    console.error("[UnityCommands LOG ERROR]", error);
  }
}

const UnityCommands = {
  bound: false,
  commands: [],
  available: false,

  init() {
    this.bind();
    // Lists what is registered. Asking the Editor what it has means
    // running the CLI, and that happens when Refresh is pressed.
    bridge.send(IPC.UNITY_CLI_COMMANDS_REQUEST, {});
  },

  bind() {
    // Attached once for the life of the module. Router.navigate()
    // re-imports a cached module, so init() runs again on the SAME
    // object -- without this the listener accumulates and one packet
    // renders the page three times.
    if (!this.bound) {
      window.addEventListener("backend-packet", (event) => this.onPacket(event.detail));
      this.bound = true;
    }

    // Rebound every visit: navigate() replaces the markup, so the
    // elements listened to last time are not the ones on screen now.
    document.getElementById("unity-commands-back")
      ?.addEventListener("click", () => {
        window.dispatchEvent(new CustomEvent("navigatePanel",
                                             { detail: "plugins/unity-cli-config" }));
      });

    document.getElementById("unity-commands-refresh")
      ?.addEventListener("click", () => {
        this.say("Asking the Unity Editor what it can do…");
        bridge.send(IPC.UNITY_CLI_REFRESH_REQUEST, { force: true });
      });

    document.getElementById("unity-commands-search")
      ?.addEventListener("input", () => this.render());

    document.getElementById("unity-commands-enabled-only")
      ?.addEventListener("change", () => this.render());

    UnityTerminal.bind();
    UnityTerminal.bindControls();
  },

  onPacket(packet) {
    if (!packet) return;
    const payload = packet.payload || packet;

    if (packet.type === IPC.UNITY_CLI_COMMANDS_RESULT) {
      this.commands = payload.commands || [];
      this.available = Boolean(payload.available);

      if (payload.error) this.say(payload.error, true);
      else if ((payload.added || []).length) {
        this.say(`Found ${payload.added.length} new command(s).`);
      } else {
        this.say("");
      }
      this.render();
      return;
    }

    // Enabling or removing a command changes this list, so ask again
    // rather than guessing what the change did.
    if (packet.type === IPC.PLUGIN_UPDATE_RESULT
        || packet.type === IPC.PLUGIN_REMOVE_RESULT) {
      bridge.send(IPC.UNITY_CLI_COMMANDS_REQUEST, {});
      return;
    }

    if (packet.type === "error" && String(payload.request || "").startsWith("plugin_")) {
      this.say(payload.message || "That change was refused.", true);
    }
  },

  /** What the filter box and the checkbox leave. */
  visible() {
    const needle = String(
      document.getElementById("unity-commands-search")?.value || "")
      .trim().toLowerCase();
    const enabledOnly =
      document.getElementById("unity-commands-enabled-only")?.checked === true;

    return this.commands.filter((command) => {
      if (enabledOnly && command.enabled !== true) return false;
      if (!needle) return true;
      const haystack = `${command.name || ""} ${command.label || ""}`.toLowerCase();
      return haystack.includes(needle);
    });
  },

  render() {
    const grid = document.getElementById("unity-commands-grid");
    const empty = document.getElementById("unity-commands-empty");
    const count = document.getElementById("unity-commands-count");
    if (!grid) return;

    const shown = this.visible();

    if (count) {
      count.textContent = this.commands.length
        ? (shown.length === this.commands.length
           ? `${this.commands.length} command(s) from the connected Editor.`
           : `${shown.length} of ${this.commands.length} command(s).`)
        : "";
    }

    grid.innerHTML = "";
    shown.forEach((command) => grid.appendChild(this.tile(command)));

    if (empty) {
      // Three different empties, and they need three different
      // sentences: no CLI, a CLI with nothing registered, and a filter
      // that matched nothing. "No commands" for all three would be
      // wrong twice.
      let message = "";
      if (!this.available) {
        message = "The Unity CLI plugin is not enabled. Turn it on from "
                + "its page under Plugins.";
      } else if (!this.commands.length) {
        message = "No commands registered yet. Open your Unity project in "
                + "the Editor, then press Refresh Commands.";
      } else if (!shown.length) {
        message = "No command matches that filter.";
      }
      empty.textContent = message;
      empty.hidden = !message;
    }

    commandLog(`rendered ${shown.length}/${this.commands.length} command tile(s)`);
  },

  tile(command) {
    const enabled = command.enabled === true;
    const awaiting = command.discovered === true && !enabled;

    // A <div>, not a <button>: this tile holds three controls, and a
    // button containing buttons is invalid HTML the browser repairs by
    // pulling them out of it.
    const tile = document.createElement("div");
    tile.className = "settings-tile plugin-tile unity-command-tile"
      + (enabled ? "" : " is-disabled")
      + (awaiting ? " is-discovered" : "");
    tile.dataset.commandId = command.id || "";

    if (awaiting) {
      const badge = document.createElement("span");
      badge.className = "plugin-tile-badge";
      badge.textContent = "Discovered";
      tile.appendChild(badge);
    }

    const title = document.createElement("span");
    title.className = "settings-tile-title";
    title.textContent = command.name || command.id || "Unnamed command";
    tile.appendChild(title);

    const label = document.createElement("span");
    label.className = "settings-tile-subtitle";
    label.textContent = command.label || "";
    tile.appendChild(label);

    const status = document.createElement("span");
    status.className = "plugin-tile-status" + (enabled ? " is-enabled" : "");
    status.textContent = enabled ? "Enabled" : "Disabled";
    tile.appendChild(status);

    const actions = document.createElement("div");
    actions.className = "unity-command-actions";

    if (!enabled) {
      actions.appendChild(this.button("Enable", () => {
        bridge.send(IPC.PLUGIN_UPDATE_REQUEST,
                    { id: command.id, fields: { enabled: true } });
      }));
    } else {
      // Run only appears once a command is on. Discovery lists what an
      // Editor could do; being listed is not permission to run it, and
      // the backend refuses a disabled command anyway.
      actions.appendChild(this.button("Run", () => {
        UnityTerminal.run(command, []);
      }, "is-primary"));
    }

    actions.appendChild(this.button("Edit", () => this.edit(command)));
    actions.appendChild(this.button("Remove", () => {
      if (!window.confirm(`Remove ${command.name || command.id}?\n\n`
                          + `Refresh Commands will offer it again.`)) return;
      bridge.send(IPC.PLUGIN_REMOVE_REQUEST, { id: command.id });
    }, "is-danger"));

    tile.appendChild(actions);
    return tile;
  },

  button(text, onClick, extra = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "unity-command-btn" + (extra ? ` ${extra}` : "");
    button.textContent = text;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      onClick();
    });
    return button;
  },

  /**
   * Edit one command's template and arguments.
   *
   * A prompt() pair rather than a modal. It is the smallest thing that
   * actually works, and the backend validates both -- args become
   * separate argv entries, so splitting on spaces here cannot produce
   * a shell injection, only a wrong argument.
   */
  edit(command) {
    const template = window.prompt(
      `Command template for ${command.name}:`, command.command || "");
    if (template === null) return;

    const args = window.prompt(
      "Default arguments, separated by spaces:",
      (command.args || []).join(" "));
    if (args === null) return;

    bridge.send(IPC.PLUGIN_UPDATE_REQUEST, {
      id: command.id,
      fields: {
        command: template.trim(),
        args: args.split(/\s+/).filter(Boolean),
      },
    });
  },

  say(message, isError = false) {
    const banner = document.getElementById("unity-commands-message");
    if (!banner) return;
    banner.textContent = message;
    banner.hidden = !message;
    banner.classList.toggle("is-error", Boolean(isError));
  },
};

export default UnityCommands;
