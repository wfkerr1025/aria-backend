"""Standing a cleaned model up in a Unity scene.

THE ORDER IS THE WHOLE DESIGN, AND IT IS NOT THE OBVIOUS ONE
------------------------------------------------------------
The sequence everybody writes down first is import, make a prefab,
place the prefab. This CLI cannot do that. PrefabCommands.cs takes
`source` as a SCENE object -- create_prefab saves something that is
already in a scene -- so the real order is:

    import_asset -> instantiate_prefab -> create_prefab -> set_transform

Getting that wrong produces a sequence that looks entirely reasonable
and fails on its second command, so the ordering test below is the one
that matters most here.

WHERE THESE SIGNATURES CAME FROM
--------------------------------
Not memory. The package's own C#, in the project:

    Assets/AssetCommands.cs        import_asset(source, path, confirm, dry_run)
    Prefabs/PrefabCommands.cs      create_prefab(source, path)
                                   instantiate_prefab(prefab, scene_path, name)
    GameObjects/GameObjectCommands.cs
                                   set_transform(target, position, rotation, scale)
    GameObjects/ComponentCommands.cs
                                   add_component(target, type)

NO UNITY RUNS HERE. `unity cmd` talks to a live Editor over the
Pipeline server, so run_invocation is replaced and what these tests
check is the argv that would have been sent.
"""

from __future__ import annotations

import pytest

