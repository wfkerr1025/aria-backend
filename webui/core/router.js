// core/router.js
const Router = {
  currentPanel: null,

  routes: {
    chat: "components/chat/chat.html",
    diagnostics: "components/diagnostics/diagnostics.html",
    tools: "components/tools/tools.html",
    plugins: "components/plugins/plugins.html",
    fileops: "components/fileops/fileops.html",
    settings: "components/settings/settings.html"
  },

  init() {
    console.log("[Router] Initialized");
    this.navigate("chat");

    window.addEventListener("navigatePanel", (ev) => {
      this.navigate(ev.detail);
    });
  },

  async navigate(panelName) {
    console.log("[Router] Navigating to:", panelName);

    if (!this.routes[panelName]) {
      console.error(`[Router] Unknown panel: ${panelName}`);
      return;
    }

    this.currentPanel = panelName;
    this.updateSidebarSelection(panelName);

    const container = document.getElementById("panel-container");
    if (!container) {
      console.error("[Router] panel-container not found");
      return;
    }

    try {
      const html = await fetch(this.routes[panelName]).then(r => r.text());
      container.innerHTML = html;

      const jsPath = `/components/${panelName}/${panelName}.js`;

      try {
        const module = await import(jsPath);
        module?.default?.init?.();
      } catch (err) {
        console.warn(`[Router] No JS module for ${panelName}`, err);
      }

      console.log(`[Router] Loaded panel: ${panelName}`);
    } catch (err) {
      console.error(`[Router] Failed to load panel ${panelName}:`, err);
    }
  },

  updateSidebarSelection(panelName) {
    document.querySelectorAll(".sidebar-btn").forEach(btn => {
      btn.classList.remove("active");
    });

    const activeBtn = document.querySelector(`.sidebar-btn[data-panel="${panelName}"]`);
    if (activeBtn) activeBtn.classList.add("active");
  }
};

export { Router };
