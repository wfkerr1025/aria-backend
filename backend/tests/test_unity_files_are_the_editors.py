"""A file Unity serialises is not a file to hand-write.

THE FAILURE THIS EXISTS FOR
---------------------------
Twice in one evening, both measured on the developer's machine. Asked to
change a scene:

    OpenScene("Assets/Scenes/SampleScene.unity")
    ...
    progress {"label":"writing Assets/Scenes/SampleScene.unity"}

and, an hour later, after the routing that was supposed to prevent it
had shipped:

    CreateCamera("MainCamera", position={"x":0,"y":1.6,"z":-3}, ...)
    ...
    progress {"label":"writing Assets/Scenes/WorkshopScene.unity"}

Both times nemo-12b took the only shape it recognised -- a file path --
and generated scene YAML for ninety seconds. Both times it ran out of
context before finishing, which is the only reason a real scene survived.

Routing is the fix and it is in. This is the FLOOR under the routing,
because both incidents share one step: a model held edit_file, saw a
.unity path, and nothing said no. A scene, prefab or asset carries GUIDs
and file ids only the editor can produce, so a generated one is not a
worse version of the file -- it is a different file wearing its name.

There is no correct way to write these by hand. That is why the check is
a suffix rather than a judgement a careful model could pass.
"""

from __future__ import annotations

import pytest

from backend.core import brief_plugins, file_tools
from backend.core.file_tools import UNITY_SERIALISED_SUFFIXES, WorkspaceError, edit_file
from backend.core.tool_brief import action_tool_brief
from backend.core.tool_orchestrator import run_answer_actions
from backend.plugins import unity_csharp


REAL_SCENE = (
    "%YAML 1.1\n"
    "%TAG !u! tag:unity3d.com,2011:\n"
    "--- !u!29 &1\n"
    "OcclusionCullingSettings:\n"
    "  m_ObjectHideFlags: 0\n"
    "  serializedVersion: 2\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    scenes = tmp_path / "Assets" / "Scenes"
    scenes.mkdir(parents=True)
    (scenes / "SampleScene.unity").write_text(REAL_SCENE, encoding="utf-8", newline="")
    return tmp_path


def block(payload: str) -> str:
    return f"```json\n{payload}\n```"


# ======================================================
# The refusal
# ======================================================

@pytest.mark.parametrize("suffix", sorted(UNITY_SERIALISED_SUFFIXES))
def test_every_serialised_format_is_refused(project, suffix):
    with pytest.raises(WorkspaceError, match="the Unity editor writes"):
        edit_file(f"Assets/Thing{suffix}", "anything", confirm=True)


def test_the_suffix_is_read_however_it_is_capitalised(project):
    with pytest.raises(WorkspaceError):
        edit_file("Assets/Scenes/Other.UNITY", "anything", confirm=True)


def test_it_is_refused_at_preview_too(project):
    """Before the diff, not just before the write.

    A diff of an invented scene against a real one is not something to
    show anyone, and by preview time the model has already spent the turn
    generating it.
    """
    with pytest.raises(WorkspaceError):
        edit_file("Assets/Scenes/SampleScene.unity", "anything", confirm=False)


def test_the_scene_on_disk_is_untouched(project):
    scene = project / "Assets" / "Scenes" / "SampleScene.unity"
    before = scene.read_bytes()

    with pytest.raises(WorkspaceError):
        edit_file("Assets/Scenes/SampleScene.unity", "invented YAML\n", confirm=True)

    assert scene.read_bytes() == before


def test_the_refusal_says_where_to_go_instead(project):
    with pytest.raises(WorkspaceError) as refused:
        edit_file("Assets/Scenes/SampleScene.unity", "x", confirm=True)

    said = str(refused.value)
    assert "Assets/Scenes/SampleScene.unity" in said
    assert "GUIDs" in said
    # A refusal with no next step in it is a dead end, and the model
    # will simply try again.
    assert "Ask the Unity editor" in said


# ======================================================
# Everything else still writes
# ======================================================

@pytest.mark.parametrize("path", [
    "Assets/Scripts/Player.cs",
    "Assets/Scripts/notes.md",
    "README.md",
    "tools/build.py",
    # Not a Unity format, and close enough to one to be worth pinning.
    "Assets/data.json",
    "Assets/shader.shader",
])
def test_an_ordinary_file_is_unaffected(project, path):
    (project / path).parent.mkdir(parents=True, exist_ok=True)
    result = edit_file(path, "content\n", confirm=True)

    assert result["applied"] is True
    assert (project / path).read_text(encoding="utf-8") == "content\n"


# ======================================================
# Through the action path a model actually reaches
# ======================================================

def test_an_action_block_targeting_a_scene_writes_nothing(project):
    scene = project / "Assets" / "Scenes" / "SampleScene.unity"
    before = scene.read_bytes()

    report = run_answer_actions(
        block('{"tool": "edit_file", "path": "Assets/Scenes/SampleScene.unity", '
              '"content": "%YAML 1.1\\n"}'),
        "yes, do it")

    assert report is not None
    assert report["results"][0]["status"] != "ok"
    assert scene.read_bytes() == before


# ======================================================
# And the model is told, so the turn is not spent first
# ======================================================

def unity_brief(prompt: str) -> str:
    brief_plugins.clear_plugins()
    try:
        unity_csharp.register()
        return action_tool_brief("nemo-12b-q5", prompt)
    finally:
        brief_plugins.clear_plugins()


def test_the_unity_brief_says_not_to_write_them():
    """The guard saves the file; this saves the ninety seconds.

    Refusing at edit_file happens AFTER the model has generated the whole
    file, so on its own the guard turns a destroyed scene into a wasted
    turn. The rule is what stops the turn being spent.
    """
    brief = unity_brief('open the scene Assets/Scenes/SampleScene.unity')

    assert ".unity" in brief
    assert ".prefab" in brief


def test_the_rule_only_arrives_when_the_prompt_looks_like_unity():
    """Stated rather than assumed, because it bounds what the rule does.

    The plugin activates on Unity's vocabulary and deliberately does not
    fire on generic words -- "scene" and "camera" belong to films and
    stories too, and a plugin that claimed them would push Unity idioms
    at someone writing prose. So a Unity request that never names Unity
    reaches a model without this rule, and the edit_file guard above is
    the only thing standing between it and the scene. That is the
    layering, and it is why the guard is not merely a nicety on top of
    the brief.
    """
    assert ".unity" not in unity_brief("open the scene and delete the camera")
