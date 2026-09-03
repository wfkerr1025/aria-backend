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


# ======================================================
# Looping, which is what "the clips didn't play very long" was
#
# Every Mixamo clip imports with loopTime FALSE. Measured on the ones
# downloaded here: Walking is 31 frames and Running is 19, so they
# play for about a second and stop dead. An attack must NOT loop -- a
# one-shot that repeats is a character swinging a sword forever, and
# the Attack state leaves on exit time, which never arrives if the
# clip restarts.
# ======================================================

def test_the_cycles_loop_and_the_one_shots_do_not():
    assert "Walk" in mixamo.LOOPING_STATES
    assert "Run" in mixamo.LOOPING_STATES
    assert "Idle" in mixamo.LOOPING_STATES
    assert "Attack" in mixamo.ONE_SHOT_STATES
    assert "Attack" not in mixamo.LOOPING_STATES


def test_looping_is_set_per_state(cli):
    calls, state = cli
    state["result"] = "Walking=True"

    result = mixamo.configure_looping({"Walk": DOWNLOADED[-1]})

    assert result["looping"] == {"Walk": True}
    assert "true" in calls[0]["args"]["code"]


def test_an_attack_is_turned_off_rather_than_left(cli):
    """Left alone it would inherit whatever the importer chose, and
    the importer chooses False -- correct by luck rather than by
    intent, and luck changes."""
    calls, state = cli
    state["result"] = "Slash=False"
    attack = dict(DOWNLOADED[0], clip="Sword And Shield Slash")

    result = mixamo.configure_looping({"Attack": attack})

    assert result["looping"] == {"Attack": False}
    assert "false" in calls[0]["args"]["code"]


def test_a_state_in_neither_list_is_left_as_imported(cli):
    """Whether an unfamiliar motion should repeat is not something to
    decide from its name."""
    calls, state = cli
    state["result"] = "X=True"

    result = mixamo.configure_looping({"Cartwheel": DOWNLOADED[0]})

    assert result["left_alone"] == ["Cartwheel"]
    assert calls == []


def test_the_whole_clip_list_is_rewritten_not_one_element(cli):
    """clipAnimations is a struct array: assigning a single element
    edits a copy and changes nothing. The defaults are copied in,
    edited, and assigned back."""
    calls, state = cli
    state["result"] = "Walking=True"
    mixamo.configure_looping({"Walk": DOWNLOADED[-1]})

    code = calls[0]["args"]["code"]
    assert "defaultClipAnimations" in code
    assert "SaveAndReimport" in code
    # The assignment must be LIVE, not commented out. An earlier
    # version of this assertion passed against "// im.clipAnimations".
    assert any(line.strip().startswith("im.clipAnimations = clips")
               for line in code.splitlines()), code


def test_a_flag_that_did_not_take_is_reported(cli):
    """Asking is not the same as it having happened."""
    calls, state = cli
    state["result"] = "Walking=False"

    result = mixamo.set_looping("Assets/A.fbx", True)

    assert result["success"] is False
    assert "got False" in result["error"]


def test_a_model_with_no_clips_is_reported(cli):
    calls, state = cli
    state["result"] = "NO_CLIPS"

    result = mixamo.set_looping("Assets/A.fbx", True)

    assert result["success"] is False
    assert "could not set looping" in result["error"]


# ======================================================
# Blend variants
#
# The seven clips left over after eleven states were wired are all
# VARIANTS of states that exist -- a second fall, a running jump, four
# different landings -- rather than behaviours of their own. As
# separate states they would need triggers nobody would ever set; as
# blend children they are chosen by a number the game already knows.
# ======================================================

VARIANTS = library("Jump", "Jumping", "Running Jump", "Falling Idle",
                   "Falling", "Landing", "Falling To Landing",
                   "Falling To Roll", "Hard Landing",
                   "Falling Flat Impact")


def test_a_jump_blends_on_speed():
    found = mixamo.match_variants(VARIANTS, "Jump")

    assert found["parameter"] == "speed"
    assert [c["clip"] for c in found["children"]] == [
        "Jump", "Jumping", "Running Jump"]


