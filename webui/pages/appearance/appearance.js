// pages/appearance/appearance.js
//
// Appearance Settings: everything that changes how ARIA-Lite LOOKS, not
// how it behaves. Two of these controls (Base Theme, Accent Color) are
// the exact same working feature the old single-page Settings panel
// already had (webui/core/theme-loader.js's Theme singleton) — moved
// here unchanged as part of splitting Settings into a Steam-style hub.
// The rest (Theme light/dark/system, Font Size, UI Density, Animations,
// Chat Bubble Style) are new preferences: each persists to localStorage
// and applies as a `data-*` attribute on <html>, the same mechanism
// Theme.applyThemeAttribute() already uses for the base theme, so every
// existing semantic-token CSS file can key off them the same way.
//
// Scope note: Chat Bubble Style persists and applies its attribute like
// every other control here, but no chat.css rule currently keys off
// `data-chat-bubble-style` — actually re-skinning chat bubbles per style
// is chat-rendering work, out of scope for this settings/routing pass.
// The control is real and wired, not a dead stub; a future chat.css
// pass can add the corresponding rules without touching this file.

import { Theme } from "../../core/theme-loader.js";

function appearanceLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Appearance", msg);
    }
  } catch (err) {
    console.error("[Appearance LOG ERROR]", err);
  }
}

appearanceLog("=== APPEARANCE MODULE LOADED ===");

const BASE_THEMES = ["charcoal", "cream", "crimson", "ruby", "pinknoir", "purpledepth", "violetbloom"];
const ACCENT_THEMES = ["teal", "pink", "green", "yellow", "red", "silver", "purple", "orange", "bluegrey"];

const baseThemePath = (name) => `themes/base/${name}.css`;
const accentThemePath = (name) => `themes/accents/${name}.css`;

// name -> {attribute, storageKey, default}
const SIMPLE_PREFS = {
  colorScheme: { attribute: "data-color-scheme", storageKey: "aria-color-scheme", default: "system" },
  fontSize: { attribute: "data-font-size", storageKey: "aria-font-size", default: "medium" },
  density: { attribute: "data-density", storageKey: "aria-density", default: "comfortable" },
  animations: { attribute: "data-animations", storageKey: "aria-animations", default: "on" },
  chatBubbleStyle: { attribute: "data-chat-bubble-style", storageKey: "aria-chat-bubble-style", default: "rounded" },
};

function applyPref(pref, value) {
  document.documentElement.setAttribute(pref.attribute, value);
  localStorage.setItem(pref.storageKey, value);
}

function loadPref(pref) {
  return localStorage.getItem(pref.storageKey) || pref.default;
}

export default {
  init() {
    appearanceLog("Appearance.init() called.");
    console.log("[Appearance] Initializing appearance panel");

    this.initBaseAndAccentThemes();
    this.initColorScheme();
    this.initSimpleSelect("fontSize", "appearance-font-size-select");
    this.initSimpleSelect("density", "appearance-density-select");
    this.initAnimationsToggle();
    this.initSimpleSelect("chatBubbleStyle", "appearance-chat-bubble-select");

    console.log("[Appearance] Appearance panel initialized");
    appearanceLog("Appearance panel initialized.");
  },

  // ---- Base theme (palette) + accent color — unchanged from the old
  // settings.js, just relocated. ----
  initBaseAndAccentThemes() {
    const baseSelect = document.getElementById("appearance-base-theme-select");
    const accentSelect = document.getElementById("appearance-accent-select");

    if (!baseSelect || !accentSelect) {
      appearanceLog("ERROR: Base/accent theme dropdown elements not found.");
      return;
    }

    BASE_THEMES.forEach((theme) => {
      const opt = document.createElement("option");
      opt.value = theme;
      opt.textContent = theme;
      baseSelect.appendChild(opt);
    });

    ACCENT_THEMES.forEach((theme) => {
      const opt = document.createElement("option");
      opt.value = theme;
      opt.textContent = theme;
      accentSelect.appendChild(opt);
    });

    const savedBase = localStorage.getItem("aria-theme-name") || "charcoal";
    const savedAccent = ACCENT_THEMES.find((a) => (localStorage.getItem("aria-theme-accent") || "").endsWith(`${a}.css`)) || "teal";

    baseSelect.value = savedBase;
    accentSelect.value = savedAccent;

    baseSelect.addEventListener("change", () => {
      const value = baseSelect.value;
      appearanceLog("Base theme changed → " + value);
      Theme.applyBase(baseThemePath(value));
      Theme.applyThemeAttribute(value);
    });

    accentSelect.addEventListener("change", () => {
      const value = accentSelect.value;
      appearanceLog("Accent theme changed → " + value);
      Theme.applyAccent(accentThemePath(value));
    });
  },

  // ---- Theme (light/dark/system) ----
  initColorScheme() {
    const select = document.getElementById("appearance-color-scheme-select");
    if (!select) {
      appearanceLog("ERROR: #appearance-color-scheme-select not found.");
      return;
    }

    const pref = SIMPLE_PREFS.colorScheme;
    const saved = loadPref(pref);
    select.value = saved;
    applyPref(pref, saved);

    select.addEventListener("change", () => {
      appearanceLog("Color scheme changed → " + select.value);
      applyPref(pref, select.value);
    });
  },

  // ---- Generic {attribute}-backed <select> preferences (Font Size, UI
  // Density, Chat Bubble Style — same shape, so one function handles all
  // three instead of three near-identical copies). ----
  initSimpleSelect(prefName, elementId) {
    const select = document.getElementById(elementId);
    if (!select) {
      appearanceLog(`ERROR: #${elementId} not found.`);
      return;
    }

    const pref = SIMPLE_PREFS[prefName];
    const saved = loadPref(pref);
    select.value = saved;
    applyPref(pref, saved);

    select.addEventListener("change", () => {
      appearanceLog(`${prefName} changed → ` + select.value);
      applyPref(pref, select.value);
    });
  },

  // ---- Animation toggle ----
  initAnimationsToggle() {
    const toggle = document.getElementById("appearance-animations-toggle");
    if (!toggle) {
      appearanceLog("ERROR: #appearance-animations-toggle not found.");
      return;
    }

    const pref = SIMPLE_PREFS.animations;
    const saved = loadPref(pref);
    toggle.checked = saved !== "off";
    applyPref(pref, saved);

    toggle.addEventListener("change", () => {
      const value = toggle.checked ? "on" : "off";
      appearanceLog("Animations toggled → " + value);
      applyPref(pref, value);
    });
  },
};

appearanceLog("Appearance exported.");
