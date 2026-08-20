// core/theme-loader.js

function themeLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Theme", msg);
    }
  } catch (err) {
    console.error("[ThemeLoader LOG ERROR]", err);
  }
}

console.log("[ThemeLoader] Loaded");
themeLog("=== THEME LOADER MODULE LOADED ===");

class ThemeLoader {
  constructor() {
    this.baseLink = null;
    this.accentLink = null;

    this.defaultBase = "themes/base/charcoal.css";
    this.defaultAccent = "themes/accents/teal.css";
    this.defaultThemeName = "charcoal";

    themeLog("Constructor: defaults set.");
    this.init();
  }

  init() {
    document.addEventListener("DOMContentLoaded", () => {
      console.log("[ThemeLoader] Initializing...");
      themeLog("Initializing theme loader…");

      this.baseLink = document.getElementById("aria-base-theme");
      this.accentLink = document.getElementById("aria-accent-theme");

      if (!this.baseLink || !this.accentLink) {
        console.error("[ThemeLoader] Missing theme <link> elements");
        themeLog("ERROR: Missing <link> elements for base/accent themes.");
        return;
      }

      // Load saved theme paths
      const savedBase = localStorage.getItem("aria-theme-base");
      const savedAccent = localStorage.getItem("aria-theme-accent");
      const savedThemeName = localStorage.getItem("aria-theme-name");

      themeLog("Saved base = " + savedBase);
      themeLog("Saved accent = " + savedAccent);
      themeLog("Saved themeName = " + savedThemeName);

      // Apply CSS files
      this.applyBase(savedBase || this.defaultBase);
      this.applyAccent(savedAccent || this.defaultAccent);

      // Apply theme attribute (CRITICAL)
      this.applyThemeAttribute(savedThemeName || this.defaultThemeName);

      themeLog("Theme loader ready.");
      console.log("[ThemeLoader] Ready");
    });
  }

  /* -------------------------------------------------------
     APPLY BASE THEME FILE
     ------------------------------------------------------- */
  applyBase(path) {
    if (!path) return;
    console.log("[ThemeLoader] Applying base theme:", path);
    themeLog("Applying base theme: " + path);

    this.baseLink.href = path;
    localStorage.setItem("aria-theme-base", path);

    this.baseLink.addEventListener("load", () => {
      themeLog("Base theme loaded successfully: " + path);
    });

    this.baseLink.addEventListener("error", () => {
      themeLog("ERROR loading base theme: " + path);
    });
  }

  /* -------------------------------------------------------
     APPLY ACCENT THEME FILE
     ------------------------------------------------------- */
  applyAccent(path) {
    if (!path) return;
    console.log("[ThemeLoader] Applying accent theme:", path);
    themeLog("Applying accent theme: " + path);

    this.accentLink.href = path;
    localStorage.setItem("aria-theme-accent", path);

    this.accentLink.addEventListener("load", () => {
      themeLog("Accent theme loaded successfully: " + path);
    });

    this.accentLink.addEventListener("error", () => {
      themeLog("ERROR loading accent theme: " + path);
    });
  }

  /* -------------------------------------------------------
     APPLY THEME ATTRIBUTE (CRITICAL FOR SEMANTIC TOKENS)
     ------------------------------------------------------- */
  applyThemeAttribute(name) {
    console.log("[ThemeLoader] Setting data-theme:", name);
    themeLog("Setting data-theme = " + name);

    document.documentElement.setAttribute("data-theme", name);
    localStorage.setItem("aria-theme-name", name);

    themeLog("Semantic tokens activated for theme: " + name);
  }

  /* -------------------------------------------------------
     PUBLIC API: SET THEME
     ------------------------------------------------------- */
  setTheme(basePath, accentPath, themeName) {
    themeLog("setTheme() called with base=" + basePath + ", accent=" + accentPath + ", name=" + themeName);

    if (basePath) this.applyBase(basePath);
    if (accentPath) this.applyAccent(accentPath);
    if (themeName) this.applyThemeAttribute(themeName);

    themeLog("Theme switch complete.");
  }
}

export const Theme = new ThemeLoader();
themeLog("ThemeLoader instance created.");