def test_a_landing_blends_on_force_not_speed():
    """A fast run into a gentle step down is not a hard landing, and
    blending those on the same number would make it one."""
    found = mixamo.match_variants(VARIANTS, "Land")

    assert found["parameter"] == "landForce"
    assert len(found["children"]) == 5


def test_the_thresholds_are_in_order():
    """A 1D blend tree needs them ascending; the numbers themselves are
    placeholders to tune against real units."""
    for state in mixamo.BLEND_VARIANTS:
        found = mixamo.match_variants(VARIANTS, state)
        thresholds = [c["threshold"] for c in found["children"]]
        assert thresholds == sorted(thresholds), state


def test_a_variant_that_was_never_downloaded_is_skipped():
    """A blend child with no motion plays nothing at exactly the value
    it was meant to cover."""
    found = mixamo.match_variants(library("Jump", "Running Jump"), "Jump")

    assert [c["clip"] for c in found["children"]] == ["Jump", "Running Jump"]
    assert found["missing"] == ["jumping"]


def test_a_state_with_no_variants_declared_asks_for_nothing():
    assert mixamo.match_variants(VARIANTS, "Idle")["children"] == []


def test_one_variant_is_a_clip_not_a_blend(cli):
    """A tree with a single child is a clip wearing a costume."""
    result = mixamo.assign_blend_tree(
        "Assets/X.controller", "Jump", "speed",
        [{"asset": "Assets/A.fbx", "clip": "Jump", "threshold": 0.0}])

    assert result["success"] is False
    assert "rather than a blend" in result["error"]


def test_automatic_thresholds_are_turned_off(cli):
    """MEASURED: useAutomaticThresholds is ON by default and
    redistributes evenly the moment children are added. Jump asked for
    0, 1, 3 and came back 0, 0.5, 1 -- the order survives and the
    numbers do not, which matters because speed is in metres per
    second."""
    calls, state = cli
    state["result"] = "Jump:2:A,B,:0.00/3.00/"

    mixamo.assign_blend_tree("Assets/X.controller", "Jump", "speed",
                             [{"asset": "a.fbx", "clip": "A", "threshold": 0.0},
                              {"asset": "b.fbx", "clip": "B", "threshold": 3.0}])

    code = calls[0]["args"]["code"]
    live = [line.strip() for line in code.splitlines()
            if not line.strip().startswith("//")]
    assert "tree.useAutomaticThresholds = false;" in live
    assert "tree.children = kids;" in live,         "the edited array is never assigned back"


def test_thresholds_that_came_back_wrong_are_reported(cli):
    """Asking is not the same as it having happened, and this one
    silently did not happen the first time."""
    calls, state = cli
    state["result"] = "Jump:2:A,B,:0.00/1.00/"

    result = mixamo.assign_blend_tree(
        "Assets/X.controller", "Jump", "speed",
        [{"asset": "a.fbx", "clip": "A", "threshold": 0.0},
         {"asset": "b.fbx", "clip": "B", "threshold": 3.0}])

    assert result["success"] is False
    assert "redistributed" in result["error"]


def test_a_blend_replaces_rather_than_nests(cli):
    """Running it twice should not leave a tree inside a tree."""
    calls, state = cli
    state["result"] = "Jump:2:A,B,:0.00/3.00/"
    mixamo.assign_blend_tree("Assets/X.controller", "Jump", "speed",
                             [{"asset": "a.fbx", "clip": "A", "threshold": 0.0},
                              {"asset": "b.fbx", "clip": "B", "threshold": 3.0}])

    assert "DestroyImmediate(old" in calls[0]["args"]["code"]


def test_configuring_blends_skips_what_it_cannot_build(cli):
    calls, state = cli
    state["result"] = "Jump:3:A,B,C,:0.00/1.00/3.00/"

    result = mixamo.configure_blends("Assets/X.controller",
                                     library("Jump", "Jumping", "Running Jump"))

    assert "Jump" in result["blends"]
    assert any("Fall" in s for s in result["skipped"])


