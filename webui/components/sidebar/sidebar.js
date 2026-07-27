// components/sidebar/sidebar.js
// ARIA Lite Sidebar Controller

const Sidebar = {
  init() {
    console.log("[Sidebar] Initializing...");
    this.bindUI();
    console.log("[Sidebar] Ready");
  },

  bindUI() {
    // FIXED: use .sidebar-btn instead of .sidebar-item
    const items = document.querySelectorAll(".sidebar-btn");

    items.forEach(item => {
      item.addEventListener("click", () => {
        const panel = item.dataset.panel;
        if (!panel) {
          console.warn("[Sidebar] Item missing data-panel attribute");
          return;
        }

        this.setActive(item);

        // Dispatch navigation event to router
        window.dispatchEvent(
          new CustomEvent("navigatePanel", { detail: panel })
        );

        console.log("[Sidebar] Dispatching navigatePanel:", panel);
      });
    });
  },

  // Highlight the active sidebar item
  setActive(activeItem) {
    // FIXED: use .sidebar-btn instead of .sidebar-item
    document.querySelectorAll(".sidebar-btn").forEach(item => {
      item.classList.remove("active");
    });

    activeItem.classList.add("active");
  }
};

export default Sidebar;
