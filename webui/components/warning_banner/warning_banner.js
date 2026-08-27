// components/warning_banner/warning_banner.js
//
// Unified warning banner (spec: "WARNING SYSTEM SPECIFICATION") — a
// persistent, page-global overlay bootstrapped once at app startup
// (same pattern as webui/components/statusbar/statusbar.js: not part
// of any router-loaded panel's own HTML, so it survives navigation and
// is never destroyed/recreated), shown only while the Chat panel is
// the active one so it reads as "top of the chat window" without
// needing its own DOM lifecycle tied to chat.html's router remounts.
//
// Consumes backend/core/warning_manager.py's `warning_event` packets
// (flat, pushed unsolicited — see backend/ipc_schema.py's own comment
// on why this type is a flat exception, same as stream_start/
// safety_warning). The backend already handles the once-per-session
// dedup for "normal" warnings and the always-refire rule for
// "critical" ones (see that module's docstring) — this component's
// only job is to render whatever it's told and let the user dismiss a
// still-visible one, re-showing it only when a NEW event for that same
// id arrives (which for a critical warning can happen again this same
// session; for a normal one, the backend will simply never send it a
// second time).

function bannerLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("WarningBanner", msg);
    }
  } catch (err) {
    console.error("[WarningBanner LOG ERROR]", err);
  }
}

const WarningBanner = {
  // id -> { level, message, model_id, dismissed }
  _banners: new Map(),

  init() {
    this.container = document.getElementById("aria-warning-banner-container");
    if (!this.container) {
      bannerLog("ERROR: #aria-warning-banner-container not found.");
      return;
    }

    window.addEventListener("backend-packet", (evt) => {
      const packet = evt.detail;
      if (!packet || packet.type !== "warning_event") return;
      this._handleEvent(packet);
    });

    // Banner visibility depends on which panel is active (chat only) —
    // re-render on every navigation, not just on a new event.
    window.addEventListener("navigatePanel", () => this._render());

    this._render();
    bannerLog("WarningBanner ready.");
  },

  _activeModelName(fallbackId) {
    // Reuses Statusbar's own already-resolved Active Model name rather
    // than re-deriving it from a second IPC round trip — see
    // components/statusbar/statusbar.js's getActiveModelDisplayName().
    const name = window.Statusbar?.getActiveModelDisplayName?.();
    return name || fallbackId || "the active model";
  },

  _handleEvent(event) {
    const existing = this._banners.get(event.id);
    this._banners.set(event.id, {
      level: event.level,
      message: event.message,
      model_id: event.model_id,
      // A critical event always un-dismisses itself so it reappears
      // every time it's re-triggered; a normal one keeps whatever
      // dismissed state it already had (the backend will never send
      // that same id again this connection anyway).
      dismissed: event.level === "critical" ? false : (existing ? existing.dismissed : false),
    });
    bannerLog("warning_event received: " + JSON.stringify(event));
    this._render();
  },

  _dismiss(id) {
    const entry = this._banners.get(id);
    if (!entry) return;
    entry.dismissed = true;
    bannerLog("Dismissed warning: " + id);
    this._render();
  },

  _render() {
    if (!this.container) return;

    const onChat = window.Router?.currentPanel === "chat";
    this.container.innerHTML = "";
    if (!onChat) return;

    for (const [id, entry] of this._banners) {
      if (entry.dismissed) continue;

      const el = document.createElement("div");
      el.className = `aria-warning-banner level-${entry.level}`;

      const msg = document.createElement("span");
      msg.className = "aria-warning-banner-message";
      msg.textContent = `${entry.message} (Active Model: ${this._activeModelName(entry.model_id)})`;
      el.appendChild(msg);

      const dismissBtn = document.createElement("button");
      dismissBtn.className = "aria-warning-banner-dismiss";
      dismissBtn.type = "button";
      dismissBtn.textContent = "✕";
      dismissBtn.title = "Dismiss";
      dismissBtn.addEventListener("click", () => this._dismiss(id));
      el.appendChild(dismissBtn);

      this.container.appendChild(el);
    }
  },
};

export default WarningBanner;
bannerLog("WarningBanner module loaded.");
