// core/router.js

function routerLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Router", msg);
    }
  } catch (err) {
    console.error("[Router LOG ERROR]", err);
  }
}

routerLog("=== ROUTER MODULE LOADED ===");

const Router = {
  currentPanel: null,

  // Panel names, not real URL paths — this app has no history/hash
  // router (Electron single-window app, navigation is the
  // "navigatePanel" CustomEvent sidebar.js/settings_home.js dispatch).
  // The Settings hub's subpages use "settings/..." names purely as a
  // readable, hierarchical naming convention that also matches the
  // task's requested route names; Router.navigate("settings/appearance")
  // works exactly like any other panel name.
  routes: {
    chat: "components/chat/chat.html",
    diagnostics: "components/diagnostics/diagnostics.html",
    tools: "components/tools/tools.html",
    plugins: "pages/plugins/plugins.html",
    fileops: "components/fileops/fileops.html",
    models: "pages/models/models.html",
    debug: "pages/debug/debug.html",

    // Settings hub — Steam-style tile page + four dedicated subpages.
    // Replaces the old single-page components/settings/settings.html.
    settings: "pages/settings_home/settings_home.html",
    "settings/appearance": "pages/appearance/appearance.html",
    "settings/cloud-llms": "pages/cloud_llms/cloud_llms.html",
    "settings/modules": "pages/modules/modules.html",
    "settings/local-models": "pages/local_models/local_models.html",
    "settings/workspaces": "pages/workspaces/workspaces.html",

    // Plugin configuration. Three routes, one page: they do the same
    // four things and differ only in which fields exist, so the id is
    // read from the route name rather than the page being copied twice.
    "plugins/unity-config": "pages/plugin_config/plugin_config.html",
    "plugins/blender-config": "pages/plugin_config/plugin_config.html",
    "plugins/ludo-config": "pages/plugin_config/plugin_config.html",

    // The Unity Editor's tools, as things to run. Its own page rather
    // than a section on the hub: a connected Editor reports 142 of
    // them, which is twenty times the size of the page that listed
    // them and needs a filter of its own.
    "plugins/unity-cli-commands": "pages/unity_commands/unity_commands.html",
  },

  init() {
    console.log("[Router] Initialized");
    routerLog("Router initialized.");

    this.navigate("chat");

    window.addEventListener("navigatePanel", (ev) => {
      routerLog("navigatePanel event → " + ev.detail);
      this.navigate(ev.detail);
    });
  },

  /**
   * The HTML for a panel name, or null when there is no such panel.
   *
   * Every "plugins/<id>-config" route resolves to the one config page,
   * whether or not it is listed above. It has to: a discovered plugin's
   * id is whatever ARIA found on the machine -- godot, unreal, anything
   * added later -- and a hard-coded table cannot list a route for a
   * plugin nobody had heard of when the table was written. The three
   * listed entries stay for readability; this is what makes the fourth
   * one work.
   *
   * The pattern is deliberately narrow. Only "plugins/" + an id made of
   * word characters + "-config" matches, so this cannot be talked into
   * fetching an arbitrary path.
   */
  resolve(panelName) {
    const name = String(panelName || "");
    if (this.routes[name]) return this.routes[name];

    if (/^plugins\/[\w.-]+-config$/.test(name)) {
      return "pages/plugin_config/plugin_config.html";
    }
    return null;
  },

  async navigate(panelName) {
    console.log("[Router] Navigating to:", panelName);
    routerLog("Navigating to panel: " + panelName);

    const route = this.resolve(panelName);
    if (!route) {
      console.error(`[Router] Unknown panel: ${panelName}`);
      routerLog("ERROR: Unknown panel: " + panelName);
      return;
    }

    this.currentPanel = panelName;
    this.updateSidebarSelection(panelName);

    const container = document.getElementById("panel-container");
    if (!container) {
      console.error("[Router] panel-container not found");
      routerLog("ERROR: panel-container not found.");
      return;
    }

    try {
      const htmlPath = route;
      routerLog("Fetching HTML for panel: " + htmlPath);
      const html = await fetch(htmlPath).then(r => r.text());
      container.innerHTML = html;

      // Derive the JS module path from the route's HTML path so
      // panels can live under components/ or pages/ (or anywhere
      // else) as long as their .js sits next to their .html.
      const jsPath = "../" + htmlPath.replace(/\.html$/, ".js");
      routerLog("Attempting JS module load: " + jsPath);

      try {
        const module = await import(jsPath);
        module?.default?.init?.();
        routerLog("JS module loaded successfully: " + jsPath);
      } catch (err) {
        console.warn(`[Router] No JS module for ${panelName}`, err);
        routerLog("WARN: No JS module for " + panelName + " (" + err + ")");
      }

      console.log(`[Router] Loaded panel: ${panelName}`);
      routerLog("Panel loaded successfully: " + panelName);

    } catch (err) {
      console.error(`[Router] Failed to load panel ${panelName}:`, err);
      routerLog("ERROR loading panel " + panelName + ": " + err);
    }
  },

  updateSidebarSelection(panelName) {
    routerLog("Updating sidebar selection → " + panelName);

    document.querySelectorAll(".sidebar-btn").forEach(btn => {
      btn.classList.remove("active");
    });

    // A settings/* subpage (Appearance, Cloud LLMs, Modules, Local
    // Models) has no sidebar button of its own — only the top-level
    // "settings" hub does — so keep that one highlighted while any of
    // its subpages is open, the same way a real app keeps a parent nav
    // item lit up while a child page is active.
    const exactMatch = document.querySelector(`.sidebar-btn[data-panel="${panelName}"]`);
    const fallbackPanel = panelName.startsWith("settings/") ? "settings"
      : panelName.startsWith("plugins/") ? "plugins"
      : null;
    const activeBtn = exactMatch || (fallbackPanel && document.querySelector(`.sidebar-btn[data-panel="${fallbackPanel}"]`));

    if (activeBtn) {
      activeBtn.classList.add("active");
      routerLog("Sidebar active button set: " + panelName);
    } else {
      routerLog("WARN: No sidebar button found for " + panelName);
    }
  }
};

export { Router };
routerLog("Router exported.");
