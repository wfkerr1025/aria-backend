# backend/diagnostics/startup_toast.py
import tkinter as tk
from tkinter import ttk
import traceback
import time
import os


TOAST_DURATION_MS = 6000


def show_startup_toast(root, result, theme, on_view_details):
    """
    Unified startup toast:
    - Appears bottom-right of the main window
    - Shows warnings/errors from StartupResult
    - Uses unified theme styling
    - Auto-destroys after TOAST_DURATION_MS
    """

    # No toast for clean startup
    if not result.has_issues():
        return

    try:
        # Create toast window
        toast = tk.Toplevel(root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)

        # Ensure geometry is up-to-date
        root.update_idletasks()

        # Position bottom-right of root window
        rx = root.winfo_x()
        ry = root.winfo_y()
        rw = root.winfo_width()
        rh = root.winfo_height()

        width = 320
        height = 120

        x = rx + rw - width - 20
        y = ry + rh - height - 40

        toast.geometry(f"{width}x{height}+{x}+{y}")

        # Frame
        frame = ttk.Frame(toast, padding=10)
        frame.pack(fill="both", expand=True)

        # Title
        if result.errors_count() > 0:
            title = "Startup Errors Detected"
        else:
            title = "Startup Warnings Detected"

        title_label = ttk.Label(
            frame,
            text=title,
            font=theme.get("font_header")
        )
        title_label.pack(anchor="w")

        # Summary
        summary_label = ttk.Label(
            frame,
            text=f"Warnings: {result.warnings_count()}   Errors: {result.errors_count()}",
            font=theme.get("font_default")
        )
        summary_label.pack(anchor="w", pady=(4, 8))

        # Button
        btn = ttk.Button(
            frame,
            text="View Details",
            command=lambda: _on_view(toast, on_view_details)
        )
        btn.pack(anchor="e")

        # Clicking anywhere also opens details
        frame.bind("<Button-1>", lambda e: _on_view(toast, on_view_details))

        # Auto-destroy
        toast.after(TOAST_DURATION_MS, toast.destroy)

    except Exception as e:
        _report_error("show_startup_toast", e)


def _on_view(toast, callback):
    """Destroy toast and invoke callback safely."""
    try:
        toast.destroy()
    except Exception:
        pass

    try:
        callback()
    except Exception as e:
        _report_error("startup_toast callback", e)


def _report_error(label, exception):
    """Unified error logging for toast failures."""
    tb = traceback.format_exc()

    print("\n" + "=" * 60)
    print(f"[StartupToast ERROR] Action: {label}")
    print(f"[StartupToast ERROR] Exception: {exception}")
    print(tb)
    print("=" * 60 + "\n")

    os.makedirs("logs", exist_ok=True)
    with open("logs/startup_toast_errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.ctime()}] ERROR in '{label}': {exception}\n")
        f.write(tb + "\n")
