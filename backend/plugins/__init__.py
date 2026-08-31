"""ARIA Lite - the brief plugins that ship with it.

A plugin says how the global brief's properties are satisfied in one
world. This package holds the ones ARIA knows out of the box; a project
can register its own with brief_plugins.register_plugin at any time.

LOADED ONCE, AND NEVER BEHIND A TEST'S BACK
-------------------------------------------
load_builtins() is called from active_plugin_sections the first time a
brief is built, so the running app has its plugins without an explicit
startup step. clear_plugins() marks them as handled, so a test that
clears the registry stays cleared -- otherwise every test of the GLOBAL
brief would silently get Unity's section back and would be testing
something other than what it says it is.
"""

from __future__ import annotations

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["load_builtins"]


def load_builtins() -> list:
    """Register every plugin that ships with ARIA. Returns their names.

    A plugin that fails to import costs itself and not the others: a
    broken plugin should degrade ARIA to the global brief, which is
    still correct, rather than take the turn down.
    """
    from backend.plugins import unity_csharp

    loaded = []
    for module in (unity_csharp,):
        try:
            module.register()
            loaded.append(module.NAME)
        except Exception:  # pragma: no cover - a plugin is not worth a turn
            logger.exception("could not register brief plugin from %s",
                             getattr(module, "__name__", module))
    return loaded
