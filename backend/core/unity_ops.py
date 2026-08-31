"""ARIA Lite - driving the Unity Editor from Python.

The other half of Assets/ARIA/ARIAEditorBridge.cs. The bridge exposes ten
operations inside the editor; this runs the editor and reads what they
said.

HOW A CALL ACTUALLY WORKS
-------------------------
Unity's -executeMethod takes a PARAMETERLESS static method and has no way
to return a value. So the bridge's CreateGameObject(string) cannot be
invoked that way, and this does not pretend otherwise: every call goes to
one entry point, ARIA.ARIAEditorBridge.RunFromCommandLine, which reads
the method name and its arguments out of -ariaArgs, dispatches inside the
editor, and prints its result between two sentinels.

    python                unity -batchmode -executeMethod ...
      |                     |
      |  -ariaArgs={json}   |
      |-------------------->|  RunFromCommandLine reads it,
      |                     |  dispatches, prints
      |   <<<ARIA:...>>>    |
      |<--------------------|
      |  parsed from stdout |

The sentinels are what make stdout usable. Unity's batchmode output is
licence checks, asset imports, compiler messages and shader warnings;
the answer is a few dozen characters somewhere in the middle of it.
Searching for "the JSON" would find the first brace in a log line.

WHAT THIS DELIBERATELY IS NOT
-----------------------------
It is not a way to run arbitrary programs. The executable comes from
configuration, never from a caller; the method name must be one of ten;
the arguments are JSON-encoded into a single argv entry rather than
interpolated into a command line; and there is no shell anywhere. That
is the same shape file_tools.run_tests uses, and it is deliberate: this
module is the one place in ARIA that starts an external process on the
user's machine.

It is also not registered as a tool. Nothing here is in
action_plan.ACTION_TOOLS, so a model cannot propose a Unity operation and
have it run. Wiring that up is a separate decision with its own consent
question, and it is not made here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "UNITY_COMMANDS",
    "UnityUnavailable",
    "editor_path",
    "project_path",
    "run_unity_command",
    "validate_asset_path",
    # The ten operations, in the order the bridge declares them.
    "create_game_object",
    "add_component",
    "create_prefab",
    "create_scriptable_object",
    "load_scene",
    "save_scene",
    "run_build",
    "import_asset",
    "set_serialized_field",
    "get_scene_summary",
]

# ======================================================
# Configuration
# ======================================================

# Where Unity is. Set this and everything else follows.
ENV_EDITOR = "ARIA_UNITY_PATH"
# Which project to open. Defaults to the file tools' workspace.
ENV_PROJECT = "ARIA_UNITY_PROJECT"
# How long one editor run may take before it is killed.
ENV_TIMEOUT = "ARIA_UNITY_TIMEOUT"

# The spec's number. It is enough for an operation in an already-imported
# project and is NOT enough for a cold open that recompiles everything --
# which is why it is configurable and why a timeout says so in as many
# words rather than reporting a failure.
DEFAULT_TIMEOUT_SECONDS = 120

# Unity prints a great deal. The bridge wraps its answer in these so it
# can be found without guessing which brace was the real one.
RESULT_OPEN = "<<<ARIA_RESULT>>>"
RESULT_CLOSE = "<<<ARIA_END>>>"
_RESULT = re.compile(re.escape(RESULT_OPEN) + r"(.*?)" + re.escape(RESULT_CLOSE),
                     re.DOTALL)

# The one method Unity is ever told to execute. Everything else is data.
ENTRY_POINT = "ARIA.ARIAEditorBridge.RunFromCommandLine"

# A transient failure is worth one more go; a rejected argument is not.
# Unity fails to start when another editor holds the project lock, which
# clears on its own, and that is the case this exists for.
MAX_ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 3.0

# Everything the bridge will dispatch. A method not on this list is
# refused here, before a process starts.
BRIDGE_METHODS = frozenset({
    "CreateGameObject",
    "AddComponent",
    "CreatePrefab",
    "CreateScriptableObject",
    "LoadScene",
    "SaveScene",
    "RunBuild",
    "ImportAsset",
    "SetSerializedField",
    "GetSceneSummary",
})


class UnityUnavailable(RuntimeError):
    """Unity is not installed, or not where configuration says it is."""


# ======================================================
# Where things are
# ======================================================

def _plugin_setting(plugin_id: str, field: str) -> str:
    """A value from the plugins registry, or "" when there is not one.

    Imported here rather than at module scope so unity_ops stays usable
    on an install with no registry at all, and so a fault in the plugin
    system costs a fallback rather than the module.
    """
    try:
        from backend.plugins import plugin_settings

        return plugin_settings.configured_path(plugin_id, field)
    except Exception:  # pragma: no cover - configuration is not worth a crash
        logger.exception("could not read the %s plugin's %s", plugin_id, field)
        return ""


def _hub_candidates() -> list:
    """Editors a Unity Hub install would have put on this machine."""
    roots = [
        Path(r"C:\Program Files\Unity\Hub\Editor"),
        Path(r"C:\Program Files (x86)\Unity\Hub\Editor"),
        Path("/Applications/Unity/Hub/Editor"),
        Path.home() / "Unity" / "Hub" / "Editor",
    ]

    found = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            # Newest version first, so a machine with several editors
            # gets the current one rather than an alphabetical accident.
            for version in sorted(root.iterdir(), reverse=True):
                for relative in ("Editor/Unity.exe", "Unity.app/Contents/MacOS/Unity",
                                 "Editor/Unity"):
                    candidate = version / relative
                    if candidate.is_file():
                        found.append(candidate)
        except OSError:  # pragma: no cover - an unreadable root is not fatal
            logger.debug("could not read %s while looking for Unity", root)
    return found


def editor_path() -> Path:
    """The Unity executable to run.

    Configuration first, then a Unity Hub install, then whatever is on
    PATH. Raises rather than guessing when there is nothing: a caller
    that gets a wrong path spends two minutes finding out, and one that
    gets an exception knows immediately.
    """
    configured = os.environ.get(ENV_EDITOR)
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise UnityUnavailable(
                f"{ENV_EDITOR} points at {configured!r}, which is not a file."
            )
        return path

    # Then the Unity plugin's own setting. This is what makes the Unity
    # config page a page rather than a form that saves a string nobody
    # reads: the path a user types there is the executable ARIA runs.
    from_plugin = _plugin_setting("unity", "unity_path")
    if from_plugin:
        path = Path(from_plugin)
        if not path.is_file():
            raise UnityUnavailable(
                f"The Unity plugin is configured with {from_plugin!r}, "
                f"which is not a file."
            )
        return path

    for candidate in _hub_candidates():
        return candidate

    on_path = shutil.which("Unity") or shutil.which("unity")
    if on_path:
        return Path(on_path)

    raise UnityUnavailable(
        f"Could not find Unity. Set {ENV_EDITOR} to the editor executable."
    )


def project_path() -> Path:
    """The Unity project to open.

    Defaults to the workspace the file tools are confined to, so ARIA
    operates on the project the rest of the session is about.
    """
    configured = os.environ.get(ENV_PROJECT)
    if configured:
        return Path(configured).resolve()

    from_plugin = _plugin_setting("unity", "project_path")
    if from_plugin:
        return Path(from_plugin).resolve()

    try:
        from backend.core import file_tools

        return file_tools.workspace_root()
    except Exception:  # pragma: no cover - a missing workspace is not fatal here
        logger.exception("could not read the workspace root; using the cwd")
        return Path.cwd().resolve()


def _timeout() -> int:
    raw = os.environ.get(ENV_TIMEOUT)
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        logger.warning("%s=%r is not a number; using %d",
                       ENV_TIMEOUT, raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS


# ======================================================
# Results
# ======================================================

def _ok(result) -> dict:
    return {"ok": True, "result": result, "error": None}


def _error(message: str, details=None) -> dict:
    """A failure the caller can act on, and a log line for a human.

    Always the same shape as a success. A caller that has to tell the two
    apart by which keys are present will eventually get it wrong.
    """
    logger.warning("unity: %s", message)
    return {"ok": False, "result": None, "error": message,
            "details": details if details is None else str(details)[:4000]}


# ======================================================
# Paths
# ======================================================

def validate_asset_path(path, *, suffix: str = "") -> str | None:
    """Why `path` is not a usable asset path, or None if it is.

    Returns the REASON rather than a boolean, so the caller can say what
    was wrong instead of "invalid". The rules are the bridge's own, kept
    here as well on purpose: the C# side refuses these too, and a check
    that exists only across a process boundary is a check that runs two
    minutes later than it needed to.
    """
    if not isinstance(path, str) or not path.strip():
        return "an asset path is required"

    if path != path.strip():
        return "an asset path must not begin or end with whitespace"
    if "\\" in path:
        return "use forward slashes in asset paths"
    if ".." in path:
        return "an asset path must not contain '..'"
    if path.endswith("/"):
        return "an asset path must not end with '/'"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "an asset path must be relative to the project, not absolute"
    if path != "Assets" and not path.startswith("Assets/"):
        return "an asset path must be inside Assets/"
    if suffix and not path.lower().endswith(suffix.lower()):
        return f"this path must end with {suffix}"

    return None


def _require_text(value, name: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"{name} is required"
    return None


# ======================================================
# Talking to the editor
# ======================================================

def _extract_result(stdout: str):
    """The bridge's JSON, out of everything else Unity printed.

    Returns the parsed object, or None when the sentinels are absent or
    what is between them is not JSON.
    """
    match = _RESULT.search(stdout or "")
    if not match:
        return None

    body = match.group(1).strip()
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        logger.warning("unity printed something between the sentinels that is "
                       "not JSON: %r", body[:400])
        return None


def _invoke(method: str, arguments: dict) -> dict:
    """Run one bridge method and return its structured result."""
    if method not in BRIDGE_METHODS:
        return _error(f"{method!r} is not a Unity bridge method")

    try:
        unity = editor_path()
    except UnityUnavailable as unavailable:
        return _error(str(unavailable))

    project = project_path()
    if not (project / "Assets").is_dir():
        return _error(f"{project} does not look like a Unity project "
                      f"(no Assets folder).")

    payload = json.dumps({"method": method, "args": arguments},
                         separators=(",", ":"))

    command = [
        str(unity),
        "-batchmode",
        "-quit",
        "-nographics",
        "-projectPath", str(project),
        "-executeMethod", ENTRY_POINT,
        f"-ariaArgs={payload}",
    ]

    logger.info("unity: %s %s", method, arguments)

    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_timeout(),
                shell=False,
                cwd=str(project),
            )
        except FileNotFoundError:
            return _error(f"Could not run {unity}.")
        except subprocess.TimeoutExpired:
            # Said plainly, because the usual cause is a cold open that
            # recompiles the project rather than anything being wrong.
            last = _error(
                f"Unity did not finish within {_timeout()}s. A first run "
                f"that recompiles the project often needs longer; raise "
                f"{ENV_TIMEOUT}.")
            break
        except OSError as error:  # pragma: no cover - platform-level failure
            last = _error(f"Could not start Unity: {error}")
            break

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        logger.debug("unity stdout (%d bytes), stderr (%d bytes), exit %s",
                     len(stdout), len(stderr), completed.returncode)

        parsed = _extract_result(stdout)

        if parsed is None:
            # No sentinels at all. Either the bridge is not in the
            # project, or Unity fell over before reaching it.
            if not stdout.strip() and not stderr.strip():
                last = _error("Unity returned no output", stderr or stdout)
            elif RESULT_OPEN in stdout:
                last = _error("Unity's result was not valid JSON", stdout[-4000:])
            else:
                last = _error(
                    "Unity produced no ARIA result. Is Assets/ARIA/"
                    "ARIAEditorBridge.cs in this project and compiling?",
                    (stderr or stdout)[-4000:])
        elif not isinstance(parsed, dict):
            last = _error("Unity's result was not an object", stdout[-4000:])
        elif parsed.get("ok"):
            return _ok(parsed.get("value", ""))
        else:
            # The bridge ran and said no. That is an answer, not a
            # transient fault, so it is returned rather than retried.
            return _error(parsed.get("error") or f"{method} failed",
                          stderr[-4000:] or None)

        # Only the shapes above are worth a second attempt: a project
        # lock clears, a rejected argument does not.
        if attempt < MAX_ATTEMPTS:
            logger.info("unity: attempt %d failed; retrying in %.0fs",
                        attempt, RETRY_PAUSE_SECONDS)
            time.sleep(RETRY_PAUSE_SECONDS)

    return last or _error(f"{method} produced no result")


# ======================================================
# The ten operations
# ======================================================

def create_game_object(name: str) -> dict:
    """Create an empty GameObject in the open scene."""
    problem = _require_text(name, "name")
    if problem:
        return _error(problem)
    return _invoke("CreateGameObject", {"name": name})


def add_component(game_object_name: str, component_type: str) -> dict:
    """Add a component to a GameObject in the open scene."""
    problem = (_require_text(game_object_name, "game_object_name")
               or _require_text(component_type, "component_type"))
    if problem:
        return _error(problem)
    return _invoke("AddComponent", {"gameObjectName": game_object_name,
                                    "componentType": component_type})


def create_prefab(prefab_path: str, game_object_name: str) -> dict:
    """Save a scene GameObject as a prefab asset."""
    problem = (validate_asset_path(prefab_path, suffix=".prefab")
               or _require_text(game_object_name, "game_object_name"))
    if problem:
        return _error(problem)
    return _invoke("CreatePrefab", {"prefabPath": prefab_path,
                                    "gameObjectName": game_object_name})


def create_scriptable_object(type_name: str, asset_path: str) -> dict:
    """Create a ScriptableObject asset of a named type."""
    problem = (_require_text(type_name, "type_name")
               or validate_asset_path(asset_path, suffix=".asset"))
    if problem:
        return _error(problem)
    return _invoke("CreateScriptableObject", {"typeName": type_name,
                                              "assetPath": asset_path})


def load_scene(scene_path: str) -> dict:
    """Open a scene, saving the current one first if the user agrees."""
    problem = validate_asset_path(scene_path, suffix=".unity")
    if problem:
        return _error(problem)
    return _invoke("LoadScene", {"scenePath": scene_path})


def save_scene() -> dict:
    """Save the currently open scene."""
    return _invoke("SaveScene", {})


def run_build(build_path: str) -> dict:
    """Build the project's enabled scenes to a path.

    The build output is NOT an asset, so it is not held to the Assets/
    rule -- a build that could only be written inside the project would
    be useless. Traversal is still refused.
    """
    problem = _require_text(build_path, "build_path")
    if problem:
        return _error(problem)
    if ".." in build_path:
        return _error("a build path must not contain '..'")
    return _invoke("RunBuild", {"buildPath": build_path})


def import_asset(asset_path: str) -> dict:
    """Import or re-import an asset already on disk."""
    problem = validate_asset_path(asset_path)
    if problem:
        return _error(problem)
    return _invoke("ImportAsset", {"assetPath": asset_path})


def set_serialized_field(game_object_name: str, component_type: str,
                         field_name: str, value: str) -> dict:
    """Set one serialized field on a component, by name."""
    problem = (_require_text(game_object_name, "game_object_name")
               or _require_text(component_type, "component_type")
               or _require_text(field_name, "field_name"))
    if problem:
        return _error(problem)
    # value may legitimately be "" -- clearing a string field -- so it is
    # required to be a string and not required to be non-empty.
    if not isinstance(value, str):
        return _error("value must be a string")

    return _invoke("SetSerializedField", {"gameObjectName": game_object_name,
                                          "componentType": component_type,
                                          "fieldName": field_name,
                                          "value": value})


def get_scene_summary() -> dict:
    """Describe the open scene's hierarchy.

    The bridge returns its summary as a JSON string inside the result, so
    it is parsed here rather than handed to the caller as text to parse
    again.
    """
    outcome = _invoke("GetSceneSummary", {})
    if not outcome.get("ok"):
        return outcome

    raw = outcome.get("result") or ""
    try:
        return _ok(json.loads(raw) if isinstance(raw, str) and raw else raw)
    except (ValueError, TypeError):
        return _error("Unity's scene summary was not valid JSON", raw)


# ======================================================
# The routing surface
# ======================================================

UNITY_COMMANDS = {
    "unity.create_game_object": create_game_object,
    "unity.add_component": add_component,
    "unity.create_prefab": create_prefab,
    "unity.create_scriptable_object": create_scriptable_object,
    "unity.load_scene": load_scene,
    "unity.save_scene": save_scene,
    "unity.run_build": run_build,
    "unity.import_asset": import_asset,
    "unity.set_serialized_field": set_serialized_field,
    "unity.get_scene_summary": get_scene_summary,
}


def run_unity_command(command_name: str, **kwargs) -> dict:
    """Run one named Unity command and return a structured result.

    The single entry point ARIA's routing layer calls. An unknown name,
    a missing argument or an argument that is not accepted all come back
    in the same shape as a success -- nothing here raises, because a
    caller across a routing boundary cannot catch usefully.
    """
    handler = UNITY_COMMANDS.get(command_name)
    if handler is None:
        return _error(f"{command_name!r} is not a Unity command",
                      "known: " + ", ".join(sorted(UNITY_COMMANDS)))

    logger.info("unity command: %s(%s)", command_name,
                ", ".join(f"{key}={value!r}" for key, value in kwargs.items()))

    try:
        return handler(**kwargs)
    except TypeError as error:
        # Wrong or missing keyword arguments, which is a caller mistake
        # and should read like one rather than like a Unity failure.
        return _error(f"{command_name} was called with the wrong arguments: {error}")
    except Exception as error:  # pragma: no cover - a bug here is not the caller's
        logger.exception("unity command %s failed", command_name)
        return _error(f"{command_name} failed: {error}")
