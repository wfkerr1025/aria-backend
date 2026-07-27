# backend/module_status.py
from backend.status_registry import StatusRegistry

registry = StatusRegistry()

mark_loaded = registry.mark_loaded
mark_failed = registry.mark_failed