from backend.unity import unity_delivery as delivery


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A Unity project on disk, with nothing in it yet."""
    root = tmp_path / "Project"
    (root / "Assets").mkdir(parents=True)
    monkeypatch.setattr(delivery, "unity_project_root", lambda: root)
    return root


@pytest.fixture
def fbx(tmp_path):
    path = tmp_path / "swordsman_unity.fbx"
    path.write_bytes(b"not really an fbx")
    return path


@pytest.fixture
def cli(monkeypatch):
    """Record every `unity cmd` and answer it the way the CLI does."""
    calls = []

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2)}
        calls.append({"command": command, "args": named})

        name = command.replace("cmd ", "")
        data = {"hierarchyPath": "/" + named.get("name", "Object"),
                "globalId": "GlobalObjectId_V1-2-abc-123-0"}
        if name == "create_prefab":
            data = {"assetPath": named.get("path")}
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "data": data, "errors": []}}

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls


def commands_of(calls):
    return [c["command"].replace("cmd ", "") for c in calls]


def args_of(calls, command):
    for call in calls:
        if call["command"] == f"cmd {command}":
            return call["args"]
    return {}


# ======================================================
# The happy path
# ======================================================

def test_it_imports_places_prefabs_then_transforms(project, fbx, cli):
    """The order create_prefab actually supports."""
    delivery.place_character(str(fbx))

    assert commands_of(cli) == [
        "import_asset", "instantiate_prefab", "create_prefab", "set_transform"]


def test_the_prefab_is_made_from_the_scene_object_not_the_asset(project, fbx, cli):
    """create_prefab's `source` is a scene object. Handing it the
    imported asset path is the mistake this whole ordering exists to
    avoid."""
    delivery.place_character(str(fbx), name="Hero")

    assert args_of(cli, "create_prefab")["source"] == "GlobalObjectId_V1-2-abc-123-0"
    assert args_of(cli, "create_prefab")["path"] == \
        "Assets/ARIA/Prefabs/Hero.prefab"


def test_the_prefab_path_is_reported(project, fbx, cli):
    answer = delivery.place_character(str(fbx))

    assert answer["success"] is True
    assert answer["prefab_path"] == \
        "Assets/ARIA/Prefabs/swordsman_unity.prefab"
    assert answer["asset_path"] == "Assets/ARIA/swordsman_unity.fbx"


def test_it_stands_on_the_ground_at_scale_one(project, fbx, cli):
    """y=0 only works because Blender's cleanup put the origin at the
    model's feet. Scale is 1 because that same stage normalised the
    height already."""
    answer = delivery.place_character(str(fbx))

    assert answer["position"] == (0.0, 0.0, 0.0)
    assert answer["scale"] == (1.0, 1.0, 1.0)
    assert args_of(cli, "set_transform")["position"] == "[0,0,0]"
    assert args_of(cli, "set_transform")["scale"] == "[1,1,1]"


def test_it_faces_forward_by_default(project, fbx, cli):
    answer = delivery.place_character(str(fbx))

    assert answer["rotation"] == (0.0, 0.0, 0.0)
    assert args_of(cli, "set_transform")["rotation"] == "[0,0,0]"


def test_a_given_position_is_used(project, fbx, cli):
    answer = delivery.place_character(str(fbx), position=(3, 0, -1.5))

    assert args_of(cli, "set_transform")["position"] == "[3,0,-1.5]"
    assert answer["position"] == (3, 0, -1.5)


def test_the_instance_is_named_by_global_id_not_hierarchy_path(project, fbx, cli):
    """AuthoringResult carries several identities and _object_ref picks
    globalId first, because a hierarchy path stops being true the
    moment somebody renames or reparents the object -- and a pipeline
    that places ten characters does exactly that kind of thing between
    one command and the next.
    """
    delivery.place_character(str(fbx), name="Hero")

    target = args_of(cli, "set_transform")["target"]
    assert target.startswith("GlobalObjectId")
    assert target != "/Hero"


def test_the_transform_targets_the_instance(project, fbx, cli):
    delivery.place_character(str(fbx), name="Hero")

    assert args_of(cli, "set_transform")["target"] == "GlobalObjectId_V1-2-abc-123-0"


def test_the_import_source_is_absolute_and_the_destination_relative(
        project, fbx, cli):
    """import_asset takes an absolute filesystem path for source and an
    Assets/-relative one for path. Swapping them is silent."""
    delivery.place_character(str(fbx))

    arguments = args_of(cli, "import_asset")
    assert arguments["source"] == str(fbx.resolve())
    assert arguments["path"].startswith("Assets/")


def test_without_a_global_id_the_hierarchy_path_is_used(project, fbx, monkeypatch):
    """Older results, or a command that answers with less, still have
    to chain -- ObjectRef resolves a hierarchy path too."""
    from backend.unity import unity_cli_engine

    def fake(command, args=None, **kwargs):
        return {"success": True, "output": "", "error": None,
                "json": {"success": True,
                         "data": {"hierarchyPath": "/Hero"}, "errors": []}}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    answer = delivery.place_character(str(fbx), name="Hero")

    assert answer["instance"] == "/Hero"


# ======================================================
# The scene
# ======================================================

def test_no_scene_means_the_active_one(project, fbx, cli):
    """instantiate_prefab defaults to the active scene, so scene_path
    is left off rather than guessed at."""
    answer = delivery.place_character(str(fbx))

    assert "scene_path" not in args_of(cli, "instantiate_prefab")
    assert answer["scene_path"] == "the active scene"


def test_a_named_scene_is_passed_through(project, fbx, cli):
    answer = delivery.place_character(
        str(fbx), scene_path="Assets/Scenes/Forest.unity")

    assert args_of(cli, "instantiate_prefab")["scene_path"] == \
        "Assets/Scenes/Forest.unity"
    assert answer["scene_path"] == "Assets/Scenes/Forest.unity"


# ======================================================
# Components
# ======================================================

def test_a_collider_is_added_when_asked_for(project, fbx, cli):
    answer = delivery.place_character(str(fbx), collider="capsule")

    assert args_of(cli, "add_component")["type"] == "CapsuleCollider"
    assert answer["components"] == ["CapsuleCollider"]


def test_a_friendly_collider_name_becomes_the_unity_one(project, fbx, cli):
    for asked, expected in delivery.COLLIDERS.items():
        calls = []
        cli.clear()
        delivery.place_character(str(fbx), collider=asked, name=f"x{asked}")
        assert args_of(cli, "add_component")["type"] == expected


def test_a_unity_type_name_is_passed_through_untouched(project, fbx, cli):
    delivery.place_character(str(fbx), collider="CharacterController")

    assert args_of(cli, "add_component")["type"] == "CharacterController"


def test_a_rigidbody_is_added_when_asked_for(project, fbx, cli):
    answer = delivery.place_character(str(fbx), rigidbody=True)

    assert "Rigidbody" in answer["components"]


def test_both_a_collider_and_a_rigidbody(project, fbx, cli):
    answer = delivery.place_character(
        str(fbx), collider="box", rigidbody=True)

    assert answer["components"] == ["BoxCollider", "Rigidbody"]
    assert commands_of(cli).count("add_component") == 2


def test_nothing_is_added_when_nothing_is_asked_for(project, fbx, cli):
    delivery.place_character(str(fbx))

    assert "add_component" not in commands_of(cli)


def test_components_target_the_instance(project, fbx, cli):
    delivery.place_character(str(fbx), name="Hero", collider="capsule")

    assert args_of(cli, "add_component")["target"] == "GlobalObjectId_V1-2-abc-123-0"


# ======================================================
# Running it twice
# ======================================================

def test_an_existing_prefab_is_reused_not_rebuilt(project, fbx, cli):
    """A second call should add a second instance, not re-import the
    model and overwrite the prefab somebody may have edited."""
    prefab = project / "Assets" / "ARIA" / "Prefabs"
    prefab.mkdir(parents=True)
    (prefab / "swordsman_unity.prefab").write_text("prefab")

    answer = delivery.place_character(str(fbx))

    assert answer["reused"] is True
    assert commands_of(cli) == ["instantiate_prefab", "set_transform"]


def test_the_reused_prefab_is_the_one_instantiated(project, fbx, cli):
    prefab = project / "Assets" / "ARIA" / "Prefabs"
    prefab.mkdir(parents=True)
    (prefab / "swordsman_unity.prefab").write_text("prefab")

    delivery.place_character(str(fbx))

    assert args_of(cli, "instantiate_prefab")["prefab"] == \
        "Assets/ARIA/Prefabs/swordsman_unity.prefab"


def test_an_existing_asset_is_not_imported_again(project, fbx, cli):
    """The asset is there but no prefab is, so it still gets placed and
    prefabbed -- just not re-imported."""
    models = project / "Assets" / "ARIA"
    models.mkdir(parents=True)
    (models / "swordsman_unity.fbx").write_text("already here")

    answer = delivery.place_character(str(fbx))

    assert "import_asset" not in commands_of(cli)
    assert "create_prefab" in commands_of(cli)
    assert answer["reused"] is False


def test_reuse_can_be_switched_off(project, fbx, cli):
    prefab = project / "Assets" / "ARIA" / "Prefabs"
    prefab.mkdir(parents=True)
    (prefab / "swordsman_unity.prefab").write_text("prefab")

    delivery.place_character(str(fbx), reuse=False)

    assert "import_asset" in commands_of(cli)


def test_overwriting_confirms_because_the_command_requires_it(
        project, fbx, cli):
    """import_asset takes confirm only for an overwrite and refuses
    without it. It is sent when replacing and never otherwise, so the
    guard keeps working."""
    models = project / "Assets" / "ARIA"
    models.mkdir(parents=True)
    (models / "swordsman_unity.fbx").write_text("already here")

    delivery.place_character(str(fbx), overwrite=True)

    assert args_of(cli, "import_asset")["confirm"] == "true"


def test_a_first_import_does_not_send_confirm(project, fbx, cli):
    delivery.place_character(str(fbx))

    assert "confirm" not in args_of(cli, "import_asset")


# ======================================================
# When it goes wrong
# ======================================================

def _fails(monkeypatch, failing, message):
    from backend.unity import unity_cli_engine

    def fake(command, args=None, **kwargs):
        name = command.replace("cmd ", "")
        if name == failing:
            return {"success": False, "output": "", "json": None,
                    "error": message}
        return {"success": True, "output": "", "error": None,
                "json": {"success": True,
                         "data": {"hierarchyPath": "/Object"}, "errors": []}}

    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)


def test_a_failed_import_says_so(project, fbx, monkeypatch):
    _fails(monkeypatch, "import_asset", "disk full")

    answer = delivery.place_character(str(fbx))

    assert answer["success"] is False
    assert "could not import" in answer["error"]
    assert "disk full" in answer["error"]


def test_a_failed_instantiate_says_so(project, fbx, monkeypatch):
    _fails(monkeypatch, "instantiate_prefab", "No valid loaded scene")

    answer = delivery.place_character(str(fbx))

    assert answer["success"] is False
    assert "could not place" in answer["error"]
    assert "No valid loaded scene" in answer["error"]


def test_a_failed_prefab_says_the_object_is_still_in_the_scene(
        project, fbx, monkeypatch):
    """It got placed. Saying only "prefab failed" would leave somebody
    hunting for a GameObject they were not told about."""
    _fails(monkeypatch, "create_prefab", "path is not under Assets")

    answer = delivery.place_character(str(fbx))

    assert answer["success"] is False
    assert "placed" in answer["error"] and "could not save a prefab" in answer["error"]
    assert answer["steps"] == ["import_asset", "instantiate_prefab"]


def test_no_editor_running_is_reported_as_itself(project, fbx, monkeypatch):
    """The CLI's own message names the cause and the fix. Rewording it
    into something vaguer helps nobody."""
    _fails(monkeypatch, "import_asset",
           "No Pipeline instance found for project: D:/Project. Make sure "
           "Unity Editor is running with the Pipeline package installed.")

    answer = delivery.place_character(str(fbx))

    assert "Unity Editor is running" in answer["error"]


def test_a_component_that_will_not_attach_is_a_warning_not_a_failure(
        project, fbx, monkeypatch):
    """The character is in the scene and standing up. A missing collider
    is worth saying, not worth calling the whole placement failed."""
    _fails(monkeypatch, "add_component", "Could not resolve component type")

    answer = delivery.place_character(str(fbx), collider="capsule")

    assert answer["success"] is True
    assert any("could not add" in w for w in answer["warnings"])
    assert answer["components"] == []


# ======================================================
# Refusals, before Unity is asked anything
# ======================================================

def test_a_file_that_is_not_there_is_refused(project, tmp_path, cli):
    answer = delivery.place_character(str(tmp_path / "nothing.fbx"))

    assert answer["success"] is False
    assert answer["ran"] is False
    assert cli == []


def test_no_file_at_all_is_refused(project, cli):
    answer = delivery.place_character("")

    assert answer["success"] is False
    assert cli == []


def test_no_configured_project_is_refused(fbx, cli, monkeypatch):
    monkeypatch.setattr(delivery, "unity_project_root", lambda: None)

    answer = delivery.place_character(str(fbx))

    assert answer["success"] is False
    assert "No Unity project is configured" in answer["error"]
    assert cli == []


# ======================================================
# What the answer says
# ======================================================

def test_it_warns_that_there_is_no_animator(project, fbx, cli):
    """A model from Ludo has no armature, so nothing can drive an
    Animator yet. Saying so is how the pipeline knows rigging is still
    outstanding."""
    answer = delivery.place_character(str(fbx))

    assert any("Animator" in w for w in answer["warnings"])


def test_it_warns_when_there_is_no_collider(project, fbx, cli):
    answer = delivery.place_character(str(fbx))

    assert any("collider" in w for w in answer["warnings"])


def test_asking_for_a_collider_removes_that_warning(project, fbx, cli):
    answer = delivery.place_character(str(fbx), collider="capsule")

    assert not any("no collider" in w for w in answer["warnings"])


def test_the_answer_lists_what_it_did(project, fbx, cli):
    answer = delivery.place_character(str(fbx), collider="box")

    assert answer["steps"] == ["import_asset", "instantiate_prefab",
                               "create_prefab", "set_transform",
                               "add_component:BoxCollider"]


def test_the_answer_names_the_instance(project, fbx, cli):
    answer = delivery.place_character(str(fbx), name="Hero")

    assert answer["name"] == "Hero"
    assert answer["instance"]


# ======================================================
# The vector spelling, which is the one unverified piece
# ======================================================

def test_a_vector_is_sent_as_a_json_array():
    """The C# takes float[] and the schema generator calls it an array.
    The binder that turns argv into JSON lives in the CLI executable,
    which cannot be asked without a running Editor -- so this pins the
    spelling in one place, and one place is where it changes if it is
    wrong."""
    assert delivery._vector_arg((0, 0, 0)) == "[0,0,0]"
    assert delivery._vector_arg((1.5, -2, 0.25)) == "[1.5,-2,0.25]"


def test_a_vector_never_carries_spaces():
    """It travels as one argv entry. A space would split it into
    several and the command would see a malformed array."""
    assert " " not in delivery._vector_arg((1.5, 2.5, 3.5))
