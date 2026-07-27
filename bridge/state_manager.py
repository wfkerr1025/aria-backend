# backend/bridge/state_manager.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
import time


@dataclass
class BridgeSession:
    session_id: str
    name: str = "Untitled Session"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Full history
    history: List[Dict[str, Any]] = field(default_factory=list)

    # Last reply from ARIA or Copilot
    last_reply: Optional[Dict[str, Any]] = None

    # Last error from backend or Copilot
    last_error: Optional[str] = None

    # Chat summary support
    summary: Optional[str] = None

    # Arbitrary metadata (plugins, summaries, project info)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BridgeStateManager:
    """
    Unified state manager for the ARIA–Copilot bridge.
    Tracks:
      - active session
      - chat history
      - last reply from Copilot
      - metadata
    """

    def __init__(self) -> None:
        self.sessions: Dict[str, BridgeSession] = {}
        self.active_session_id: Optional[str] = None

    # ---------------------------------------------------------
    # CREATE SESSION
    # ---------------------------------------------------------
    def create_session(self, session_id: str, name: Optional[str] = None) -> BridgeSession:
        if session_id in self.sessions:
            return self.sessions[session_id]

        session = BridgeSession(
            session_id=session_id,
            name=name or "ARIA–Copilot Session",
        )

        self.sessions[session_id] = session
        self.active_session_id = session_id
        return session

    # ---------------------------------------------------------
    # GET SESSION
    # ---------------------------------------------------------
    def get_session(self, session_id: Optional[str] = None) -> Optional[BridgeSession]:
        if session_id is None:
            session_id = self.active_session_id
        if session_id is None:
            return None
        return self.sessions.get(session_id)

    # ---------------------------------------------------------
    # SET ACTIVE SESSION
    # ---------------------------------------------------------
    def set_active_session(self, session_id: str) -> None:
        if session_id in self.sessions:
            self.active_session_id = session_id

    # ---------------------------------------------------------
    # APPEND HISTORY
    # ---------------------------------------------------------
    def append_history(
        self,
        session_id: str,
        role: str,
        content: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:

        session = self.get_session(session_id)
        if session is None:
            session = self.create_session(session_id)

        entry: Dict[str, Any] = {
            "timestamp": time.time(),
            "role": role,
            "content": content,
        }

        if extra:
            entry.update(extra)

        session.history.append(entry)
        session.updated_at = time.time()

    # ---------------------------------------------------------
    # SET LAST REPLY
    # ---------------------------------------------------------
    def set_last_reply(self, session_id: str, reply: Dict[str, Any]) -> None:
        session = self.get_session(session_id)
        if session is None:
            session = self.create_session(session_id)

        session.last_reply = reply
        session.updated_at = time.time()

    # ---------------------------------------------------------
    # SET LAST ERROR
    # ---------------------------------------------------------
    def set_last_error(self, session_id: str, error: str) -> None:
        session = self.get_session(session_id)
        if session is None:
            session = self.create_session(session_id)

        session.last_error = error
        session.updated_at = time.time()

    # ---------------------------------------------------------
    # RENAME SESSION
    # ---------------------------------------------------------
    def rename_session(self, session_id: str, new_name: str) -> None:
        session = self.get_session(session_id)
        if session:
            session.name = new_name
            session.updated_at = time.time()

    # ---------------------------------------------------------
    # SET METADATA
    # ---------------------------------------------------------
    def set_metadata(self, session_id: str, metadata: Dict[str, Any]) -> None:
        session = self.get_session(session_id)
        if session:
            session.metadata.update(metadata)
            session.updated_at = time.time()

    # ---------------------------------------------------------
    # GET STATUS (Copilot Packet)
    # ---------------------------------------------------------
    def get_status(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        session = self.get_session(session_id)

        if session is None:
            return {
                "status": "ok",
                "operation": "bridge_status",
                "active": False,
                "session_id": None,
                "name": None,
                "history_length": 0,
                "last_reply": None,
                "last_error": None,
                "metadata": {},
            }

        return {
            "status": "ok",
            "operation": "bridge_status",
            "active": True,
            "session_id": session.session_id,
            "name": session.name,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "history_length": len(session.history),
            "last_reply": session.last_reply,
            "last_error": session.last_error,
            "summary": session.summary,
            "metadata": session.metadata,
        }


# Singleton instance for the bridge
bridge_state = BridgeStateManager()
