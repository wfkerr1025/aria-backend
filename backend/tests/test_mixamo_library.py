"""Using Mixamo clips instead of generated motion.

WHY THIS ROUTE EXISTS
---------------------
Ludo's generated motion was built, paid for and measured, and it is
not usable for locomotion. Read off the source curves, before any of
our code touches them:

    frame    LeftThigh    RightThigh
       6       -29.2        -20.6      same direction
      24       -29.2        -20.6      same direction

Both legs swing the same way at the same time, at every frame. That is
a shuffle, not a walk. A second generation with a more prescriptive
prompt came back with 6 moving curves out of 180 -- effectively
static.

The same measurement on the Mixamo clip, retargeted onto the same
character through the Avatar system:

    leg split 17.1 to 61.4 degrees -- a range of 44.3
    against the generated clip's 6.1

MOST OF THIS SUITE NEEDS NO UNITY. Matching a folder of clips to a set
of states is ordinary string work, and it is where the mistakes are:
"walk" matches both Walking and Crouched Walking, "run" matches both
Running and Running Jump.
"""

from __future__ import annotations

import pytest

from backend.unity import mixamo_library as mixamo
from backend.unity import unity_delivery as delivery


def library(*names):
    """A folder of clips, as discover() reports one."""
    return [{"asset": f"Assets/ARIA/Animations/Mixamo/X Bot@{name}.fbx",
             "clip": name, "length": 1.0, "import_type": "Human"}
            for name in names]


# The seventeen actually downloaded, for the matcher to choose from.
DOWNLOADED = library(
    "Crouched Walking", "Crouching Idle", "Falling Flat Impact",
    "Falling Idle", "Falling To Landing", "Falling To Roll", "Falling",
    "Hard Landing", "Jump", "Jumping", "Landing", "Running Jump",
    "Running", "Standing Dodge Left", "Standing Dodge Right",
    "Standing Idle", "Walking")


@pytest.fixture
def cli(monkeypatch):
    calls = []
    state = {"result": "", "fail": None}

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        calls.append({"command": name, "args": named})
        if name == state["fail"]:
            return {"success": False, "output": "", "json": None,
                    "error": "refused"}
        result = ({"success": True, "result": state["result"]}
                  if name == "eval" else
                  {"applied": ["animationType", "avatarSetup"], "unknown": []})
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": name, "parameters": named,
                                  "result": result, "target": {},
                                  "success": True}}}

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls, state


# ======================================================
# Matching, which is where the mistakes are
# ======================================================

def test_walking_beats_crouched_walking():
    """Both contain "walk". A similarity score would pick between them
    by accident, and the accident would change the next time somebody
    downloads one more clip."""
    found = mixamo.match_states(DOWNLOADED, ["Walk"])

    assert found["matched"]["Walk"]["clip"] == "Walking"


def test_running_beats_running_jump():
    found = mixamo.match_states(DOWNLOADED, ["Run"])

    assert found["matched"]["Run"]["clip"] == "Running"


def test_standing_idle_beats_crouching_and_falling_idle():
    found = mixamo.match_states(DOWNLOADED, ["Idle"])

    assert found["matched"]["Idle"]["clip"] == "Standing Idle"


def test_the_three_locomotion_states_all_resolve():
    found = mixamo.match_states(DOWNLOADED, ["Idle", "Walk", "Run"])

    assert {s: e["clip"] for s, e in found["matched"].items()} == {
        "Idle": "Standing Idle", "Walk": "Walking", "Run": "Running"}


def test_a_state_with_no_clip_is_reported_not_invented():
    """The seventeen downloaded contain no attack. Quietly leaving the
    state empty would produce a character that plays nothing when it
    attacks, and nobody would know why."""
    found = mixamo.match_states(DOWNLOADED, ["Idle", "Walk", "Run", "Attack"])

    assert found["missing"] == ["Attack"]
    assert "Attack" not in found["matched"]


def test_what_was_not_used_is_listed():
    """So somebody can see there are fourteen more clips sitting there
    and decide whether the graph should have more states."""
    found = mixamo.match_states(DOWNLOADED, ["Idle", "Walk", "Run"])

    assert len(found["unused"]) == 14
    assert any(e["clip"] == "Jump" for e in found["unused"])


def test_one_clip_is_never_used_for_two_states():
    """Two states that genuinely compete for the same file. Walk and
    Crouch do not -- they resolve to different clips anyway -- so this
    aliases a second state onto Walking on purpose, which is the only
    way the guard is actually exercised."""
    found = mixamo.match_states(
        DOWNLOADED, ["Walk", "Stroll"],
        aliases={"Stroll": ("walking",)})

    assets = [e["asset"] for e in found["matched"].values()]
    assert len(assets) == len(set(assets)), "one clip served two states"
    assert found["matched"]["Walk"]["clip"] == "Walking"
    assert found["matched"].get("Stroll", {}).get("clip") != "Walking"


