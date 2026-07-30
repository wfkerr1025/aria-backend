// core/theme-loader.js

console.log("[ThemeLoader] Loaded");

class ThemeLoader {
    constructor() {
        this.baseLink = null;
        this.accentLink = null;

        this.defaultBase = "themes/base/charcoal.css";
        this.defaultAccent = "themes/accents/teal.css";
        this.defaultThemeName = "charcoal";

        this.init();
    }

    init() {
        document.addEventListener("DOMContentLoaded", () => {
            console.log("[ThemeLoader] Initializing...");

            this.baseLink = document.getElementById("aria-base-theme");
            this.accentLink = document.getElementById("aria-accent-theme");

            if (!this.baseLink || !this.accentLink) {
                console.error("[ThemeLoader] Missing theme <link> elements");
                return;
            }

            // Load saved theme paths
            const savedBase = localStorage.getItem("aria-theme-base");
            const savedAccent = localStorage.getItem("aria-theme-accent");
            const savedThemeName = localStorage.getItem("aria-theme-name");

            // Apply CSS files
            this.applyBase(savedBase || this.defaultBase);
            this.applyAccent(savedAccent || this.defaultAccent);

            // Apply theme attribute (CRITICAL)
            this.applyThemeAttribute(savedThemeName || this.defaultThemeName);

            console.log("[ThemeLoader] Ready");
        });
    }

    /* -------------------------------------------------------
       THEME SWAP RULES
       -------------------------------------------------------
       NOTE: Chat subsystem theme is static and not swapped.
             Chat colors are controlled by chat-theme.css
             + user preferences.
    -------------------------------------------------------- */

    /* -------------------------------------------------------
       APPLY BASE THEME FILE
       ------------------------------------------------------- */
    applyBase(path) {
        if (!path) return;
        console.log("[ThemeLoader] Applying base theme:", path);
        this.baseLink.href = path;
        localStorage.setItem("aria-theme-base", path);
    }

    /* -------------------------------------------------------
       APPLY ACCENT THEME FILE
       ------------------------------------------------------- */
    applyAccent(path) {
        if (!path) return;
        console.log("[ThemeLoader] Applying accent theme:", path);
        this.accentLink.href = path;
        localStorage.setItem("aria-theme-accent", path);
    }

    /* -------------------------------------------------------
       APPLY THEME ATTRIBUTE (CRITICAL FOR SEMANTIC TOKENS)
       ------------------------------------------------------- */
    applyThemeAttribute(name) {
        console.log("[ThemeLoader] Setting data-theme:", name);
        document.documentElement.setAttribute("data-theme", name);
        localStorage.setItem("aria-theme-name", name);
    }

    /* -------------------------------------------------------
       PUBLIC API: SET THEME
       ------------------------------------------------------- */
    setTheme(basePath, accentPath, themeName) {
        if (basePath) this.applyBase(basePath);
        if (accentPath) this.applyAccent(accentPath);
        if (themeName) this.applyThemeAttribute(themeName);
    }
}

export const Theme = new ThemeLoader();
