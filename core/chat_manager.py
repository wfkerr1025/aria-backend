import uuid

from core.commands import detect_command, execute_command
from bridge.state_manager import bridge_state
from backend.llm.intent_detector import IntentDetector
from backend.llm.provider_selector import select_provider_with_fallback


class ChatSession:
    """
    Unified chat session model for ARIA Lite.
    """

    def __init__(self, name="New Chat"):
        self.id = str(uuid.uuid4())
        self.name = name
        self.project_path = None
        self.summary = None
        self.history = []

    def add_message(self, role: str, text: str):
        self.history.append({"role": role, "text": text})


class ChatManager:
    """
    Unified ChatManager for ARIA Lite.
    Handles:
      - chat creation
      - chat switching
      - chat summaries
      - command detection
      - streaming pipeline
      - provider fallback
      - bridge state integration
    """

    def __init__(self):
        self.sessions = []
        self.active_session_id = None

        # UI state
        self.aria_status = "Listening"
        self.backend_status = "Offline"

        # Actions (ARIAActions) attached later
        self.actions = None

        # Intent detector
        self.base_intent_detector = IntentDetector()

    # ---------------------------------------------------------
    # Attach ARIAActions
    # ---------------------------------------------------------
    def attach_actions(self, actions):
        self.actions = actions

    # ---------------------------------------------------------
    # Rename Chat
    # ---------------------------------------------------------
    def rename_chat(self, session_id: str, new_name: str):
        for session in self.sessions:
            if session.id == session_id:
                session.name = new_name
                bridge_state.rename_session(session_id, new_name)
                return True
        return False

    # ---------------------------------------------------------
    # Set Summary
    # ---------------------------------------------------------
    def set_summary(self, session_id: str, summary: str):
        for session in self.sessions:
            if session.id == session_id:
                session.summary = summary
                bridge_state.set_metadata(session_id, {"summary": summary})
                return True
        return False

    # ---------------------------------------------------------
    # Create Chat
    # ---------------------------------------------------------
    def create_chat(self, name="New Chat") -> ChatSession:
        session = ChatSession(name)
        self.sessions.append(session)
        self.active_session_id = session.id

        bridge_state.create_session(session_id=session.id, name=session.name)
        return session

    # ---------------------------------------------------------
    # Switch Chat
    # ---------------------------------------------------------
    def switch_to(self, session_id: str) -> ChatSession | None:
        for session in self.sessions:
            if session.id == session_id:
                self.active_session_id = session_id
                bridge_state.set_active_session(session_id)
                return session
        return None

    # ---------------------------------------------------------
    # Get Active Session
    # ---------------------------------------------------------
    def get_active_session(self) -> ChatSession | None:
        for session in self.sessions:
            if session.id == self.active_session_id:
                return session
        return None

    # ---------------------------------------------------------
    # Process Message
    # ---------------------------------------------------------
    def process_message(self, message: str) -> str:
        session = self.get_active_session()
        if not session:
            session = self.create_chat("New Chat")

        # Mirror user message
        session.add_message("user", message)
        bridge_state.append_history(
            session_id=session.id,
            role="user",
            content=message
        )

        # -----------------------------------------------------
        # COMMAND DETECTION
        # -----------------------------------------------------
        command_key = detect_command(message)
        if command_key:
            result = execute_command(command_key, message, session_id=session.id)

            bridge_state.append_history(
                session_id=session.id,
                role="aria",
                content=result.reply
            )

            return result.reply

        # -----------------------------------------------------
        # LLM PIPELINE (Unified)
        # -----------------------------------------------------
        full_text = self.process_llm_message(message)

        # Mirror ARIA reply
        session.add_message("aria", full_text)
        bridge_state.append_history(
            session_id=session.id,
            role="aria",
            content=full_text
        )

        return full_text

    # ---------------------------------------------------------
    # LLM PIPELINE (Unified)
    # ---------------------------------------------------------
    def process_llm_message(self, message: str) -> str:
        """
        Unified LLM pipeline using backend.llm.llm_engine.
        Returns structured LLM output.
        """

        if not self.actions:
            return "[ERROR] ChatManager.actions not attached"

        # Detect intent
        intent = self.base_intent_detector.detect(message)
        print("DETECTED INTENT:", intent)

        # Intent normalization map
        INTENT_MAP = {
            "code": "code_generate",
            "generate_code": "code_generate",
            "coding": "code_generate",
            "write_code": "code_generate",
            "programming": "code_generate",

            "story": "story_create",
            "write_story": "story_create",

            "poem": "poem_create",
            "poetry": "poem_create",

            "song": "song_create",
            "lyrics": "song_create",

            "script": "script_create",
            "write_script": "script_create",

            "reasoning": "deep_reasoning",
            "think": "deep_reasoning",

            "plan": "planning",
            "planning": "planning",

            "summarize": "summarize",
            "summary": "summarize",

            "document": "document",
            "write_document": "document",

            "translate": "translate",
            "translation": "translate",
        }

        # Apply mapping
        intent = INTENT_MAP.get(intent, intent)

        # Call backend LLM engine
        result = self.actions.generate_with_llm(
            prompt=message,
            context={"intent": intent}
        )

        if not isinstance(result, dict):
            return "[ERROR] Invalid LLM response"

        if not result.get("success", False):
            return f"[LLM Error] {result.get('detail', 'Unknown error')}"

        return result.get("content", "")
