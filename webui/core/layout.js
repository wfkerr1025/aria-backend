// core/layout.js
// Responsible for loading and wiring the main UI components:
// sidebar and topbar. Panels are loaded by the Router.

import { Router } from "./router.js";

function layoutLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Layout", msg);
    }
  } catch (err) {
    console.error("[Layout LOG ERROR]", err);
  }
}

layoutLog("=== LAYOUT MODULE LOADED ===");

const Layout = {
  async init() {
    layoutLog("Layout.init() called.");

    await this.loadSidebar();
    await this.loadTopbar();

    layoutLog("Dispatching layoutReady event.");
    window.dispatchEvent(new Event("layoutReady"));
  },

  async loadSidebar() {
    const container = document.getElementById("sidebar-container");
    if (!container) {
      console.error("[Layout] sidebar-container not found");
      layoutLog("ERROR: sidebar-container not found.");
      return;
    }

    layoutLog("Loading sidebar HTML…");

    try {
      const html = await fetch("components/sidebar/sidebar.html").then(r => r.text());
      container.innerHTML = html;

      layoutLog("Sidebar HTML loaded. Importing JS module…");

      const module = await import("../components/sidebar/sidebar.js");
      module.Sidebar?.init?.();

      layoutLog("Sidebar module initialized.");
    } catch (err) {
      console.error("[Layout] Failed to load sidebar:", err);
      layoutLog("ERROR loading sidebar: " + err);
    }
  },

  async loadTopbar() {
    const container = document.getElementById("main-container");
    if (!container) {
      console.error("[Layout] main-container not found");
      layoutLog("ERROR: main-container not found.");
      return;
    }

    layoutLog("Loading topbar HTML…");

    try {
      const html = await fetch("components/topbar/topbar.html").then(r => r.text());
      container.insertAdjacentHTML("afterbegin", html);

      layoutLog("Topbar HTML loaded. Importing JS module…");

      const module = await import("../components/topbar/topbar.js");
      module.Topbar?.init?.();

      layoutLog("Topbar module initialized.");
    } catch (err) {
      console.error("[Layout] Failed to load topbar:", err);
      layoutLog("ERROR loading topbar: " + err);
    }
  }
};

window.addEventListener("DOMContentLoaded", () => {
  layoutLog("DOMContentLoaded → calling Layout.init()");
  Layout.init().catch(err => {
    console.error("[Layout] init failed:", err);
    layoutLog("ERROR: Layout.init() failed: " + err);
  });
});

// Router starts AFTER layout is ready
window.addEventListener("layoutReady", () => {
  layoutLog("layoutReady event → Router.init()");
  Router.init();
});

export { Layout };
layoutLog("Layout exported.");
