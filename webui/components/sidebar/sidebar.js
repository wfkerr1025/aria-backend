// components/sidebar/sidebar.js
// ARIA Lite Sidebar Controller

function sidebarLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Sidebar", msg);
    }
  } catch (err) {
    console.error("[Sidebar LOG ERROR]", err);
  }
}

sidebarLog("=== SIDEBAR MODULE LOADED ===");

const Sidebar = {
  init() {
    sidebarLog("Sidebar.init() called.");
    console.log("[Sidebar] Initializing...");

    this.bindUI();
    this.bindNewChatButton();

    // Close any open "⋮" chat menu on an outside click — the menu's own
    // toggle button stops propagation (see renderChatList() below) so
    // this only ever fires for genuine outside clicks, not the click
    // that just opened it.
    document.addEventListener("click", () => this._closeAllChatMenus());

    console.log("[Sidebar] Ready");
    sidebarLog("Sidebar subsystem ready.");
  },

  // Direct chat navigation: "New Chat" lives right under the static
  // Chat icon/title (webui/index.html), always visible, on every page —
  // not a .sidebar-btn (it doesn't navigate to a panel by data-panel, it
  // performs an action), so it's wired separately from bindUI()'s
  // generic nav-button loop below. Chat.startNewChat() itself handles
  // navigating to the Chat panel if it isn't already the active one.
  bindNewChatButton() {
    const btn = document.getElementById("sidebar-new-chat-btn");
    if (!btn) {
      sidebarLog("ERROR: #sidebar-new-chat-btn not found.");
      return;
    }
    btn.addEventListener("click", () => {
      sidebarLog("New Chat clicked.");
      window.chatPanel?.startNewChat?.();
    });
  },

  bindUI() {
    sidebarLog("Binding sidebar UI events.");

    // .sidebar-new-chat-btn shares .sidebar-btn's visual styling but
    // isn't a panel-navigation button (no data-panel, wired separately
    // by bindNewChatButton() above) — excluded here so it doesn't also
    // pick up this generic nav-button handler.
    const items = document.querySelectorAll(".sidebar-btn:not(.sidebar-new-chat-btn)");

    if (!items.length) {
      sidebarLog("ERROR: No .sidebar-btn elements found.");
    }

    items.forEach(item => {
      item.addEventListener("click", () => {
        const panel = item.dataset.panel;

        if (!panel) {
          console.warn("[Sidebar] Item missing data-panel attribute");
          sidebarLog("WARN: Sidebar item missing data-panel attribute.");
          return;
        }

        sidebarLog("Sidebar button clicked → " + panel);

        this.setActive(item);

        // Dispatch navigation event to router
        window.dispatchEvent(
          new CustomEvent("navigatePanel", { detail: panel })
        );

        console.log("[Sidebar] Dispatching navigatePanel:", panel);
        sidebarLog("navigatePanel dispatched → " + panel);
      });
    });

    sidebarLog("Sidebar UI bound successfully.");
  },

  // Highlight the active sidebar item
  setActive(activeItem) {
    sidebarLog("Setting active sidebar item.");

    document.querySelectorAll(".sidebar-btn").forEach(item => {
      item.classList.remove("active");
    });

    activeItem.classList.add("active");

    sidebarLog("Active item set → " + activeItem.dataset.panel);
  },

  // Copilot/ChatGPT-style chat list under the Chat button. Called by
  // chat.js (window.Sidebar.renderChatList(...)) whenever its session
  // list changes: first init, re-bind after navigating back to chat,
  // startNewChat(), switchToSession(), deleteSession(), renameSession().
  // Sessions/active id are owned by Chat, not duplicated here — this
  // only renders what it's given and delegates clicks back to
  // Chat.switchToSession()/renameSession()/deleteSession().
  renderChatList(sessions, activeConversationId) {
    const container = document.getElementById("sidebar-chat-list");
    if (!container) {
      sidebarLog("renderChatList() — #sidebar-chat-list not in DOM, skipping.");
      return;
    }

    container.innerHTML = "";

    if (!sessions || !sessions.length) {
      return;
    }

    // Newest first, like Copilot/ChatGPT's history list.
    sessions.slice().reverse().forEach(session => {
      const row = document.createElement("div");
      row.className = "sidebar-chat-row";

      const item = document.createElement("button");
      item.className = "sidebar-chat-item" + (session.id === activeConversationId ? " active" : "");

      const firstUserTurn = (session.history || []).find(t => t.role === "user");
      const title = document.createElement("span");
      title.className = "sidebar-chat-item-title";
      title.textContent = session.label || (firstUserTurn ? firstUserTurn.content.slice(0, 32) : "New Chat");

      const time = document.createElement("span");
      time.className = "sidebar-chat-item-time";
      time.textContent = new Date(session.createdAt || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

      item.appendChild(title);
      item.appendChild(time);

      item.addEventListener("click", () => {
        sidebarLog("Sidebar chat item clicked → " + session.id);
        window.chatPanel?.switchToSession?.(session.id);
      });

      // Hover-reveal "⋮" menu (see sidebar.css) — Rename Chat / Delete
      // Chat, a small Steam-style dropdown anchored to this row.
      const menuBtn = document.createElement("button");
      menuBtn.type = "button";
      menuBtn.className = "sidebar-chat-item-menu-btn";
      menuBtn.textContent = "⋮";
      menuBtn.setAttribute("aria-label", "Chat options");

      const menu = document.createElement("div");
      menu.className = "sidebar-chat-item-menu";

      const renameBtn = document.createElement("button");
      renameBtn.type = "button";
      renameBtn.className = "sidebar-chat-item-menu-option";
      renameBtn.textContent = "Rename Chat";
      renameBtn.addEventListener("click", () => {
        sidebarLog("Rename Chat clicked → " + session.id);
        const newLabel = window.prompt("Rename chat:", title.textContent);
        if (newLabel && newLabel.trim()) {
          window.chatPanel?.renameSession?.(session.id, newLabel.trim());
        }
      });

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "sidebar-chat-item-menu-option sidebar-chat-item-menu-option-danger";
      deleteBtn.textContent = "Delete Chat";
      deleteBtn.addEventListener("click", () => {
        sidebarLog("Delete Chat clicked → " + session.id);
        window.chatPanel?.deleteSession?.(session.id);
      });

      menu.appendChild(renameBtn);
      menu.appendChild(deleteBtn);

      // Stop propagation so the SAME click that opens the menu doesn't
      // also immediately trigger init()'s document-level "close all"
      // listener — genuine outside clicks (and rename/delete, which
      // trigger a full re-render anyway) still close it normally.
      menuBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const isOpen = menu.classList.contains("open");
        this._closeAllChatMenus();
        if (!isOpen) menu.classList.add("open");
      });

      row.appendChild(item);
      row.appendChild(menuBtn);
      row.appendChild(menu);
      container.appendChild(row);
    });

    sidebarLog("Chat list rendered. count=" + sessions.length);
  },

  _closeAllChatMenus() {
    document.querySelectorAll(".sidebar-chat-item-menu.open").forEach(m => m.classList.remove("open"));
  },
};

export default Sidebar;

sidebarLog("Sidebar exported.");
