function compatLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("CompatibilitySummary", msg);
    }
  } catch (err) {
    console.error("[CompatibilitySummary LOG ERROR]", err);
  }
}

compatLog("=== COMPATIBILITY SUMMARY MODULE LOADED ===");

// Row labels for each check key returned by check_requirements()
const CHECK_LABELS = {
  ram: "System RAM",
  cpu_cores: "CPU Cores",
  cpu_features: "CPU Features",
  vram: "GPU VRAM",
  avx2_tier: "AVX2 Throughput",
  storage: "Storage",
};

export const CompatibilitySummary = {
  init() {
    compatLog("CompatibilitySummary.init() called.");
    this.cache();
    compatLog("CompatibilitySummary ready.");
  },

  cache() {
    this.root = document.getElementById("compat-summary");
    this.badge = document.getElementById("compat-summary-badge");
    this.rows = document.getElementById("compat-summary-rows");

    compatLog(
      "CACHE RESULT: " +
        JSON.stringify({
          root: !!this.root,
          badge: !!this.badge,
          rows: !!this.rows,
        })
    );
  },

  /**
   * Render a compatibility dict as returned by
   * backend.core.compatibility_checker.check_requirements():
   * {
   *   model_id, difficulty, checks: {ram, cpu_cores, cpu_features, vram},
   *   meets_minimum, meets_recommended,
   *   missing_minimum, missing_recommended
   * }
   */
  render(compat) {
    compatLog("render() called with compat=" + JSON.stringify(compat));

    if (!compat) {
      compatLog("render() → no compat data, clearing summary.");
      this.setBadge("unknown", "No compatibility data");
      if (this.rows) this.rows.innerHTML = "";
      return;
    }

    if (!compat.meets_minimum) {
      this.setBadge("fail", "Your system may experience slow performance.");
    } else if (!compat.meets_recommended) {
      this.setBadge("warn", "Your system meets minimum requirements.");
    } else if (compat.exceeds_recommended) {
      this.setBadge("pass", "Your system exceeds recommended requirements.");
    } else {
      this.setBadge("pass", "Your system meets recommended requirements.");
    }

    if (this.rows) {
      this.rows.innerHTML = "";

      Object.entries(compat.checks || {}).forEach(([key, check]) => {
        const row = document.createElement("div");
        row.className = "compat-row";

        const status = check.pass_min
          ? (check.pass_rec ? "pass" : "warn")
          : "fail";

        const icon = status === "fail" ? "✘" : "✔";
        const label = CHECK_LABELS[key] || key;
        const tooltip = check.pass_min
          ? (check.pass_rec
              ? `${label}: ${this.formatValue(check.value)} meets the recommended target (${this.formatValue(check.rec)}).`
              : `${label}: ${this.formatValue(check.value)} meets the minimum (${this.formatValue(check.min)}) but not the recommended target (${this.formatValue(check.rec)}).`)
          : `${label}: ${this.formatValue(check.value)} is below the minimum required (${this.formatValue(check.min)}).`;

        row.title = tooltip;
        row.innerHTML = `
          <span class="compat-indicator compat-indicator-${status}" data-status="${status}" aria-hidden="true">${icon}</span>
          <span class="compat-row-label">${label}</span>
          <span class="compat-row-value">${this.formatValue(check.value)}</span>
          <span class="compat-row-target">min ${this.formatValue(check.min)} / rec ${this.formatValue(check.rec)}</span>
        `;

        this.rows.appendChild(row);
      });
    }

    compatLog("render() complete.");
  },

  formatValue(value) {
    if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
    return value;
  },

  setBadge(status, text) {
    if (!this.badge) return;
    this.badge.textContent = text;
    this.badge.className = "compat-badge compat-badge-" + status;
    compatLog(`Badge set → status=${status}, text=${text}`);
  },
};
