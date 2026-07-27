import { ThemeLoader } from "../../scripts/theme/theme-loader.js";

export default {
    init() {
        console.log("[Settings] Initializing settings panel");

        const baseSelect = document.getElementById("base-theme-select");
        const accentSelect = document.getElementById("accent-theme-select");

        if (!baseSelect || !accentSelect) {
            console.error("[Settings] Dropdown elements not found");
            return;
        }

        // Base themes
        const baseThemes = [
            "ui-charcoal.css",
            "ui-cream.css",
            "ui-crimson.css",
            "ui-ruby.css",
            "ui-pinknoir.css",
            "ui-purpledepth.css",
            "ui-violetbloom.css"
        ];

        // Accent themes
        const accentThemes = [
            "teal.css",
            "pink.css",
            "green.css",
            "yellow.css",
            "red.css",
            "silver.css",
            "purple.css",
            "orange.css",
            "bluegrey.css"
        ];

        // Populate Base Theme dropdown
        baseThemes.forEach(theme => {
            const opt = document.createElement("option");
            opt.value = theme;
            opt.textContent = theme.replace(".css", "");
            baseSelect.appendChild(opt);
        });

        // Populate Accent Theme dropdown
        accentThemes.forEach(theme => {
            const opt = document.createElement("option");
            opt.value = theme;
            opt.textContent = theme.replace(".css", "");
            accentSelect.appendChild(opt);
        });

        // Restore saved selections
        baseSelect.value = localStorage.getItem("aria-base-theme") || "ui-charcoal.css";
        accentSelect.value = localStorage.getItem("aria-accent-theme") || "teal.css";

        // Apply base theme
        baseSelect.addEventListener("change", () => {
            ThemeLoader.applyBase(baseSelect.value);
        });

        // Apply accent theme
        accentSelect.addEventListener("change", () => {
            ThemeLoader.applyAccent(accentSelect.value);
        });

        console.log("[Settings] Settings panel initialized");
    }
};
