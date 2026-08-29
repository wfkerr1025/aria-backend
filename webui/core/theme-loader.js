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
    this.syncWindowChrome();
  }

  /* -------------------------------------------------------
     WINDOW CHROME
     -------------------------------------------------------
     The title bar is hidden and the minimise/maximise/close buttons are
     drawn by Electron as an overlay whose colours the app chooses. They
     are chosen here, from the live stylesheet, because that is the only
     place that knows what the active theme actually resolved to -- a
     copy of the palette in the main process is one that goes stale the
     first time a theme is edited, and ARIA has seven of them.

     Deferred a frame: the stylesheet that defines these variables is
     swapped immediately before this runs, and reading a custom property
     in the same tick returns the OLD theme's value. */
  syncWindowChrome() {
    if (typeof window === "undefined" || !window.aria?.setWindowChrome) return;

    requestAnimationFrame(() => {
      const styles = getComputedStyle(document.documentElement);
      const color = styles.getPropertyValue("--bg-app").trim();
      const symbolColor = styles.getPropertyValue("--text-primary").trim();

      // Only real colours. An unresolved variable reads as "", and
      // handing that to setTitleBarOverlay throws rather than falling
      // back -- so the window would keep whatever it had, which is the
      // right outcome but a confusing way to reach it.
      if (!color || !symbolColor) {
        themeLog("window chrome not synced: theme variables did not resolve");
        return;
      }

      themeLog(`Window chrome -> ${color} / ${symbolColor}`);
      window.aria.setWindowChrome({ color, symbolColor });
    });
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
