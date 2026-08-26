"""ARIA Lite - watching for processes that compete for the machine.

Answers one question: is something else running that ARIA should leave room
for? Today the only such thing is the Unity editor, which is why the check
used to sit inline in resource_monitor with "Unity" written into it.

It is here instead because that is domain knowledge, and resource_monitor is
where the machine is measured. A CPU reading is true of any machine; "Unity
counts as a heavy neighbour" is a fact about what this assistant is for, and
the next such fact (Blender, Unreal, a game build) belongs beside it rather
than as another `if` in a hardware probe.

Which is why the check is a name list rather than a function per program.
Adding Unreal is a string; it does not touch resource_monitor, safety
policy, or anything that reads a snapshot.

What deliberately did not move is ResourceSnapshot.unity_running. That field
is read by safety_manager and safety_profiles and published over both the
IPC and REST contracts, so renaming it would be a breaking change to
something outside this codebase's control. The detection moved; the name it
reports under stayed.
"""

from __future__ import annotations

import psutil

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["HEAVY_NEIGHBOURS", "detect_running", "unity_running"]

# Processes worth leaving headroom for, as case-insensitive substrings of
# either the executable name or the first command-line argument. Matched
# loosely on purpose: the editor ships as "Unity.exe", "Unity Hub.exe" and
# a versioned path depending on how it was launched.
HEAVY_NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "unity": ("unity",),
}


def detect_running(needles) -> bool:
    """Whether any running process matches one of `needles`.

    Every per-process read is guarded individually. Iterating processes on a
    live machine races against them exiting, and a permission error on one
    system process is not a reason to report the machine as idle -- so a
    failure to inspect one process skips that process and nothing else.
    """
    wanted = tuple(needle.lower() for needle in needles if needle)
    if not wanted:
        return False

    for process in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            name = (process.info.get("name") or "").lower()
            cmdline = process.info.get("cmdline") or []
            first = (cmdline[0] if cmdline else "").lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except Exception:  # pragma: no cover - psutil raises OS-specific errors
            continue

        if any(needle in name or needle in first for needle in wanted):
            return True
    return False


def unity_running() -> bool:
    """Whether the Unity editor appears to be running."""
    found = detect_running(HEAVY_NEIGHBOURS["unity"])
    if found:
        logger.debug("Unity detected running")
    return found
