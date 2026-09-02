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


INSTANCE_ID = "GlobalObjectId_V1-2-abc-123-0"

# Whether the imported model behaves like one Unity built an Avatar
# for. A humanoid model prefab arrives with an Animator on its root,
# which is why ensure_component exists.
PREFAB_IS_HUMANOID = {"value": True}


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
    """The Unity CLI, modelled on what a real Editor sent back.

    Every shape here was corrected against a live run, and each
    correction was a bug the previous fake had agreed with:

      * the AuthoringResult is nested at data["result"], inside an
        envelope that repeats the command and its parameters. The first
        fake put the identity at data[...] and so did the code, so the
        tests passed and a real Editor returned an instance of "";

      * a model imported as Human ALREADY has an Animator, and asking
        for another is refused. add_component failing does not mean the
        component is absent;

      * eval answers with the value its C# returned, under
        data["result"]["result"].
    """
    calls = []
    components = {}

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        calls.append({"command": name, "args": named, "argv": argv})

        def envelope(result, ok=True):
            return {"success": ok, "output": "", "error": None,
                    "json": {"success": ok, "errors": [],
                             "data": {"command": name, "parameters": named,
                                      "result": result, "target": {},
                                      "success": ok}}}

        if name == "eval":
            # The C# returns the prefab path it wrote.
            code = named.get("code", "")
            written = ""
            for piece in code.split('"'):
                if piece.endswith(".prefab"):
                    written = piece
                    break
            return envelope({"success": True, "result": written,
                             "diagnostics": [], "error": None})

        if name == "set_import_settings":
            return envelope({"assetPath": named.get("asset"),
                             "importerType": "ModelImporter",
                             "applied": ["animationType", "avatarSetup"],
                             "unknown": []})

        if name == "get_component_properties":
            target, wanted = named.get("target"), named.get("type")
            if wanted in components.get(target, set()):
                return envelope({"type": wanted, "properties": {}})
            return {"success": False, "output": "", "json": None,
                    "error": f"no {wanted} on {target}"}

        if name == "add_component":
            target, wanted = named.get("target"), named.get("type")
            held = components.setdefault(target, set())
            if wanted in held:
                return {"success": False, "output": "", "json": None,
                        "error": f"Failed to add component '{wanted}' "
                                 "(it may be disallowed on this GameObject)."}
            held.add(wanted)
            return envelope({"type": wanted, "globalId": INSTANCE_ID})

        if name == "instantiate_prefab":
            target = INSTANCE_ID
            # A humanoid model prefab arrives with an Animator already.
            components.setdefault(target, set()).update(
                {"Animator"} if PREFAB_IS_HUMANOID["value"] else set())
            return envelope({"globalId": target, "assetPath": None,
                             "guid": None, "fileId": None,
                             "instanceId": -4124,
                             "hierarchyPath": "/" + named.get("name", "Object"),
                             "type": "GameObject"})

        return envelope({"globalId": INSTANCE_ID, "assetPath": named.get("path"),
                         "hierarchyPath": "/" + named.get("name", "Object"),
                         "type": "GameObject"})

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls


def commands_of(calls):
    return [c["command"] for c in calls]


def args_of(calls, command):
    for call in calls:
        if call["command"] == command:
            return call["args"]
    return {}


# ======================================================
# The happy path
# ======================================================

def test_it_imports_places_prefabs_then_transforms(project, fbx, cli):
    """The order create_prefab actually supports."""
    delivery.place_character(str(fbx))

    assert commands_of(cli) == [
        "import_asset", "eval", "instantiate_prefab", "set_transform"]


def test_the_prefab_is_made_from_the_model_before_anything_is_placed(
        project, fbx, cli):
    """The documented sequence -- import, instantiate, create_prefab --
    cannot work. instantiate_prefab refuses any asset whose path does
    not end in ".prefab", so an FBX never gets into the scene, and
    create_prefab needs a scene object that therefore never exists.
    A live Editor answered "is not a prefab asset". So eval bridges
    the gap and the prefab exists BEFORE anything is instantiated."""
    delivery.place_character(str(fbx), name="Hero")

    order = commands_of(cli)
    assert order.index("eval") < order.index("instantiate_prefab")
    assert "Assets/ARIA/Prefabs/Hero.prefab" in args_of(cli, "eval")["code"]
    assert args_of(cli, "instantiate_prefab")["prefab"] == \
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
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": "x", "parameters": {},
                                  "result": {"hierarchyPath": "/Hero",
                                             "result": named.get("code", "")
                                             and "Assets/ARIA/Prefabs/Hero.prefab"},
                                  "target": {}, "success": True}}}

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
    assert "eval" in commands_of(cli), "it skipped making the prefab"
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
    """Every command succeeds except one.

    The other answers still have to be truthful. eval must return the
    prefab path its C# was asked to write, or model_to_prefab fails
    first and the test injects its failure into a step that is never
    reached. get_component_properties must say NO, or ensure_component
    decides a component that failed to attach was already there.
    """
    from backend.unity import unity_cli_engine

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        if name == failing:
            return {"success": False, "output": "", "json": None,
                    "error": message}
        if name == "get_component_properties":
            return {"success": False, "output": "", "json": None,
                    "error": "not present"}
        if name == "eval":
            written = ""
            for piece in named.get("code", "").split('"'):
                if piece.endswith(".prefab"):
                    written = piece
                    break
            return {"success": True, "output": "", "error": None,
                    "json": {"success": True, "errors": [],
                             "data": {"command": name, "parameters": named,
                                      "result": {"success": True,
                                                 "result": written},
                                      "target": {}, "success": True}}}
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": name, "parameters": {},
                                  "result": {"globalId": INSTANCE_ID,
                                             "hierarchyPath": "/Object",
                                             "result": "x.prefab"},
                                  "target": {}, "success": True}}}

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


