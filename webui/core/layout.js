// core/layout.js
// Responsible for loading and wiring the main UI components:
// sidebar and topbar. Panels are loaded by the Router.

import { Router } from "./router.js";

const Layout = {
  async init() {
    await this.loadSidebar();
    await this.loadTopbar();

    // Router will take over panel loading
    window.dispatchEvent(new Event("layoutReady"));
  },

  async loadSidebar() {
    const container = document.getElementById("sidebar-container");
    if (!container) return console.error("[Layout] sidebar-container not found");

    try {
      const html = await fetch("components/sidebar/sidebar.html").then(r => r.text());
      container.innerHTML = html;

      const module = await import("../components/sidebar/sidebar.js");
      module.Sidebar?.init?.();
    } catch (err) {
      console.error("[Layout] Failed to load sidebar:", err);
    }
  },

  async loadTopbar() {
    const container = document.getElementById("main-container");
    if (!container) return console.error("[Layout] main-container not found");

    try {
      const html = await fetch("components/topbar/topbar.html").then(r => r.text());
      container.insertAdjacentHTML("afterbegin", html);

      const module = await import("../components/topbar/topbar.js");
      module.Topbar?.init?.();
    } catch (err) {
      console.error("[Layout] Failed to load topbar:", err);
    }
  }
};

window.addEventListener("DOMContentLoaded", () => {
  Layout.init().catch(err => console.error("[Layout] init failed:", err));
});

// Router starts AFTER layout is ready
window.addEventListener("layoutReady", () => {
  Router.init();
});

export { Layout };
