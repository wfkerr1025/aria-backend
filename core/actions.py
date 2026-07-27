import requests


class ARIAActions:
    """
    Unified backend communication + streaming for ARIA Lite.
    """

    def __init__(self, chat_manager=None):
        self.chat_manager = chat_manager

        self.backend_base_url = "http://127.0.0.1:5000"
        self.stream_url = f"{self.backend_base_url}/stream"
        self.ping_url = f"{self.backend_base_url}/ping"

    # ---------------------------------------------------------
    # Backend Ping
    # ---------------------------------------------------------
    def ping_backend(self):
        """
        Returns True if backend is reachable.
        Called at ARIA Lite startup.
        """
        try:
            resp = requests.get(self.ping_url, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    # ---------------------------------------------------------
    # Streaming
    # ---------------------------------------------------------
    def stream_response_with_provider(self, prompt, provider, model, on_chunk, on_finish):
        """
        Chunk‑by‑chunk streaming from backend.
        on_chunk: callback(text_so_far)
        on_finish: callback(full_text)
        """

        cm = self.chat_manager
        if cm is None:
            on_finish("[ERROR] ChatManager not attached to ARIAActions")
            return

        # Unified UI state
        cm.aria_status = "Thinking…"
        cm.backend_status = "Offline"

        try:
            payload = {
                "prompt": prompt,
                "provider": provider,
                "model": model,
            }

            with requests.post(
                self.stream_url,
                json=payload,
                stream=True,
                timeout=120
            ) as resp:
                resp.raise_for_status()

                cm.backend_status = "Online"
                cm.aria_status = "Writing…"

                full_text = ""

                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue

                    decoded = raw_line.decode("utf-8").strip()

                    # Ignore terminators
                    if decoded in ("[DONE]", "done", "done done"):
                        continue

                    full_text += decoded
                    on_chunk(full_text)

                cm.aria_status = "Listening"
                on_finish(full_text)

        except Exception as e:
            cm.backend_status = "Offline"
            cm.aria_status = "Listening"
            on_finish(f"[Backend Offline] {str(e)}")


    # ---------------------------------------------------------
    # New Chat (Unified UI Action)
    # ---------------------------------------------------------
    def new_chat(self):
        """
        Creates a new chat session via ChatManager.
        Unified entrypoint used by Sidebar + MenuBar.
        """
        if self.chat_manager is None:
            return

        # ChatManager already has a unified session creation API
        self.chat_manager.start_new_session()

    # ---------------------------------------------------------
    # LLM GENERATION (Unified)
    # ---------------------------------------------------------
    def generate_with_llm(self, prompt: str, context: dict | None = None):
        """
        Sends an LLM generation request to the backend.
        Returns structured LLM output from backend.llm.llm_engine.
        """

        payload = {
            "task": "llm_generate",
            "prompt": prompt,
            "context": context or {}
        }

        print("PAYLOAD SENT TO BACKEND:", payload)   # <‑‑ DEBUG PRINT

        try:
            resp = requests.post(
                f"{self.backend_base_url}/command",
                json=payload,
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            return {
                "status": "error",
                "operation": "llm_generate",
                "detail": str(e)
            }
