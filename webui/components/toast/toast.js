// components/toast/toast.js
// ARIA Lite — Global Toast Notification System (Premium, Namespaced)

function toastLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Toast", msg);
    }
  } catch (err) {
    console.error("[Toast LOG ERROR]", err);
  }
}

toastLog("=== TOAST MODULE LOADED ===");

export const Toast = {
  init() {
    toastLog("Toast.init() called.");
    console.log("[Toast] Initialized");

    this.container = document.getElementById("aria-toast-container");

    // If container missing, auto-create it
    if (!this.container) {
      console.warn("[Toast] Missing #aria-toast-container — creating dynamically");
      toastLog("Container missing → creating dynamically.");

      this.container = document.createElement("div");
      this.container.id = "aria-toast-container";
      this.container.className = "aria-toast-container";

      document.body.appendChild(this.container);

      toastLog("Toast container appended to DOM.");
    }

    // Expose global API for Bridge + Topbar
    window.showToast = (message, mode = "auto") => {
      toastLog(`Global showToast() called → ${message} (mode=${mode})`);
      this.show(message, mode);
    };

    toastLog("Toast subsystem ready.");
  },

  /* -----------------------------------------------------------
     PUBLIC API — Show a toast anywhere in the app
     ----------------------------------------------------------- */
  show(message, mode = "auto") {
    toastLog(`Toast.show() → "${message}" (mode=${mode})`);

    if (!this.container) {
      toastLog("ERROR: Toast container missing.");
      return;
    }

    const toast = document.createElement("div");

    // IMPORTANT: Namespaced mode class to avoid collisions with Topbar
    const modeClass = `toast-mode-${mode}`;
    toast.className = `aria-toast ${modeClass}`;
    toast.textContent = message;

    toastLog("Toast element created with class: " + modeClass);

    this.container.appendChild(toast);
    toastLog("Toast appended to container.");

    // Trigger fade-in animation
    requestAnimationFrame(() => {
      toast.classList.add("visible");
      toastLog("Toast fade-in triggered.");
    });

    // Auto-hide after 3 seconds
    setTimeout(() => {
      toast.classList.add("hide");
      toastLog("Toast fade-out triggered.");

      setTimeout(() => {
        toast.remove();
        toastLog("Toast removed from DOM.");
      }, 350);
    }, 3000);
  }
};

export default Toast;

toastLog("Toast exported.");
