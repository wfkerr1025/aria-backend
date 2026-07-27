class ARIAInterface:
    """
    Unified interface layer between backend packet handlers and the ARIAWindow UI.
    Backend handlers call methods here instead of touching UI directly.
    """

    def __init__(self, aria_window):
        self.aria = aria_window

    # ---------------------------------------------------------
    # FILE OPERATIONS
    # ---------------------------------------------------------
    def write_file(self, path, content):
        """Write text content to a file."""
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"status": "ok", "path": path}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def read_file(self, path):
        """Read text content from a file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {"status": "ok", "content": f.read()}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def exists_file(self, path):
        """Check if a file exists."""
        import os
        return {"status": "ok", "exists": os.path.exists(path)}

    # ---------------------------------------------------------
    # UI OPERATIONS
    # ---------------------------------------------------------
    def send_message_to_chat(self, text):
        """Send a message to the ARIA chat panel."""
        try:
            self.aria.chat_panel.add_aria_message(text)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def open_file_in_editor(self, path):
        """Open a file in ARIA's editor (future expansion)."""
        try:
            self.aria.chat_panel.add_aria_message(f"[Open File] {path}")
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ---------------------------------------------------------
    # PROGRAM / PLUGIN EXECUTION
    # ---------------------------------------------------------
    def execute_program(self, program_name, args=None):
        """Execute a plugin program by name."""
        for plugin in self.aria.plugins:
            manifest = getattr(plugin, "manifest", {})
            if manifest.get("name") == program_name:
                try:
                    return plugin.run(args or {})
                except Exception as e:
                    return {"status": "error", "message": str(e)}
        return {"status": "error", "message": f"Program '{program_name}' not found"}

    # ---------------------------------------------------------
    # TASK EXECUTION
    # ---------------------------------------------------------
    def run_task(self, task_id, args=None):
        """Run a task through the TaskConsole."""
        if self.aria.task_console:
            try:
                return self.aria.task_console.run_task(task_id, args or {})
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "TaskConsole not available"}

    # ---------------------------------------------------------
    # PACKET EXECUTION
    # ---------------------------------------------------------
    def execute_packet(self, envelope):
        """
        Execute a packet through ARIAWindow's process_envelope().
        Backend handlers call this when they need ARIA to run a packet.
        """
        try:
            return self.aria.process_envelope(envelope)
        except Exception as e:
            return {"status": "error", "message": str(e)}