# ======================================================
# How long the state lasts, when physics decides it
# ======================================================

def test_the_jump_matches_its_variants_durations_and_the_others_do_not():
    """MEASURED, the three clips in the Jump tree:

        threshold 0.0  Jump          1.00s
        threshold 1.0  Jumping       1.90s   <- walking speed picks this
        threshold 3.0  Running Jump  0.90s

    Exit time is NORMALISED, a fraction of the tree's length, so the
    walking slot held the character in the jump nearly twice as long
    after he had already landed. Land is deliberately NOT matched: the
    difference in length there is the difference between a gentle
    landing and a hard one.
    """
    assert mixamo.BLEND_VARIANTS["Jump"].get("match_duration") is True
    assert not mixamo.BLEND_VARIANTS["Land"].get("match_duration")
    assert not mixamo.BLEND_VARIANTS["Fall"].get("match_duration")


def test_matching_durations_only_happens_when_it_is_asked_for(cli):
    calls, state = cli
    state["result"] = "Jump:2:A,B,:0.00/3.00/:1.00/1.00/"
    children = [{"asset": "a.fbx", "clip": "A", "threshold": 0.0},
                {"asset": "b.fbx", "clip": "B", "threshold": 3.0}]

    mixamo.assign_blend_tree("Assets/X.controller", "Land", "landForce",
                             children)
    assert ".timeScale =" not in calls[-1]["args"]["code"], (
        "every tree REPORTS its scales; only a matched one writes them")

    mixamo.assign_blend_tree("Assets/X.controller", "Jump", "speed",
                             children, match_duration=True)
    assert ".timeScale =" in calls[-1]["args"]["code"]


def test_a_shorter_variant_is_never_stretched(cli):
    """Running Jump is 0.90s against a 1.00s reference. Scaling it to
    match would SLOW it down, and a running jump in slow motion is a
    worse bug than the one being fixed."""
    calls, state = cli
    state["result"] = "Jump:2:A,B,:0.00/3.00/:1.00/1.90/"
    mixamo.assign_blend_tree(
        "Assets/X.controller", "Jump", "speed",
        [{"asset": "a.fbx", "clip": "A", "threshold": 0.0},
         {"asset": "b.fbx", "clip": "B", "threshold": 3.0}],
        match_duration=True)

    statements = [line.split("//")[0].strip()
                  for line in calls[-1]["args"]["code"].splitlines()]
    assert any("scale > 1f ? scale : 1f" in line for line in statements), (
        "nothing stops a short variant from being slowed to fill time")


def test_durations_that_did_not_change_are_reported(cli):
    """Asking is not the same as it having happened -- the same lesson
    the thresholds taught, and the same silence if it is not checked."""
    calls, state = cli
    state["result"] = "Jump:2:A,B,:0.00/3.00/:1.00/1.00/"

    result = mixamo.assign_blend_tree(
        "Assets/X.controller", "Jump", "speed",
        [{"asset": "a.fbx", "clip": "A", "threshold": 0.0},
         {"asset": "b.fbx", "clip": "B", "threshold": 3.0}],
        match_duration=True)

    assert result["success"] is False
    assert "timeScale 1" in result["error"]
    assert result["time_scales"] == [1.0, 1.0]


def test_configuring_blends_matches_the_jump_and_not_the_landing(cli):
    calls, state = cli
    state["result"] = "Jump:3:A,B,C,:0.00/1.00/3.00/:1.00/1.90/1.00/"

    mixamo.configure_blends(
        "Assets/X.controller",
        library("Jump", "Jumping", "Running Jump",
                "Landing", "Hard Landing", "Falling To Roll"))

    asked = {c["args"]["code"].split('s.state.name == "')[1].split('"')[0]:
             (".timeScale =" in c["args"]["code"])
             for c in calls if c["command"] == "eval"}
    assert asked.get("Jump") is True, "the jump's length is set by gravity"
    assert asked.get("Land") is False, "a hard landing takes longer, truthfully"
