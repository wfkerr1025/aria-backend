// components/topbar/topbar.js
// Unified ARIA Lite Topbar — status updates + actions

export const Topbar = {
  init() {
    console.log("[Topbar] Initialized");

    this.statusEl = document.getElementById("system-status");
    this.bindActions();

    // Initial status
    this.setStatus("ok", "System OK");
  },

  /* -----------------------------------------------------------
     STATUS MANAGEMENT
     ----------------------------------------------------------- */
  setStatus(level, text) {
    if (!this.statusEl) return;

    this.statusEl.textContent = text;

    this.statusEl.classList.remove("status-ok", "status-warn", "status-error");

    switch (level) {
      case "ok":
        this.statusEl.classList.add("status-ok");
        break;
      case "warn":
        this.statusEl.classList.add("status-warn");
        break;
      case "error":
        this.statusEl.classList.add("status-error");
        break;
      default:
        this.statusEl.classList.add("status-warn");
        break;
    }

    console.log(`[Topbar] Status → ${level.toUpperCase()}: ${text}`);
  },

  /* -----------------------------------------------------------
     ACTION BUTTONS
     ----------------------------------------------------------- */
  bindActions() {
    const refreshBtn = document.getElementById("topbar-refresh-btn");
    const settingsBtn = document.getElementById("topbar-settings-btn");

    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        console.log("[Topbar] Refresh clicked");
        window.dispatchEvent(new Event("aria:refresh"));
      });
    }

    if (settingsBtn) {
      settingsBtn.addEventListener("click", () => {
        console.log("[Topbar] Settings clicked");
        window.dispatchEvent(new Event("aria:openSettings"));
      });
    }
  }
};

export default Topbar;