def test_a_failed_prefab_stops_before_the_scene_is_touched(
        project, fbx, monkeypatch):
    """The prefab is made first now, so a failure there means nothing
    was placed and there is no stray GameObject to hunt for."""
    _fails(monkeypatch, "eval", "Compilation Failed")

    answer = delivery.place_character(str(fbx))

    assert answer["success"] is False
    assert answer["steps"] == ["import_asset"]


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

    assert answer["steps"] == ["import_asset", "create_prefab",
                               "instantiate_prefab", "set_transform",
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


# ======================================================
# What a live Editor found that every fake had agreed with
#
# The whole Unity layer was written and tested against mocks. The
# first run against a real Editor found four things, and each was a
# case of the test encoding the same assumption as the code -- so no
# amount of test-writing would have caught them.
# ======================================================

def test_every_command_asks_for_json(project, fbx, cli):
    """WITHOUT --json THE CLI ANSWERS WITH A TABLE.

    "Command<tab>Success<tab>Result<tab>Parameters" parses as no JSON,
    so every globalId and hierarchyPath was dropped before anything
    could read it. instantiate_prefab reported success and handed back
    an empty instance; set_transform then said it needed an object to
    move. The fakes had always returned parsed JSON, so this could not
    have failed in a test.
    """
    delivery.place_character(str(fbx))

    for call in cli:
        assert "--json" in call["argv"], call["command"]


def test_the_authoring_result_is_read_from_inside_the_envelope():
    """The server answers {"data": {"command", "parameters", "result",
    "target"}} and the identity lives in that inner `result`. Reading
    `data` itself finds nothing."""
    envelope = {"command": "instantiate_prefab", "parameters": {},
                "result": {"globalId": "GID", "hierarchyPath": "/X"},
                "target": {}, "success": True}

    assert delivery._result_of(envelope) == {"globalId": "GID",
                                             "hierarchyPath": "/X"}


def test_something_that_is_not_an_envelope_is_left_alone():
    assert delivery._result_of({"globalId": "GID"}) == {"globalId": "GID"}
    assert delivery._result_of(None) is None


def test_a_component_that_is_already_there_is_not_a_failure(project, fbx, cli):
    """A model imported as Human arrives with an Animator on its root,
    and Unity allows only one. The real error is "Failed to add
    component 'Animator' ... it may be disallowed on this GameObject",
    and reporting that as a failure says the character cannot be
    animated at exactly the moment it can."""
    answer = delivery.place_character(str(fbx), components=["Animator"])

    assert answer["success"] is True
    assert "Animator" in answer["components"]
    assert "already_had:Animator" in answer["steps"]


def test_a_component_that_is_genuinely_absent_still_fails(
        project, fbx, monkeypatch):
    _fails(monkeypatch, "add_component", "no such type")

    answer = delivery.place_character(str(fbx), collider="capsule")

    assert answer["components"] == []
    assert any("could not add" in w for w in answer["warnings"])


def test_the_generated_csharp_has_no_backslashes():
    """Path.GetDirectoryName returns them on Windows and every layer
    between here and Roslyn escapes them differently -- the first
    version reached the compiler as Replace("\\", "/") and failed with
    "Newline in constant". The folder is worked out in Python instead."""
    code = delivery._MODEL_TO_PREFAB.format(
        model="Assets/ARIA/a.fbx", prefab="Assets/ARIA/Prefabs/a.prefab",
        folder="Assets/ARIA/Prefabs", parent="Assets/ARIA", leaf="Prefabs")

    assert chr(92) not in code


def test_the_prefab_folder_is_worked_out_in_python(project, fbx, cli):
    delivery.place_character(str(fbx), name="Hero")

    code = args_of(cli, "eval")["code"]
    assert '"Assets/ARIA/Prefabs"' in code
    assert '"Assets/ARIA", "Prefabs"' in code
