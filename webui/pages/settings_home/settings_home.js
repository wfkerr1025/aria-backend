// pages/settings_home/settings_home.js
//
// Steam-library-style Settings hub: four tiles, each navigating to its
// own dedicated subpage. Navigation reuses the exact same event-based
// mechanism every sidebar button already uses (webui/components/sidebar/
// sidebar.js dispatches "navigatePanel"; webui/core/router.js listens
// for it) — a tile is just another way to trigger the same panel switch,
// not a parallel navigation system.

function settingsHomeLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("SettingsHome", msg);
    }
  } catch (err) {
    console.error("[SettingsHome LOG ERROR]", err);
  }
}

settingsHomeLog("=== SETTINGS HOME MODULE LOADED ===");

const SettingsHome = {
  init() {
    settingsHomeLog("SettingsHome.init() called.");

    const tiles = document.querySelectorAll(".settings-tile");
    if (!tiles.length) {
      settingsHomeLog("ERROR: No .settings-tile elements found.");
      return;
    }

    tiles.forEach((tile) => {
      tile.addEventListener("click", () => {
        const panel = tile.dataset.panel;
        if (!panel) {
          console.warn("[SettingsHome] Tile missing data-panel attribute");
          return;
        }
        settingsHomeLog("Tile clicked → " + panel);
        window.dispatchEvent(new CustomEvent("navigatePanel", { detail: panel }));
      });
    });

    settingsHomeLog("Settings hub ready. tiles=" + tiles.length);
  },
};

export default SettingsHome;
settingsHomeLog("SettingsHome exported.");
