import tkinter as tk
from tkinter import ttk, messagebox
import traceback
import os
import time


class TelemetryPanel(tk.Frame):
    """
    Unified architecture:
    - Frame-based diagnostic panel
    - (parent, app) signature
    - Uses app.telemetry for metrics
    - Uses app.theme_manager for styling
    - Live-updating startup + packet telemetry
    """

    def __init__(self, parent, app):
        super().__init__(parent)

        # Unified architecture references
        self.app = app
        self.backend = app.backend
        self.telemetry = app.telemetry
        self.theme = app.theme_manager
        self.window = app.window

        # Install safe_call + error reporter
        self.safe_call = self._install_safe_call()
        self._report_error = self._install_error_reporter()

        # Build UI safely
        self.safe_call("Build TelemetryPanel UI", self._build_ui)

        # Start refresh loop AFTER UI is built
        self.after(100, lambda: self.safe_call("Start Telemetry Refresh", self._refresh))

    # =========================================================
    # SAFE CALL SYSTEM
    # =========================================================
    def _install_safe_call(self):
        def safe_call(label, func):
            try:
                print(f"[TelemetryPanel] Executing: {label}")
                start = time.time()
                result = func()
                elapsed = round((time.time() - start) * 1000, 2)
                print(f"[TelemetryPanel] Completed: {label} ({elapsed} ms)")
                return result
            except Exception as e:
                self._report_error(label, e)
                return None
        return safe_call

    def _install_error_reporter(self):
        def report_error(label, exception):
            tb = traceback.format_exc()

            print("\n" + "=" * 60)
            print(f"[TelemetryPanel ERROR] Action: {label}")
            print(f"[TelemetryPanel ERROR] Exception: {exception}")
            print(tb)
            print("=" * 60 + "\n")

            os.makedirs("logs", exist_ok=True)
            with open("logs/telemetry_panel_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
                f.write(tb + "\n")

            try:
                messagebox.showerror(
                    "Telemetry Panel Error",
                    f"An error occurred while running '{label}'.\n\n{exception}"
                )
            except Exception:
                pass

        return report_error

    # =========================================================
    # BUILD UI
    # =========================================================
    def _build_ui(self):
        self.configure(bg=self.theme.get("bg"))

        # Header
        header = tk.Label(
            self,
            text="Live Telemetry",
            bg=self.theme.get("header_bg"),
            fg=self.theme.get("header_fg"),
            font=self.theme.get("font_header")
        )
        header.pack(anchor="w", pady=(10, 5), fill="x")

        # =========================================================
        # STARTUP METRICS
        # =========================================================
        startup_container = tk.Frame(self, bg=self.theme.get("panel_bg"))
        startup_container.pack(fill="x", padx=10, pady=5)

        self.startup_frame = tk.LabelFrame(
            startup_container,
            text="Startup Duration",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("header_fg"),
            bd=1
        )
        self.startup_frame.pack(fill="x")

        self.startup_label = tk.Label(
            self.startup_frame,
            text="No startup data yet",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            anchor="w"
        )
        self.startup_label.pack(anchor="w")

        # =========================================================
        # PACKET METRICS
        # =========================================================
        packet_container = tk.Frame(self, bg=self.theme.get("panel_bg"))
        packet_container.pack(fill="x", padx=10, pady=5)

        self.packet_frame = tk.LabelFrame(
            packet_container,
            text="Packet Execution Time",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("header_fg"),
            bd=1
        )
        self.packet_frame.pack(fill="x")

        self.packet_label = tk.Label(
            self.packet_frame,
            text="No packet data yet",
            bg=self.theme.get("panel_bg"),
            fg=self.theme.get("text"),
            anchor="w"
        )
        self.packet_label.pack(anchor="w")

    # =========================================================
    # REFRESH LOOP
    # =========================================================
    def _refresh(self):
        self.safe_call("Update Startup Metrics", self._update_startup)
        self.safe_call("Update Packet Metrics", self._update_packets)
        self.after(1500, lambda: self.safe_call("Refresh Telemetry", self._refresh))

    # =========================================================
    # STARTUP METRICS
    # =========================================================
    def _update_startup(self):
        metrics = self.telemetry.get_startup_metrics()

        if not metrics or metrics["min"] is None:
            self.startup_label.configure(text="No startup data yet")
            return

        text = (
            f"Min: {metrics['min']:.2f}s   "
            f"Avg: {metrics['avg']:.2f}s   "
            f"Max: {metrics['max']:.2f}s\n"
            f"Samples: {self._sparkline(metrics['samples'])}"
        )

        self.startup_label.configure(text=text)

    # =========================================================
    # PACKET METRICS
    # =========================================================
    def _update_packets(self):
        metrics = self.telemetry.get_packet_metrics()

        if not metrics or metrics["min"] is None:
            self.packet_label.configure(text="No packet data yet")
            return

        text = (
            f"Min: {metrics['min']:.2f}s   "
            f"Avg: {metrics['avg']:.2f}s   "
            f"Max: {metrics['max']:.2f}s\n"
            f"Samples: {self._sparkline(metrics['samples'])}"
        )

        self.packet_label.configure(text=text)

    # =========================================================
    # SPARKLINE
    # =========================================================
    def _sparkline(self, samples):
        try:
            if not samples:
                return ""

            max_val = max(samples)
            if max_val == 0:
                return "·" * len(samples)

            blocks = "▁▂▃▄▅▆▇█"
            result = []

            for s in samples:
                idx = int((s / max_val) * (len(blocks) - 1))
                result.append(blocks[idx])

            return "".join(result)

        except Exception as e:
            self._report_error("Sparkline Generation", e)
            return "?"
