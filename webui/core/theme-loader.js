// core/theme-loader.js

console.log("[ThemeLoader] Loaded");

class ThemeLoader {
    constructor() {
        this.baseLink = null;
        this.accentLink = null;

        this.defaultBase = "themes/base/charcoal.css";
        this.defaultAccent = "themes/accents/teal.css";

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

            const savedBase = localStorage.getItem("aria-theme-base");
            const savedAccent = localStorage.getItem("aria-theme-accent");

            this.applyBase(savedBase || this.defaultBase);
            this.applyAccent(savedAccent || this.defaultAccent);

            console.log("[ThemeLoader] Ready");
        });
    }

    applyBase(path) {
        if (!path) return;

        console.log("[ThemeLoader] Applying base theme:", path);
        this.baseLink.href = path;
        localStorage.setItem("aria-theme-base", path);
    }

    applyAccent(path) {
        if (!path) return;

        console.log("[ThemeLoader] Applying accent theme:", path);
        this.accentLink.href = path;
        localStorage.setItem("aria-theme-accent", path);
    }

    setTheme(basePath, accentPath) {
        this.applyBase(basePath);
        this.applyAccent(accentPath);
    }
}

export const Theme = new ThemeLoader();