def test_aliases_can_be_overridden():
    """A project that calls its states something else, or a folder
    named differently, should not need this module changed."""
    found = mixamo.match_states(DOWNLOADED, ["Sneak"],
                                aliases={"Sneak": ("crouched walking",)})

    assert found["matched"]["Sneak"]["clip"] == "Crouched Walking"


def test_an_empty_folder_matches_nothing():
    found = mixamo.match_states([], ["Idle", "Walk"])

    assert found["matched"] == {}
    assert found["missing"] == ["Idle", "Walk"]


def test_matching_is_on_the_clip_name_not_the_filename():
    """Mixamo files are "X Bot@Walking.fbx" -- matching the filename
    would match "Bot" against everything."""
    odd = [{"asset": "Assets/whatever.fbx", "clip": "Walking",
            "length": 1.0, "import_type": "Human"}]

    assert mixamo.match_states(odd, ["Walk"])["matched"]["Walk"]["clip"] == \
        "Walking"


# ======================================================
# Discovery
# ======================================================

def test_discovery_reads_what_unity_has_imported(cli):
    calls, state = cli
    state["result"] = ("Assets/A.fbx\tWalking\t1.03\tHuman\n"
                       "Assets/B.fbx\tRunning\t0.63\tGeneric\n")

    found = mixamo.discover()

    assert [e["clip"] for e in found] == ["Walking", "Running"]
    assert found[0]["length"] == 1.03
    assert found[1]["import_type"] == "Generic"


def test_discovery_refreshes_first(cli):
    """Files dropped into the folder while the Editor is open are not
    in the AssetDatabase until it looks."""
    calls, state = cli
    mixamo.discover()

    assert "AssetDatabase.Refresh" in calls[0]["args"]["code"]


def test_a_folder_with_nothing_in_it_is_not_an_error(cli):
    calls, state = cli
    state["result"] = ""

    assert mixamo.discover() == []


def test_a_failed_listing_returns_nothing_rather_than_raising(cli):
    calls, state = cli
    state["fail"] = "eval"

    assert mixamo.discover() == []


# ======================================================
# Humanoid, which is what makes retargeting work
# ======================================================

def test_both_the_clip_and_the_character_need_humanoid():
    """This route needs Humanoid where the generated-curve route
    needed Generic, and getting that backwards is a character that
    stands perfectly still."""
    assert mixamo.HUMANOID_IMPORT["animationType"] == "Human"
    assert mixamo.HUMANOID_IMPORT["avatarSetup"] == "CreateFromThisModel"


def test_configuring_sets_every_clip(cli):
    calls, _ = cli
    result = mixamo.configure_humanoid(["Assets/A.fbx", "Assets/B.fbx"])

    assert result["success"] is True
    assert len(result["configured"]) == 2
    assert [c["command"] for c in calls] == ["set_import_settings"] * 2


def test_a_clip_that_will_not_configure_is_reported(cli):
    calls, state = cli
    state["fail"] = "set_import_settings"

    result = mixamo.configure_humanoid(["Assets/A.fbx"])

    assert result["success"] is False
    assert result["failures"]


# ======================================================
# Assigning
# ======================================================

def test_assigning_finds_the_clip_inside_the_fbx(cli):
    """add_animator_state takes an ObjectRef, and an ObjectRef resolves
    an FBX path to the model's GameObject. The AnimationClip is a
    SUB-asset and there is no path syntax for it, so the clip is found
    by walking LoadAllAssetsAtPath -- which is what the Editor does."""
    calls, state = cli
    state["result"] = "Walk=Walking:human;"

    result = mixamo.assign_clips("Assets/X.controller",
                                 {"Walk": DOWNLOADED[-1]})

    assert result["success"] is True
    assert result["assigned"] == {"Walk": "Walking"}
    assert "LoadAllAssetsAtPath" in calls[0]["args"]["code"]


def test_a_generic_clip_is_flagged_because_it_will_not_retarget(cli):
    """It assigns perfectly and then does nothing, which is the worst
    shape of failure available here."""
    calls, state = cli
    state["result"] = "Walk=Walking:generic;"

    result = mixamo.assign_clips("Assets/X.controller",
                                 {"Walk": DOWNLOADED[-1]})

    assert result["non_humanoid"] == ["Walk (Walking)"]


def test_a_missing_controller_says_so(cli):
    calls, state = cli
    state["result"] = "NO_CONTROLLER"

    result = mixamo.assign_clips("Assets/X.controller",
                                 {"Walk": DOWNLOADED[-1]})

    assert result["success"] is False
    assert "no controller" in result["error"]


def test_assigning_nothing_is_not_a_success(cli):
    result = mixamo.assign_clips("Assets/X.controller", {})

    assert result["success"] is False


def test_the_generated_pairs_use_single_braces():
    """The pairs are inserted AFTER _ASSIGN.format(), so they must not
    carry the brace doubling that template needs. They did once, and
    the C# failed to compile."""
    import inspect
    source = inspect.getsource(mixamo.assign_clips)

    assert '{"%s", "%s"}' in source
    assert '{{"%s", "%s"}}' not in source
