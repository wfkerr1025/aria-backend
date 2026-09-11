"""ARIA Lite - the Python half of the Unity Editor Bridge.

Assets/ARIA/Editor/ARIAEditorBridge.cs runs inside an OPEN Unity Editor. It
executes the JSON commands it finds in <UnityProject>/ARIA/unity_commands.json
and writes what happened to <UnityProject>/ARIA/unity_results.json. This
module writes the one file and reads the other.

    python                                  Unity Editor (open, bridge installed)
      |                                        |
      |  write ARIA/unity_commands.json        |
      |--------------------------------------->|  the watcher (or ARIA > Run
      |                                        |  Bridge Commands) runs them and
      |  poll  ARIA/unity_results.json         |  writes ARIA/unity_results.json
      |<---------------------------------------|
      |  parse; return data, or raise          |

Files rather than a socket, on purpose: they survive a domain reload, need
no port, and a person can read both halves of every exchange afterwards.

WHAT THIS IS NOT
----------------
It is not backend/core/unity_ops.py, which starts a fresh batch-mode editor
for every call through -executeMethod. That is the tool when no editor is
open. This is the tool when one is: the user watches each change land in
the hierarchy as it happens, with undo. send_headless() joins the two: the
same files, but an editor started to process them.

OBJECT REFERENCES
-----------------
Anywhere a command takes a target, pass one of:
    "Root/Child"                           hierarchy path in any loaded scene
    "Assets/Scenes/Main.unity::Root/Child" the same, in one named scene
    "Player"                               a bare name, searched everywhere
    "id:12345"                             instance id (one editor session)
    "gid:GlobalObjectId_V1-2-..."          global id, stable across sessions
CreateGameObject returns all of these in its data, so a caller can keep
whichever one it needs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

__all__ = [
    "BRIDGE_SOURCE",
    "BRIDGE_INSTALL_PATH",
    "COMMANDS",
    "PLAY_SAFE_COMMANDS",
    "Batch",
    "BridgeResults",
    "CommandResult",
    "UnityBridgeError",
    "UnityBridgeTimeout",
    "UnityBridgeUnavailable",
    "UnityEditorBridge",
    "UnroutableCommand",
    "configure",
    "find_project_root",
    "get_bridge",
    "make_command",
    "parse_unity_command",
    "run_unity_command",
    # High-level operations on the default bridge.
    "ping",
    "create_game_object",
    "delete_game_object",
    "add_component",
    "remove_component",
    "set_transform",
    "set_field",
    "get_field",
    "get_hierarchy",
    "open_scene",
    "save_scene",
    "create_prefab",
    "modify_prefab",
    "instantiate_prefab",
    "create_light",
    "create_camera",
    "refresh_assets",
    "screenshot",
    "set_play_mode",
    "last_play",
    "Inputs",
    "send_input",
    "read_screen",
    "get_log",
    "screenshot_burst",
    "set_time",
    "step_frames",
    "run_console",
    "install_bridge",
]

# ======================================================
# Where things are
# ======================================================

FOLDER_NAME = "ARIA"
COMMANDS_FILE = "unity_commands.json"
RESULTS_FILE = "unity_results.json"
# What the bridge writes when a play session it started ends: whether the
# game's real save folder came through untouched. See last_play().
LAST_PLAY_FILE = "last_play.json"
# The variable a bridge-started game reads to find its test save folder.
# Mirrors TestSaveVariable in the C#; a test holds the two together.
TEST_SAVE_VARIABLE = "ARIA_TEST_SAVE_DIR"

# Which Unity project to talk to. Then the Unity plugin's setting, then
# the file tools' workspace, then the current directory.
ENV_PROJECT = "ARIA_UNITY_PROJECT"
# How long to wait for the editor to answer, in seconds.
ENV_TIMEOUT = "ARIA_UNITY_BRIDGE_TIMEOUT"
# Where Unity.exe is, for send_headless only.
ENV_EDITOR = "ARIA_UNITY_PATH"

DEFAULT_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.25
HEADLESS_TIMEOUT_SECONDS = 600.0

# The parameterless static method a batch-mode editor is told to run.
ENTRY_POINT = "ARIA.Bridge.ARIAEditorBridge.RunPendingCommands"

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_SOURCE = REPO_ROOT / "Assets" / "ARIA" / "Editor" / "ARIAEditorBridge.cs"
BRIDGE_INSTALL_PATH = Path("Assets", "ARIA", "Editor", "ARIAEditorBridge.cs")

# Everything the C# side dispatches. Kept here too so a typo is caught
# before a file is written and a minute spent waiting for it.
COMMANDS = frozenset({
    "Ping",
    "CreateGameObject",
    "DeleteGameObject",
    "AddComponent",
    "RemoveComponent",
    "SetTransform",
    "SetField",
    "GetField",
    "GetHierarchy",
    "OpenScene",
    "SaveScene",
    "CreatePrefab",
    "ModifyPrefab",
    "InstantiatePrefab",
    "CreateLight",
    "CreateCamera",
    "RefreshAssets",
    "Screenshot",
    "SetPlayMode",
    "SendInput",
    "ReadScreen",
    "GetLog",
    "SetTime",
    "RunConsole",
})

# What the editor will run while the game is PLAYING. Anything else is left
# waiting by the watcher until the game stops, because an edit made in play
# mode is thrown away when play ends and a bridge that made it anyway would
# report success for work about to vanish. Mirrors PlaySafeCommands in the
# C#; a test holds the two together.
PLAY_SAFE_COMMANDS = frozenset({
    "Ping",
    "Screenshot",
    "GetField",
    "GetHierarchy",
    "SetPlayMode",
    "SendInput",
    "ReadScreen",
    "GetLog",
    "SetTime",
    "RunConsole",
})


class UnityBridgeError(RuntimeError):
    """A command the editor ran and refused, or a batch it could not read."""

    def __init__(self, message: str, *, command: str | None = None,
                 index: int | None = None, results: Sequence["CommandResult"] = ()):
        super().__init__(message)
        self.command = command
        self.index = index
        self.results = list(results)


class UnityBridgeTimeout(UnityBridgeError):
    """No results arrived in time. Usually: no editor is open, or the bridge is not in it."""


class UnityBridgeUnavailable(UnityBridgeError):
    """There is no Unity project to talk to where configuration says."""


class UnroutableCommand(ValueError):
    """run_unity_command could not map a description to any bridge command."""


def find_project_root(explicit: str | os.PathLike | None = None) -> Path:
    """The Unity project folder: the one that contains Assets/.

    Explicit argument, then ARIA_UNITY_PROJECT, then whatever
    backend.core.unity_ops resolves (the plugin's setting or the workspace),
    then the current directory. A folder that holds exactly one Unity
    project resolves to that project, since the workspace is often the
    folder the projects live in. Raises rather than guessing when the
    result is neither: a bridge pointed at the wrong place would wait a
    minute to say nothing.
    """
    candidate: Path | None = None

    if explicit:
        candidate = Path(explicit)
    elif os.environ.get(ENV_PROJECT):
        candidate = Path(os.environ[ENV_PROJECT])
    else:
        try:
            from backend.core import unity_ops

            candidate = unity_ops.project_path()
        except Exception:  # pragma: no cover - the backend is optional here
            candidate = Path.cwd()

    root = candidate.expanduser().resolve()
    if (root / "Assets").is_dir():
        return root

    inside = _projects_inside(root)
    if len(inside) == 1:
        return inside[0]
    if inside:
        names = ", ".join(repr(project.name) for project in inside)
        raise UnityBridgeUnavailable(
            f"{root} holds several Unity projects ({names}). Pass project_root "
            f"or set {ENV_PROJECT} to the one you mean.")
    raise UnityBridgeUnavailable(
        f"{root} is not a Unity project (no Assets folder). Pass project_root "
        f"or set {ENV_PROJECT}.")


def _projects_inside(root: Path) -> list[Path]:
    """The Unity projects one level down, in name order; nothing if root cannot be read."""
    try:
        children = sorted(child for child in root.iterdir() if child.is_dir())
    except OSError:
        return []
    return [child for child in children if (child / "Assets").is_dir()]


def _default_timeout() -> float:
    raw = os.environ.get(ENV_TIMEOUT)
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


# ======================================================
# Results
# ======================================================

@dataclass
class CommandResult:
    """What one command produced, as the bridge reported it."""

    command: str
    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "CommandResult":
        data = raw.get("data")
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            data = {"value": data}
        return cls(
            command=str(raw.get("command") or ""),
            success=bool(raw.get("success")),
            message=str(raw.get("message") or ""),
            data=data,
        )


@dataclass
class BridgeResults:
    """One results file: the envelope and every command's answer."""

    id: str | None
    ok: bool
    message: str
    results: list[CommandResult]
    unity_version: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "BridgeResults":
        items = raw.get("results")
        if not isinstance(items, list):
            items = []
        return cls(
            id=raw.get("id"),
            ok=bool(raw.get("ok")),
            message=str(raw.get("message") or ""),
            results=[CommandResult.from_dict(item) for item in items if isinstance(item, dict)],
            unity_version=str(raw.get("unityVersion") or ""),
            raw=raw,
        )

    def first_failure(self) -> tuple[int, CommandResult] | None:
        for index, result in enumerate(self.results):
            if not result.success:
                return index, result
        return None

    def __iter__(self) -> Iterator[CommandResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, index: int) -> CommandResult:
        return self.results[index]


# ======================================================
# Building commands
# ======================================================

def make_command(command_name: str, /, **args: Any) -> dict:
    """One {command, args} entry, with unset (None) arguments left out.

    The first parameter is positional-only so that "name" stays free for
    the argument CreateGameObject and the lights and cameras take.
    """
    if command_name not in COMMANDS:
        raise ValueError(
            f"{command_name!r} is not a bridge command. Known: {', '.join(sorted(COMMANDS))}")
    return {"command": command_name,
            "args": {key: value for key, value in args.items() if value is not None}}


def _vector(value: Any, name: str, *, sizes: Sequence[int] = (3,)) -> Any:
    """A list of floats, or a form the bridge parses itself (dict, "1,2,3")."""
    if value is None:
        return None
    if isinstance(value, (str, dict)):
        return value
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a sequence of numbers, got {value!r}") from None
    if len(numbers) not in sizes:
        wanted = " or ".join(str(size) for size in sizes)
        raise ValueError(f"{name} must have {wanted} numbers, got {len(numbers)}")
    return numbers


def _scale(value: Any) -> Any:
    if value is None or isinstance(value, (str, dict)):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return _vector(value, "scale")


def _color(value: Any) -> Any:
    if value is None or isinstance(value, (str, dict)):
        return value
    return _vector(value, "color", sizes=(3, 4))


def _json_safe(value: Any) -> Any:
    """Tuples become lists; anything else is left for json.dumps to judge."""
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _as_command_list(commands: Any) -> list[dict]:
    if isinstance(commands, Batch):
        entries = list(commands.commands)
    elif isinstance(commands, dict):
        entries = [commands]
    else:
        entries = list(commands)

    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("command"):
            raise ValueError(f"each command must be a dict with a 'command' key, got {entry!r}")
    return entries


def _drop_none(values: dict) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def _place(at: Any) -> Any:
    """An object's path as it is, or (x, y) as a list of two numbers."""
    if isinstance(at, str):
        return at
    try:
        x, y = at
        return [float(x), float(y)]
    except (TypeError, ValueError):
        raise ValueError(f"a place is (x, y) or an object's path, not {at!r}") from None


class Inputs:
    """Builders for send_input's actions, so nobody has to remember the keys.

        bridge.send_input(Inputs.key("space"), Inputs.click("Canvas/Buy"),
                          Inputs.wait(10), Inputs.text("hello"))

    Places are (x, y) in pixels from the bottom left of the game's picture --
    the coordinates read_screen reports -- or (x, y) from 0 to 1 with
    viewport=True, or an object's hierarchy path, clicked where it is drawn.
    """

    @staticmethod
    def key(name: str | Sequence[str], *, action: str | None = None,
            hold: int | None = None, times: int | None = None) -> dict:
        """Tap a key ("space", "Enter", "a", "F5", "`"), or several held together.

        action "down" or "up" presses or lets go without the other half;
        hold is frames between press and release (2).
        """
        keys = [name] if isinstance(name, str) else list(name)
        return _drop_none({"key": keys[0] if len(keys) == 1 else keys,
                           "action": action, "hold": hold, "times": times})

    @staticmethod
    def click(at: Any, *, button: str | None = None, hold: int | None = None,
              times: int | None = None, viewport: bool = False) -> dict:
        """Move there, press a frame later, let go after hold frames. button: left/right/middle."""
        return _drop_none({"click": _place(at), "button": button, "hold": hold, "times": times,
                           "space": "viewport" if viewport else None})

    @staticmethod
    def move(at: Any, *, viewport: bool = False) -> dict:
        return _drop_none({"move": _place(at), "space": "viewport" if viewport else None})

    @staticmethod
    def mouse(state: str, at: Any = None, *, button: str | None = None,
              viewport: bool = False) -> dict:
        """Press ("down") or let go ("up") of a button without the other half, e.g. for a drag."""
        return _drop_none({"mouse": state, "at": None if at is None else _place(at),
                           "button": button, "space": "viewport" if viewport else None})

    @staticmethod
    def scroll(amount: float | Sequence[float]) -> dict:
        """Turn the wheel for one frame; up is positive. A number, or (x, y)."""
        if isinstance(amount, (int, float)):
            return {"scroll": amount}
        return {"scroll": _place(amount)}

    @staticmethod
    def text(words: str) -> dict:
        """Characters for Keyboard.onTextInput (uGUI and TMP fields). IMGUI fields do not hear it."""
        return {"text": str(words)}

    @staticmethod
    def wait(frames: int | None = None, *, seconds: float | None = None) -> dict:
        """Nothing, for a while: frames, or seconds, or both."""
        if frames is None and seconds is None:
            raise ValueError("wait for frames, seconds, or both")
        return _drop_none({"wait": frames, "waitSeconds": seconds})


class _CommandBuilder:
    """Every high-level operation, once.

    UnityEditorBridge sends each one as it is called; Batch collects them.
    Both inherit these builders and differ only in _submit.
    """

    def _submit(self, command: dict) -> Any:
        raise NotImplementedError

    # --- scene objects -------------------------------------------------

    def ping(self) -> Any:
        """Confirm the bridge is alive; data has the Unity version and active scene."""
        return self._submit(make_command("Ping"))

    def create_game_object(self, name: str, parent: str | None = None,
                           position: Sequence[float] | None = None,
                           rotation: Sequence[float] | None = None,
                           scale: Sequence[float] | float | None = None, *,
                           primitive: str | None = None,
                           components: Iterable[str] | None = None) -> Any:
        """Create a GameObject; `primitive` may be Cube, Sphere, Plane, Capsule, Cylinder or Quad."""
        return self._submit(make_command(
            "CreateGameObject",
            name=name,
            parent=parent,
            position=_vector(position, "position"),
            rotation=_vector(rotation, "rotation", sizes=(3, 4)),
            scale=_scale(scale),
            primitive=primitive,
            components=list(components) if components else None,
        ))

    def delete_game_object(self, target: str) -> Any:
        return self._submit(make_command("DeleteGameObject", target=target))

    def add_component(self, target: str, component_type: str, *,
                      allow_duplicate: bool = False) -> Any:
        return self._submit(make_command(
            "AddComponent", target=target, componentType=component_type,
            allowDuplicate=True if allow_duplicate else None))

    def remove_component(self, target: str, component_type: str) -> Any:
        return self._submit(make_command("RemoveComponent", target=target, componentType=component_type))

    def set_transform(self, target: str, position: Sequence[float] | None = None,
                      rotation: Sequence[float] | None = None,
                      scale: Sequence[float] | float | None = None, *,
                      local: bool = False) -> Any:
        """Set any of position (world), rotation (euler degrees) and scale. `local` switches to local space."""
        return self._submit(make_command(
            "SetTransform",
            target=target,
            position=_vector(position, "position"),
            rotation=_vector(rotation, "rotation", sizes=(3, 4)),
            scale=_scale(scale),
            local=True if local else None,
        ))

    def set_field(self, target: str, component_type: str | None, field_name: str, value: Any) -> Any:
        """Write an inspector field. component_type None or "GameObject" targets the object itself."""
        return self._submit({
            "command": "SetField",
            "args": {key: item for key, item in {
                "target": target,
                "componentType": component_type,
                "field": field_name,
                "value": _json_safe(value),
            }.items() if key == "value" or item is not None},
        })

    def get_field(self, target: str, component_type: str | None, field_name: str) -> Any:
        """Read an inspector field; the value is under data["value"]."""
        return self._submit(make_command(
            "GetField", target=target, componentType=component_type, field=field_name))

    def get_hierarchy(self, target: str | None = None, *, depth: int | None = None) -> Any:
        """Describe the loaded scenes, or the subtree under `target`."""
        return self._submit(make_command("GetHierarchy", target=target, depth=depth))

    # --- scenes and prefabs -------------------------------------------

    def open_scene(self, path: str, *, additive: bool = False, discard_changes: bool = False) -> Any:
        return self._submit(make_command(
            "OpenScene", path=path,
            additive=True if additive else None,
            discardChanges=True if discard_changes else None))

    def save_scene(self, path: str | None = None, *, save_all: bool = False) -> Any:
        """Save the active scene, to `path` if given (save as), or every open scene with save_all."""
        return self._submit(make_command("SaveScene", path=path, saveAll=True if save_all else None))

    def create_prefab(self, target: str, prefab_path: str, *, connect: bool = True) -> Any:
        """Save a scene object as a prefab asset; connect=True turns the scene object into an instance."""
        return self._submit(make_command(
            "CreatePrefab", target=target, prefabPath=prefab_path,
            connect=None if connect else False))

    def modify_prefab(self, prefab_path: str, operations: "Batch | Iterable[dict]", *,
                      stop_on_error: bool = True) -> Any:
        """Apply a list of commands inside a prefab. Targets resolve relative to its root ("" is the root)."""
        return self._submit(make_command(
            "ModifyPrefab", prefabPath=prefab_path,
            operations=_as_command_list(operations),
            stopOnError=None if stop_on_error else False))

    def instantiate_prefab(self, prefab_path: str, name: str | None = None,
                           parent: str | None = None,
                           position: Sequence[float] | None = None,
                           rotation: Sequence[float] | None = None) -> Any:
        return self._submit(make_command(
            "InstantiatePrefab", prefabPath=prefab_path, name=name, parent=parent,
            position=_vector(position, "position"),
            rotation=_vector(rotation, "rotation", sizes=(3, 4))))

    # --- lights and cameras -------------------------------------------

    def create_light(self, light_type: str = "Point", name: str | None = None,
                     position: Sequence[float] | None = None,
                     rotation: Sequence[float] | None = None,
                     intensity: float | None = None,
                     color: Sequence[float] | str | None = None, *,
                     parent: str | None = None,
                     range: float | None = None,
                     spot_angle: float | None = None,
                     shadows: str | None = None) -> Any:
        """Create a Directional, Point or Spot light."""
        return self._submit(make_command(
            "CreateLight",
            type=light_type, name=name, parent=parent,
            position=_vector(position, "position"),
            rotation=_vector(rotation, "rotation", sizes=(3, 4)),
            intensity=intensity, color=_color(color), range=range,
            spotAngle=spot_angle, shadows=shadows))

    def create_camera(self, name: str | None = None,
                      position: Sequence[float] | None = None,
                      rotation: Sequence[float] | None = None,
                      fov: float | None = None,
                      clear_flags: str | None = None, *,
                      parent: str | None = None,
                      background_color: Sequence[float] | str | None = None,
                      main: bool = False,
                      near: float | None = None,
                      far: float | None = None,
                      orthographic: bool | None = None,
                      orthographic_size: float | None = None) -> Any:
        """Create a camera. clear_flags: Skybox, SolidColor, Depth or Nothing. main=True tags it MainCamera."""
        return self._submit(make_command(
            "CreateCamera",
            name=name, parent=parent,
            position=_vector(position, "position"),
            rotation=_vector(rotation, "rotation", sizes=(3, 4)),
            fov=fov, clearFlags=clear_flags,
            backgroundColor=_color(background_color),
            main=True if main else None,
            near=near, far=far,
            orthographic=orthographic, orthographicSize=orthographic_size))

    # --- looking at the result -----------------------------------------

    def refresh_assets(self, path: str | None = None, *,
                       force: bool = False, recursive: bool = False) -> Any:
        """Import what has changed on disk, and report what Unity made of it.

        With no path, a plain AssetDatabase.Refresh. With one, that asset is
        imported and the result describes it: the guid, the main asset's
        type, the importer, every Sprite hanging off it, and -- for a
        texture -- the settings that decide whether it became one.

        That reporting half is the point of the command. Writing a .png and
        a hand-rolled .meta beside it is a guess until something asks the
        AssetDatabase what the pair turned into.
        """
        return self._submit(make_command(
            "RefreshAssets", path=path,
            force=True if force else None,
            recursive=True if recursive else None))

    def screenshot(self, path: str | None = None, *,
                   width: int | None = None, height: int | None = None,
                   view: str | None = None, count: int | None = None,
                   every: int | None = None, every_seconds: float | None = None,
                   sheet: bool | None = None, columns: int | None = None) -> Any:
        """Render a camera to a PNG and return where it landed.

        view: "game" (default) renders the main camera, "scene" the Scene
        view. Paths are relative to the project and default under ARIA/, so
        a screenshot is not filed as a game asset.

        An overlay canvas draws to the display and appears in no camera's
        render, so the game view capture borrows every one of them onto the
        capture camera for the single frame and hands them back afterwards.
        Without that, a game that builds its interface in code photographs
        as an empty room.

        count > 1 takes a BURST while the game plays: a shot every
        every_seconds (0.1 unless every, in frames, is given), saved as
        <path>_00.png, _01.png..., laid out on one contact sheet
        <path>_sheet.png in reading order, and listed with frame and time in
        <path>.json. Shots default to 640x360 in a burst. It returns before
        the first shot; wait_for_burst() waits and reads the list, and the
        module-level screenshot_burst() does both.
        """
        return self._submit(make_command(
            "Screenshot", path=path, width=width, height=height, view=view,
            count=count, every=every, everySeconds=every_seconds, sheet=sheet, columns=columns))

    def set_play_mode(self, playing: bool = True, *, test_save: bool = True,
                      seed: str | None = None, keep_running: bool = True,
                      allow_unfocused: bool = False, skip_snapshot: bool = False) -> Any:
        """Start or stop the game, and return before it happens.

        Entering play mode reloads the C# domain and throws away everything
        holding the call, so the result is written first and the change
        lands after. Send whatever comes next as its own batch: by then the
        bridge has come back up on the other side, already playing -- and a
        playing bridge runs only PLAY_SAFE_COMMANDS: ping, screenshot, the
        two reads, and set_play_mode(False) to stop. Anything that edits a
        scene waits in the commands file until the game stops.

        This is what makes screenshot() worth having on a project that
        builds its interface at runtime. In edit mode there is no interface
        yet -- only the scripts that will make one.

        Entering refuses while Unity is not the focused application unless
        allow_unfocused=True: unfocused, the game receives a focus-lost event
        before its Start runs, and with Run In Background off it will not
        advance a frame. Before entering, the bridge copies the game's
        persistentDataPath to ARIA/snapshots (newest ten kept) and will not
        start without that copy unless skip_snapshot=True -- play mode runs
        the game's own save code against the player's real files.

        test_save (on unless refused) points the game at ARIA/testsave
        instead, through the ARIA_TEST_SAVE_DIR environment variable, for a
        game that reads it. seed decides what that folder holds as play
        begins: "keep" (the default: as the last test left it), "fresh"
        (emptied, so a new game) or "real" (a copy of the player's save, to
        start where they are without being able to write back). When the
        session ends the bridge compares the real save folder with how it
        was and says in ARIA/last_play.json whether anything changed;
        last_play() reads it.

        keep_running (on unless refused) keeps a test session advancing
        while Unity is not the focused application. Without it an unfocused
        editor plays no frames at all (measured: minimized mid-play, the
        frame count stood still). The bridge sets Application.runInBackground
        for the session -- which in the editor is also Player Settings' Run
        In Background, so both read true meanwhile -- and hands the old value
        back as play ends. With it and test_save both on, an unfocused start
        is allowed without allow_unfocused: the focus-lost save lands in the
        test folder, and the frames come.
        """
        return self._submit(make_command(
            "SetPlayMode", playing=bool(playing),
            testSave=None if test_save else False,
            seed=seed,
            keepRunning=None if keep_running else False,
            allowUnfocused=True if allow_unfocused else None,
            skipSnapshot=True if skip_snapshot else None))

    # --- playing the game ----------------------------------------------

    def send_input(self, *actions: dict, allow_real_save: bool = False) -> Any:
        """Press keys, click, scroll and type in a game the bridge started.

        Each action is a dict; Inputs builds them. The editor queues them and
        plays them over the next frames -- a press and its release have to
        land in different frames for wasPressedThisFrame to see them --
        through devices of its own, "ARIA Keyboard" and "ARIA Mouse", which
        become Keyboard.current and Mouse.current as they send. For the
        length of a test session the Input System sends input to the game
        whatever has focus, so this works with the editor in the background.

        An object's path is found where it is drawn as the command arrives;
        something that appears later is clicked after a wait sent first.
        Only a test session (set_play_mode on the test save) is driven:
        input changes the game and the game saves what changes. Pass
        allow_real_save=True to drive a real one anyway.

        Returns as soon as the steps are queued; wait_for_input() waits for
        them to play, and the module-level send_input() does both.
        """
        if not actions:
            raise ValueError("send_input needs at least one action")
        return self._submit(make_command(
            "SendInput", actions=[dict(action) for action in actions],
            allowRealSave=True if allow_real_save else None))

    def read_screen(self, targets: Iterable[str] | str | None = None, *,
                    include_hidden: bool = False, limit: int | None = None) -> Any:
        """What is on the game's screen: the text a person would read, and what they could press.

        data["lines"] is the visible text in reading order. data["texts"]
        and data["controls"] give each item's path, kind, rect and center in
        pixels from the bottom left -- the places send_input clicks -- and
        controls say whether they are interactable. Visible means active,
        enabled, not faded by a CanvasGroup, not transparent and on screen;
        include_hidden lists the rest too. targets asks where particular
        objects are drawn, UI or world. IMGUI cannot be read back.
        """
        if isinstance(targets, str):
            targets = [targets]
        return self._submit(make_command(
            "ReadScreen", targets=list(targets) if targets is not None else None,
            includeHidden=True if include_hidden else None, limit=limit))

    def get_log(self, since: int | None = None, *, session: bool = False,
                types: str | Iterable[str] | None = None, contains: str | None = None,
                limit: int | None = None, stack: bool | None = None) -> Any:
        """What the Unity console has said: the game's messages and the compiler's errors.

        since takes data["next"] from an earlier call, so each read is only
        what is new; session=True starts from the last bridge-started play
        session. types narrows it -- "errors" is every kind of fault (error,
        exception, assert, compileError); the others are log, warning,
        error, exception, assert, compile and compileError. Errors carry the
        first lines of their stack unless stack=False.

        Compile errors are the ones worth knowing about first: a script
        that does not compile leaves the editor running the last scripts
        that did, this bridge among them, so everything else goes on
        answering as if nothing were wrong.
        """
        if isinstance(types, str):
            types = [types]
        return self._submit(make_command(
            "GetLog", since=since, session=True if session else None,
            types=list(types) if types is not None else None,
            contains=contains, limit=limit, stack=stack))

    def set_time(self, time_scale: float | None = None, *, paused: bool | None = None,
                 step: int | None = None, allow_real_save: bool = False) -> Any:
        """Read or change the game's clock.

        time_scale runs it faster or slower (0 to 100); paused holds it;
        step plays exactly that many frames and holds again, pausing first
        if the game was running (Ping's stepsPending reaches 0 when done).
        Everything changed is put back as play ends. With no arguments it
        only reads: data has timeScale, paused, frame, time, unscaledTime.
        Changes are for test sessions unless allow_real_save=True.
        """
        return self._submit(make_command(
            "SetTime", timeScale=time_scale, paused=paused, step=step,
            allowRealSave=True if allow_real_save else None))

    def run_console(self, *lines: str, allow_real_save: bool = False) -> Any:
        """Type lines into the game's own debug console and return what it printed.

        Through the one method a game defines for it -- public static string
        ARIAConsole(string line) -- rather than simulated keys, which an
        IMGUI console's text field does not hear. data["ran"] is one
        {line, output} per line; a line that throws stops the rest and the
        command fails naming it. For test sessions unless allow_real_save=True.
        """
        if not lines:
            raise ValueError("run_console needs at least one line")
        return self._submit(make_command(
            "RunConsole", lines=[str(line) for line in lines],
            allowRealSave=True if allow_real_save else None))


class Batch(_CommandBuilder):
    """Commands collected to be sent together, in one file and one editor pass.

    A batch is what ModifyPrefab takes as its operations, and what
    UnityEditorBridge.batch() sends when its block ends.
    """

    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.results: BridgeResults | None = None

    def _submit(self, command: dict) -> int:
        self.commands.append(command)
        return len(self.commands) - 1

    def add(self, name: str, **args: Any) -> int:
        """Queue a raw command by name; returns its index in the batch."""
        return self._submit(make_command(name, **args))

    def __len__(self) -> int:
        return len(self.commands)

    def __iter__(self) -> Iterator[dict]:
        return iter(self.commands)


# ======================================================
# The bridge
# ======================================================

class UnityEditorBridge(_CommandBuilder):
    """Writes commands for one Unity project and reads what its editor said.

    Every high-level method returns the command's `data` dict and raises
    UnityBridgeError when the editor refused. With wait=False the methods
    return None after writing the file, for a workflow where a person
    triggers ARIA > Run Bridge Commands by hand and reads results later.
    """

    def __init__(self, project_root: str | os.PathLike | None = None, *,
                 timeout: float | None = None, wait: bool = True,
                 poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self.project_root = find_project_root(project_root)
        self.timeout = float(timeout) if timeout is not None else _default_timeout()
        self.wait = wait
        self.poll_interval = poll_interval
        self.last_results: BridgeResults | None = None

    # --- paths ---------------------------------------------------------

    @property
    def folder(self) -> Path:
        return self.project_root / FOLDER_NAME

    @property
    def commands_path(self) -> Path:
        return self.folder / COMMANDS_FILE

    @property
    def results_path(self) -> Path:
        return self.folder / RESULTS_FILE

    @property
    def bridge_script_path(self) -> Path:
        return self.project_root / BRIDGE_INSTALL_PATH

    def is_installed(self) -> bool:
        return self.bridge_script_path.is_file()

    def install_bridge(self, source: str | os.PathLike = BRIDGE_SOURCE) -> Path:
        """Copy the C# bridge into the project. Unity compiles it on its next refresh."""
        source_path = Path(source)
        if not source_path.is_file():
            raise UnityBridgeUnavailable(f"No bridge source at {source_path}.")

        destination = self.bridge_script_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != source_path.read_bytes():
            shutil.copyfile(source_path, destination)
        return destination

    # --- the exchange --------------------------------------------------

    def _submit(self, command: dict) -> dict | None:
        results = self.send([command])
        return results[0].data if results is not None else None

    def run(self, command: str, **args: Any) -> dict | None:
        """Send one raw command by name and return its data."""
        return self._submit(make_command(command, **args))

    def send(self, commands: "Batch | Iterable[dict] | dict", *, stop_on_error: bool = True,
             wait: bool | None = None, timeout: float | None = None) -> BridgeResults | None:
        """Write the commands file, then (unless wait is off) wait for and check the results.

        Raises UnityBridgeError naming the first failed command, or
        UnityBridgeTimeout when nothing answered.
        """
        entries = _as_command_list(commands)
        if not entries:
            raise ValueError("nothing to send")

        request_id = uuid.uuid4().hex
        self._remove(self.results_path)
        self.write_commands(entries, request_id=request_id, stop_on_error=stop_on_error)

        if not (self.wait if wait is None else wait):
            return None

        results = self.wait_for_results(request_id, timeout=timeout)
        self.last_results = results

        failure = results.first_failure()
        if failure is not None:
            index, item = failure
            raise UnityBridgeError(
                f"{item.command} (command {index + 1} of {len(results)}) failed: {item.message}",
                command=item.command, index=index, results=results.results)
        if not results.ok:
            raise UnityBridgeError(results.message or "the bridge reported failure",
                                   results=results.results)
        return results

    def write_commands(self, commands: Iterable[dict], *, request_id: str | None = None,
                       stop_on_error: bool = True) -> Path:
        """Write the commands file whole, then move it into place.

        The bridge polls for this file; a rename is atomic where a write
        is not, so it never reads half a document.
        """
        envelope = {
            "id": request_id or uuid.uuid4().hex,
            "stopOnError": bool(stop_on_error),
            "commands": [_json_safe(entry) for entry in commands],
        }
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.folder / (COMMANDS_FILE + ".tmp")
        temp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        os.replace(temp, self.commands_path)
        return self.commands_path

    def read_results(self, request_id: str | None = None) -> BridgeResults | None:
        """The results file, or None when it is absent, half-written, or answers another request."""
        try:
            text = self.results_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            raw = json.loads(text)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        if request_id is not None and raw.get("id") != request_id:
            return None
        return BridgeResults.from_dict(raw)

    def wait_for_results(self, request_id: str | None = None, *,
                         timeout: float | None = None) -> BridgeResults:
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        while True:
            results = self.read_results(request_id)
            if results is not None:
                return results
            if time.monotonic() >= deadline:
                break
            time.sleep(self.poll_interval)

        if self.commands_path.exists():
            raise UnityBridgeTimeout(
                f"Unity did not pick up {self.commands_path} within {limit:g}s. Is the editor "
                f"open on {self.project_root} with the bridge installed and ARIA > Watch For "
                f"Commands on? ARIA > Run Bridge Commands runs it by hand.")
        raise UnityBridgeTimeout(
            f"Unity took the commands but wrote no results within {limit:g}s. "
            f"Look for [ARIA] lines in the Unity console.")

    @contextlib.contextmanager
    def batch(self, *, stop_on_error: bool = True, wait: bool | None = None) -> Iterator[Batch]:
        """Collect calls made inside the block and send them together when it ends.

            with bridge.batch() as b:
                b.create_game_object("Floor", primitive="Plane")
                b.add_component("Floor", "BoxCollider")
            b.results[1].data
        """
        collected = Batch()
        yield collected
        if collected.commands:
            collected.results = self.send(collected, stop_on_error=stop_on_error, wait=wait)

    def pending(self) -> bool:
        """Whether a commands file is waiting for the editor."""
        return self.commands_path.exists()

    def last_play(self) -> dict | None:
        """What the last bridge-started play session left behind, or None.

        ARIA/last_play.json, written as the session ends. realSaveSafe is
        True only when every file under the game's persistentDataPath was
        the same at the end as at the start; realSaveTouched names any that
        were not. A game that ignores the test save variable shows up here.
        """
        try:
            raw = json.loads((self.folder / LAST_PLAY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def read_burst(self, manifest: str | os.PathLike) -> dict | None:
        """A burst's list of shots, with sheetPath and each shot's absolutePath added; None if absent."""
        path = Path(manifest)
        if not path.is_absolute():
            path = self.project_root / path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("sheet"):
            raw["sheetPath"] = str(self.project_root / raw["sheet"])
        for shot in raw.get("shots") or []:
            if isinstance(shot, dict) and shot.get("path"):
                shot["absolutePath"] = str(self.project_root / shot["path"])
        return raw

    def wait_for_burst(self, timeout: float | None = None) -> dict | None:
        """Wait until a burst has been taken, then return its list of shots."""
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        while True:
            data = self.ping() or {}
            if not data.get("burstPending"):
                return self.read_burst(data["lastBurst"]) if data.get("lastBurst") else None
            if time.monotonic() >= deadline:
                raise UnityBridgeTimeout(
                    f"{data.get('burstPending')} shot(s) were still to take after {limit:g}s, at frame "
                    f"{data.get('frame')}. Is the game advancing?")
            time.sleep(self.poll_interval)

    def wait_for_steps(self, timeout: float | None = None) -> dict:
        """Wait until every frame set_time(step=...) queued has played; returns the last ping."""
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        while True:
            data = self.ping() or {}
            if not data.get("stepsPending"):
                return data
            if time.monotonic() >= deadline:
                raise UnityBridgeTimeout(
                    f"{data.get('stepsPending')} frame(s) were still to step after {limit:g}s.")
            time.sleep(self.poll_interval)

    def wait_for_input(self, timeout: float | None = None) -> dict:
        """Wait until everything send_input queued has played; returns the last ping.

        Input plays as the game's frames go by, so a queue that will not
        drain is a game that is not advancing -- paused, or in the
        background with keep_running refused.
        """
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        while True:
            data = self.ping() or {}
            if not data.get("inputPending"):
                return data
            if time.monotonic() >= deadline:
                raise UnityBridgeTimeout(
                    f"{data.get('inputPending')} input step(s) were still queued after {limit:g}s, "
                    f"at frame {data.get('frame')}. Is the game advancing -- not paused, and not in "
                    f"the background with keep_running refused?")
            time.sleep(self.poll_interval)

    def clear(self) -> None:
        """Remove both files, so a stale exchange cannot be mistaken for a fresh one."""
        self._remove(self.commands_path)
        self._remove(self.results_path)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # --- without an open editor ----------------------------------------

    def send_headless(self, commands: "Batch | Iterable[dict] | dict", *,
                      unity_path: str | os.PathLike | None = None,
                      stop_on_error: bool = True,
                      timeout: float = HEADLESS_TIMEOUT_SECONDS) -> BridgeResults:
        """Start a batch-mode editor to process the commands, for a project nobody has open.

        Slow (a cold project import can take minutes) and refused by Unity
        while another editor holds the project, so the open-editor path is
        the default and this is the fallback.
        """
        entries = _as_command_list(commands)
        request_id = uuid.uuid4().hex
        self._remove(self.results_path)
        self.write_commands(entries, request_id=request_id, stop_on_error=stop_on_error)

        executable = self._editor_path(unity_path)
        log_path = self.folder / "unity_headless.log"
        command = [
            str(executable), "-batchmode", "-nographics", "-quit",
            "-projectPath", str(self.project_root),
            "-executeMethod", ENTRY_POINT,
            "-logFile", str(log_path),
        ]
        try:
            subprocess.run(command, check=False, timeout=timeout, shell=False)
        except subprocess.TimeoutExpired:
            raise UnityBridgeTimeout(
                f"Unity did not finish within {timeout:g}s; see {log_path}.") from None
        except OSError as error:
            raise UnityBridgeUnavailable(f"Could not start {executable}: {error}") from None

        results = self.read_results(request_id)
        if results is None:
            raise UnityBridgeError(
                f"The headless editor wrote no results. Is the bridge installed at "
                f"{self.bridge_script_path} and compiling? See {log_path}.")
        self.last_results = results

        failure = results.first_failure()
        if failure is not None:
            index, item = failure
            raise UnityBridgeError(
                f"{item.command} (command {index + 1} of {len(results)}) failed: {item.message}",
                command=item.command, index=index, results=results.results)
        if not results.ok:
            raise UnityBridgeError(results.message or "the bridge reported failure",
                                   results=results.results)
        return results

    @staticmethod
    def _editor_path(explicit: str | os.PathLike | None) -> Path:
        if explicit:
            return Path(explicit)
        if os.environ.get(ENV_EDITOR):
            return Path(os.environ[ENV_EDITOR])
        try:
            from backend.core import unity_ops

            return unity_ops.editor_path()
        except Exception as error:
            raise UnityBridgeUnavailable(
                f"Could not find Unity. Pass unity_path or set {ENV_EDITOR}. ({error})") from None


# ======================================================
# The default bridge, and the functions on it
# ======================================================

_default: UnityEditorBridge | None = None


def configure(project_root: str | os.PathLike | None = None, *,
              timeout: float | None = None, wait: bool = True) -> UnityEditorBridge:
    """Point the module-level functions at a project. Called lazily with defaults otherwise."""
    global _default
    _default = UnityEditorBridge(project_root, timeout=timeout, wait=wait)
    return _default


def get_bridge() -> UnityEditorBridge:
    if _default is None:
        return configure()
    return _default


def ping() -> dict | None:
    return get_bridge().ping()


def create_game_object(name: str, parent: str | None = None,
                       position: Sequence[float] | None = None,
                       rotation: Sequence[float] | None = None,
                       scale: Sequence[float] | float | None = None, *,
                       primitive: str | None = None,
                       components: Iterable[str] | None = None) -> dict | None:
    """Create a GameObject in the open scene; returns its path, instanceId and globalId."""
    return get_bridge().create_game_object(
        name, parent, position, rotation, scale, primitive=primitive, components=components)


def delete_game_object(target: str) -> dict | None:
    return get_bridge().delete_game_object(target)


def add_component(target: str, component_type: str, *, allow_duplicate: bool = False) -> dict | None:
    return get_bridge().add_component(target, component_type, allow_duplicate=allow_duplicate)


def remove_component(target: str, component_type: str) -> dict | None:
    return get_bridge().remove_component(target, component_type)


def set_transform(target: str, position: Sequence[float] | None = None,
                  rotation: Sequence[float] | None = None,
                  scale: Sequence[float] | float | None = None, *,
                  local: bool = False) -> dict | None:
    return get_bridge().set_transform(target, position, rotation, scale, local=local)


def set_field(target: str, component_type: str | None, field_name: str, value: Any) -> dict | None:
    return get_bridge().set_field(target, component_type, field_name, value)


def get_field(target: str, component_type: str | None, field_name: str) -> Any:
    """The field's value (not the data dict), for reading in one line."""
    data = get_bridge().get_field(target, component_type, field_name)
    return None if data is None else data.get("value")


def get_hierarchy(target: str | None = None, *, depth: int | None = None) -> dict | None:
    return get_bridge().get_hierarchy(target, depth=depth)


def open_scene(path: str, *, additive: bool = False, discard_changes: bool = False) -> dict | None:
    return get_bridge().open_scene(path, additive=additive, discard_changes=discard_changes)


def save_scene(path: str | None = None, *, save_all: bool = False) -> dict | None:
    return get_bridge().save_scene(path, save_all=save_all)


def create_prefab(target: str, prefab_path: str, *, connect: bool = True) -> dict | None:
    return get_bridge().create_prefab(target, prefab_path, connect=connect)


def modify_prefab(prefab_path: str, operations: "Batch | Iterable[dict]", *,
                  stop_on_error: bool = True) -> dict | None:
    return get_bridge().modify_prefab(prefab_path, operations, stop_on_error=stop_on_error)


def instantiate_prefab(prefab_path: str, name: str | None = None, parent: str | None = None,
                       position: Sequence[float] | None = None,
                       rotation: Sequence[float] | None = None) -> dict | None:
    return get_bridge().instantiate_prefab(prefab_path, name, parent, position, rotation)


def create_light(light_type: str = "Point", name: str | None = None,
                 position: Sequence[float] | None = None,
                 rotation: Sequence[float] | None = None,
                 intensity: float | None = None,
                 color: Sequence[float] | str | None = None, **extra: Any) -> dict | None:
    return get_bridge().create_light(light_type, name, position, rotation, intensity, color, **extra)


def create_camera(name: str | None = None, position: Sequence[float] | None = None,
                  rotation: Sequence[float] | None = None, fov: float | None = None,
                  clear_flags: str | None = None, **extra: Any) -> dict | None:
    return get_bridge().create_camera(name, position, rotation, fov, clear_flags, **extra)


def refresh_assets(path: str | None = None, **extra: Any) -> dict | None:
    return get_bridge().refresh_assets(path, **extra)


def screenshot(path: str | None = None, **extra: Any) -> dict | None:
    return get_bridge().screenshot(path, **extra)


def set_play_mode(playing: bool = True, **extra: Any) -> dict | None:
    return get_bridge().set_play_mode(playing, **extra)


def last_play() -> dict | None:
    return get_bridge().last_play()


def send_input(*actions: dict, wait: bool = True, **extra: Any) -> dict | None:
    """Queue the actions and, unless wait=False, return once they have played."""
    bridge = get_bridge()
    data = bridge.send_input(*actions, **extra)
    if wait and bridge.wait:
        bridge.wait_for_input()
    return data


def read_screen(targets: Iterable[str] | str | None = None, **extra: Any) -> dict | None:
    return get_bridge().read_screen(targets, **extra)


def get_log(since: int | None = None, **extra: Any) -> dict | None:
    return get_bridge().get_log(since, **extra)


def set_time(time_scale: float | None = None, **extra: Any) -> dict | None:
    return get_bridge().set_time(time_scale, **extra)


def step_frames(frames: int, **extra: Any) -> dict | None:
    """Pause, play exactly `frames` frames, and return once they have played."""
    bridge = get_bridge()
    data = bridge.set_time(step=frames, **extra)
    return bridge.wait_for_steps() if bridge.wait else data


def run_console(*lines: str, **extra: Any) -> dict | None:
    return get_bridge().run_console(*lines, **extra)


def screenshot_burst(count: int = 8, path: str | None = None, **extra: Any) -> dict | None:
    """Take a burst while the game plays, wait for it, and return its list of shots."""
    bridge = get_bridge()
    bridge.screenshot(path, count=count, **extra)
    return bridge.wait_for_burst() if bridge.wait else None


def install_bridge(project_root: str | os.PathLike | None = None) -> Path:
    """Copy the C# bridge into a project (the default one when project_root is None)."""
    bridge = get_bridge() if project_root is None else UnityEditorBridge(project_root)
    return bridge.install_bridge()


# ======================================================
# Command router: plain descriptions to bridge commands
# ======================================================
#
# Not language understanding; a handful of shapes ARIA can be relied on
# to produce. Anything it cannot map raises UnroutableCommand rather than
# guessing, because a guessed edit to a scene is worse than no edit.

_NUMBER = r"-?\d+(?:\.\d+)?"
_TRIPLE = re.compile(
    rf"\(?\s*(?P<a>{_NUMBER})\s*[, ]\s*(?P<b>{_NUMBER})\s*[, ]\s*(?P<c>{_NUMBER})\s*\)?")
_NAMED_AXES = re.compile(rf"\b([xyz])\s*=\s*({_NUMBER})", re.I)
_NAME = re.compile(r"(?:named|called)\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", re.I)
_QUOTED = re.compile(r"\"([^\"]+)\"|'([^']+)'")
_PARENT = re.compile(r"(?:under|child of|parented to|inside of)\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", re.I)
_CLAUSE_SPLIT = re.compile(r"\s*(?:;|\.\s+|,?\s+(?:and\s+)?then\s+|,\s+and\s+|\s+and\s+(?=(?:then\s+)?(?:add|attach|set|move|rotate|scale|delete|remove|save|open|create|make|spawn|place|put|enable|disable)\b))\s*", re.I)

PRIMITIVES = {"plane", "cube", "sphere", "capsule", "cylinder", "quad"}

COMPONENT_ALIASES = {
    "rigidbody": "Rigidbody",
    "rigid body": "Rigidbody",
    "rigidbody2d": "Rigidbody2D",
    "box collider": "BoxCollider",
    "sphere collider": "SphereCollider",
    "capsule collider": "CapsuleCollider",
    "mesh collider": "MeshCollider",
    "audio source": "AudioSource",
    "audio listener": "AudioListener",
    "mesh renderer": "MeshRenderer",
    "mesh filter": "MeshFilter",
    "character controller": "CharacterController",
    "nav mesh agent": "NavMeshAgent",
    "navmesh agent": "NavMeshAgent",
    "animator": "Animator",
    "light": "Light",
    "camera": "Camera",
}

# Where a field lives when the description does not say.
FIELD_COMPONENTS = {
    "mass": "Rigidbody",
    "drag": "Rigidbody",
    "angulardrag": "Rigidbody",
    "usegravity": "Rigidbody",
    "iskinematic": "Rigidbody",
    "intensity": "Light",
    "range": "Light",
    "spotangle": "Light",
    "fov": "Camera",
    "fieldofview": "Camera",
    "nearclipplane": "Camera",
    "farclipplane": "Camera",
}


def _triple(text: str) -> list[float] | None:
    named = dict((axis.lower(), float(value)) for axis, value in _NAMED_AXES.findall(text))
    if named:
        return [named.get("x", 0.0), named.get("y", 0.0), named.get("z", 0.0)]
    match = _TRIPLE.search(text)
    if match:
        return [float(match.group("a")), float(match.group("b")), float(match.group("c"))]
    return None


def _position(text: str) -> list[float] | None:
    match = re.search(rf"\b(?:at|to|position(?:ed)?(?:\s+at)?)\s+(?P<rest>.+)$", text, re.I)
    if not match:
        return None
    return _triple(match.group("rest"))


def _scalar(text: str, *keys: str) -> float | None:
    for key in keys:
        match = re.search(rf"\b{key}\s*(?:=|of|:)?\s*({_NUMBER})", text, re.I)
        if match:
            return float(match.group(1))
    return None


def _named(text: str) -> str | None:
    match = _NAME.search(text)
    if match:
        return next(group for group in match.groups() if group)
    match = _QUOTED.search(text)
    if match:
        return next(group for group in match.groups() if group)
    return None


def _parent(text: str) -> str | None:
    match = _PARENT.search(text)
    if not match:
        return None
    return next(group for group in match.groups() if group)


def _strip_target(text: str) -> str:
    text = text.strip().strip("\"'").strip()
    return re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.I)


def _component_name(text: str) -> str:
    key = " ".join(text.strip().split()).lower()
    if key in COMPONENT_ALIASES:
        return COMPONENT_ALIASES[key]
    if " " in key:
        return "".join(part[:1].upper() + part[1:] for part in text.split())
    return text.strip()


def _value(text: str) -> Any:
    raw = text.strip().rstrip(".")
    lowered = raw.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if re.fullmatch(_NUMBER, raw):
        return float(raw) if "." in raw else int(raw)
    if re.fullmatch(rf"{_NUMBER}(?:\s*,\s*{_NUMBER})+", raw):
        return [float(part) for part in raw.split(",")]
    return raw.strip("\"'")


def _asset_path(raw: str, folder: str, suffix: str) -> str:
    path = raw.strip().strip("\"'").replace("\\", "/")
    if not path.lower().endswith(suffix):
        path += suffix
    if "/" not in path:
        path = f"{folder}/{path}"
    return path


_NOT_A_COLOUR = {"background", "of", "to", "and", "with", "the", "a", "an", "at", "then"}


def _color_text(text: str) -> Any:
    # "solid colour" names a clear mode, not a colour.
    text = re.sub(r"\bsolid\s*colou?r\b", "", text, flags=re.I)
    match = re.search(rf"\bcolou?r\s*(?:=|:|of)?\s*(#[0-9a-fA-F]{{6}}|{_NUMBER}(?:\s*,\s*{_NUMBER}){{2,3}}|[A-Za-z]+)", text, re.I)
    if not match:
        return None
    value = match.group(1)
    if "," in value:
        return [float(part) for part in value.split(",")]
    if value.lower() in _NOT_A_COLOUR:
        return None
    return value


class _Router:
    """Holds the name of the object the previous clause made, so "it" works."""

    def __init__(self) -> None:
        self.last_object: str | None = None

    def target(self, text: str) -> str:
        cleaned = _strip_target(text)
        if cleaned.lower() in {"it", "that", "this", "them"} and self.last_object:
            return self.last_object
        return cleaned

    # Each handler returns a list of command dicts.

    def create_object(self, match: re.Match) -> list[dict]:
        kind = (match.group("kind") or "").lower().replace(" ", "")
        adjective = match.group("adj")
        rest = match.group("rest") or ""
        primitive = kind.capitalize() if kind in PRIMITIVES else None
        name = _named(rest) or (adjective.capitalize() if adjective else None) or (primitive or "GameObject")
        scale = _scalar(rest, "size", "scale")
        scale_vector = None
        scale_match = re.search(r"\b(?:size|scale)\s+(?P<rest>.+)$", rest, re.I)
        if scale_match:
            scale_vector = _triple(scale_match.group("rest"))
        self.last_object = name
        return [make_command(
            "CreateGameObject", name=name, primitive=primitive,
            parent=_parent(rest), position=_position(rest),
            rotation=_triple(match_or_empty(re.search(r"rotat(?:ed|ion)\s+(?P<rest>.+)$", rest, re.I))),
            scale=scale_vector if scale_vector else scale)]

    def create_light(self, match: re.Match) -> list[dict]:
        kind = (match.group("type") or "point").lower()
        light_type = {"sun": "Directional"}.get(kind, kind.capitalize())
        rest = match.group("rest") or ""
        name = _named(rest) or f"{light_type} Light"
        self.last_object = name
        return [make_command(
            "CreateLight", type=light_type, name=name, parent=_parent(rest),
            position=_position(rest),
            rotation=_triple(match_or_empty(re.search(r"rotat(?:ed|ion)\s+(?P<rest>.+)$", rest, re.I))),
            intensity=_scalar(rest, "intensity", "brightness"),
            color=_color_text(rest), range=_scalar(rest, "range"))]

    def create_camera(self, match: re.Match) -> list[dict]:
        rest = match.group("rest") or ""
        main = bool(match.group("main"))
        name = _named(rest) or ("Main Camera" if main else "Camera")
        clear = None
        if re.search(r"solid\s*colou?r", rest, re.I):
            clear = "SolidColor"
        elif re.search(r"\bskybox\b", rest, re.I):
            clear = "Skybox"
        self.last_object = name
        return [make_command(
            "CreateCamera", name=name, parent=_parent(rest),
            position=_position(rest),
            rotation=_triple(match_or_empty(re.search(r"(?:rotat(?:ed|ion)|looking)\s+(?P<rest>.+)$", rest, re.I))),
            fov=_scalar(rest, "fov", "field of view"), clearFlags=clear,
            backgroundColor=_color_text(rest), main=True if main else None)]

    def add_component(self, match: re.Match) -> list[dict]:
        return [make_command("AddComponent", target=self.target(match.group("target")),
                             componentType=_component_name(match.group("comp")))]

    def remove_component(self, match: re.Match) -> list[dict]:
        return [make_command("RemoveComponent", target=self.target(match.group("target")),
                             componentType=_component_name(match.group("comp")))]

    def delete_object(self, match: re.Match) -> list[dict]:
        return [make_command("DeleteGameObject", target=self.target(match.group("target")))]

    def move(self, match: re.Match) -> list[dict]:
        position = _triple(match.group("pos"))
        if position is None:
            raise UnroutableCommand(f"Could not read a position from {match.group('pos')!r}.")
        return [make_command("SetTransform", target=self.target(match.group("target")), position=position)]

    def rotate(self, match: re.Match) -> list[dict]:
        rotation = _triple(match.group("rot"))
        if rotation is None:
            raise UnroutableCommand(f"Could not read a rotation from {match.group('rot')!r}.")
        return [make_command("SetTransform", target=self.target(match.group("target")), rotation=rotation)]

    def scale(self, match: re.Match) -> list[dict]:
        text = match.group("scale")
        scale: Any = _triple(text)
        if scale is None:
            single = re.search(_NUMBER, text)
            if not single:
                raise UnroutableCommand(f"Could not read a scale from {text!r}.")
            scale = float(single.group(0))
        return [make_command("SetTransform", target=self.target(match.group("target")), scale=scale)]

    def set_field(self, match: re.Match) -> list[dict]:
        groups = match.groupdict()
        field_name = groups["field"].strip()
        component = groups.get("comp")
        if component:
            component = _component_name(component)
        else:
            component = FIELD_COMPONENTS.get(re.sub(r"[\s_]", "", field_name).lower())
        command = make_command("SetField", target=self.target(groups["target"]),
                               componentType=component, field=field_name)
        command["args"]["value"] = _value(groups["value"])
        return [command]

    def set_active(self, match: re.Match) -> list[dict]:
        verb = match.group("verb").lower()
        active = verb in {"enable", "activate", "show"}
        command = make_command("SetField", target=self.target(match.group("target")), field="active")
        command["args"]["value"] = active
        return [command]

    def open_scene(self, match: re.Match) -> list[dict]:
        return [make_command("OpenScene", path=_asset_path(match.group("path"), "Assets/Scenes", ".unity"))]

    def save_scene(self, match: re.Match) -> list[dict]:
        path = match.group("path")
        return [make_command("SaveScene",
                             path=_asset_path(path, "Assets/Scenes", ".unity") if path else None)]

    def create_prefab(self, match: re.Match) -> list[dict]:
        target = self.target(match.group("target"))
        path = match.group("path")
        leaf = target.split("::")[-1].split("/")[-1]
        return [make_command("CreatePrefab", target=target,
                             prefabPath=_asset_path(path or leaf, "Assets/Prefabs", ".prefab"))]

    def hierarchy(self, match: re.Match) -> list[dict]:
        return [make_command("GetHierarchy")]

    def ping(self, match: re.Match) -> list[dict]:
        return [make_command("Ping")]


def match_or_empty(match: re.Match | None) -> str:
    return match.group("rest") if match else ""


# Order matters: the first shape that fits wins, so the specific ones
# ("add a point light") come before the general ones ("add X to Y").
_ROUTES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:ping|is unity (?:up|alive|there))\??$", re.I), "ping"),
    (re.compile(r"^(?:create|add|make|place|spawn)\s+(?:a|an|the)?\s*(?:new\s+)?(?P<type>directional|point|spot|sun)?\s*(?:light|sun)\b(?P<rest>.*)$", re.I), "create_light"),
    (re.compile(r"^(?:create|add|make|place|spawn)\s+(?:a|an|the)?\s*(?:new\s+)?(?P<main>main\s+)?camera\b(?P<rest>.*)$", re.I), "create_camera"),
    (re.compile(r"^(?:create|add|make|spawn|place)\s+(?:a|an|the)?\s*(?:new\s+)?(?P<adj>floor|ground|wall|ceiling)?\s*(?P<kind>plane|cube|sphere|capsule|cylinder|quad|empty(?:\s+game\s*object)?|game\s*object|object)\b(?P<rest>.*)$", re.I), "create_object"),
    (re.compile(r"^(?:create|make|save)\s+(?:a\s+)?prefab\s+(?:from|of|for|out of)\s+(?:the\s+)?(?P<target>.+?)(?:\s+(?:at|to|as|in)\s+(?P<path>\S+))?$", re.I), "create_prefab"),
    (re.compile(r"^(?:add|attach|give)\s+(?:a|an|the)?\s*(?P<comp>[A-Za-z_][\w. ]*?)\s+(?:component\s+)?(?:to|on|onto)\s+(?P<target>.+)$", re.I), "add_component"),
    (re.compile(r"^(?:remove|detach|strip)\s+(?:the\s+)?(?:(?:a|an)\s+)?(?P<comp>[A-Za-z_][\w. ]*?)\s+(?:component\s+)?from\s+(?P<target>.+)$", re.I), "remove_component"),
    (re.compile(r"^(?:delete|remove|destroy)\s+(?:the\s+)?(?:game\s*object\s+)?(?P<target>.+)$", re.I), "delete_object"),
    (re.compile(r"^(?:move|place|put|position|teleport)\s+(?:the\s+)?(?P<target>.+?)\s+(?:to|at)\s+(?P<pos>.+)$", re.I), "move"),
    (re.compile(r"^(?:rotate|turn)\s+(?:the\s+)?(?P<target>.+?)\s+(?:to|by)\s+(?P<rot>.+)$", re.I), "rotate"),
    (re.compile(r"^(?:scale|resize)\s+(?:the\s+)?(?P<target>.+?)\s+(?:to|by)\s+(?P<scale>.+)$", re.I), "scale"),
    (re.compile(r"^set\s+(?:the\s+)?(?P<field>[\w ]+?)\s+(?:of|on)\s+(?:the\s+)?(?P<target>.+?)(?:'s)?\s+(?P<comp>[A-Za-z_][\w.]*)\s+to\s+(?P<value>.+)$", re.I), "set_field"),
    (re.compile(r"^set\s+(?:the\s+)?(?P<field>[\w ]+?)\s+(?:of|on)\s+(?:the\s+)?(?P<target>.+?)\s+to\s+(?P<value>.+)$", re.I), "set_field"),
    (re.compile(r"^set\s+(?:the\s+)?(?P<target>.+?)\s+(?P<comp>[A-Za-z_][\w.]*)\s+(?P<field>\w+)\s+to\s+(?P<value>.+)$", re.I), "set_field"),
    (re.compile(r"^(?P<verb>enable|disable|activate|deactivate|hide|show)\s+(?:the\s+)?(?P<target>.+)$", re.I), "set_active"),
    (re.compile(r"^(?:open|load)\s+(?:the\s+)?scene\s+(?P<path>.+)$", re.I), "open_scene"),
    (re.compile(r"^save\s+(?:the\s+)?(?:current\s+)?scene(?:\s+(?:as|to)\s+(?P<path>.+))?$", re.I), "save_scene"),
    (re.compile(r"^(?:list|show|describe|dump|get|print)\s+(?:the\s+)?(?:scene|hierarchy|objects|scene\s+objects)|^what(?:'s| is)\s+in\s+the\s+scene", re.I), "hierarchy"),
]


def parse_unity_command(description: str) -> list[dict]:
    """Map a plain description to bridge commands, without sending them.

    "Create a floor plane at y=0 and then add a box collider to it" becomes
    a CreateGameObject and an AddComponent. Raises UnroutableCommand for a
    clause no route recognises.
    """
    if not isinstance(description, str) or not description.strip():
        raise UnroutableCommand("An empty description maps to nothing.")

    router = _Router()
    commands: list[dict] = []
    for clause in _CLAUSE_SPLIT.split(description.strip()):
        clause = clause.strip().rstrip(".")
        if not clause:
            continue
        for pattern, handler in _ROUTES:
            match = pattern.match(clause)
            if match:
                commands.extend(getattr(router, handler)(match))
                break
        else:
            raise UnroutableCommand(
                f"Could not map {clause!r} to a Unity bridge command. Call the bridge "
                f"functions directly for anything the router does not recognise.")
    return commands


def run_unity_command(description: str, bridge: UnityEditorBridge | None = None, *,
                      wait: bool | None = None) -> list[CommandResult]:
    """Parse a description, send the commands, and return each command's result.

    Raises UnroutableCommand when the description does not map, and
    UnityBridgeError when the editor refuses a command. With wait off the
    list is empty: the commands are written and nobody has read them yet.
    """
    commands = parse_unity_command(description)
    results = (bridge or get_bridge()).send(commands, wait=wait)
    return list(results.results) if results is not None else []


# ======================================================
# Command line
# ======================================================

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aria.unity_editor_bridge",
        description="Send a plain-language command to the open Unity Editor.")
    parser.add_argument("description", nargs="*", help='e.g. "create a cube named Crate at 0,1,0"')
    parser.add_argument("--project", help=f"Unity project folder (default: ${ENV_PROJECT} or the workspace)")
    parser.add_argument("--install", action="store_true", help="copy the C# bridge into the project and exit")
    parser.add_argument("--no-wait", action="store_true", help="write the commands file and return at once")
    parser.add_argument("--timeout", type=float, help="seconds to wait for results")
    parser.add_argument("--headless", action="store_true", help="start a batch-mode editor instead of using an open one")
    parser.add_argument("--json", action="store_true", help="print the raw results as JSON")
    options = parser.parse_args(argv)

    try:
        bridge = UnityEditorBridge(options.project, timeout=options.timeout, wait=not options.no_wait)
        if options.install:
            print(f"installed {bridge.install_bridge()}")
            return 0

        description = " ".join(options.description).strip()
        if not description:
            parser.error("give a description, or --install")

        commands = parse_unity_command(description)
        if options.headless:
            results = bridge.send_headless(commands)
        else:
            results = bridge.send(commands)
    except (UnityBridgeError, UnroutableCommand, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if results is None:
        print(f"wrote {bridge.commands_path}; run ARIA > Run Bridge Commands in Unity")
        return 0

    if options.json:
        print(json.dumps(results.raw, indent=2))
    else:
        for result in results:
            print(f"{'ok  ' if result.success else 'FAIL'} {result.command}: {result.message}")
    return 0


if __name__ == "__main__":  # pragma: no cover - a doorway, not logic
    sys.exit(main())
