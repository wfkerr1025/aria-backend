"""The script that drives the animator parameters.

A controller nobody sets is a character that stands still, so the
animation pipeline is only half the job. This is the other half, and
almost everything worth testing about it is checkable without Unity:
that the parameter names agree with the ones the graph declares, that
the input system is the one the project actually uses, and that the
C# says what its comments claim.

WHY THE NEW INPUT SYSTEM, MEASURED
----------------------------------
The project's ProjectSettings say activeInputHandler: 1, which is the
new Input System ONLY. A script written against legacy Input compiles
and then throws at runtime, which is the worst way to find out.
"""

from __future__ import annotations

import pytest

from backend.unity import animation_parameters as params
from backend.unity import animation_state_graph as graphs
from backend.unity import character_controller as controller
from backend.unity import unity_delivery as delivery


@pytest.fixture
def cli(monkeypatch):
    calls = []

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        name = command.replace("cmd ", "")
        calls.append({"command": name, "args": named})

        # The field query gets a fully compiled answer by default, so a
        # test about delivery is not accidentally a test about a stale
        # assembly. The stale case is exercised deliberately below.
        said = "ok"
        if "GetFields" in named.get("code", ""):
            said = ",".join(controller.declared_fields()) + ","

        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": name, "parameters": named,
                                  "result": {"success": True, "result": said},
                                  "target": {}, "success": True}}}

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return calls


# ======================================================
# The contract with the controller it drives
# ======================================================

def test_it_sets_exactly_the_parameters_the_graph_declares():
    """SetFloat on an unknown parameter is SILENT. A rename on one side
    and not the other is a character that stands still and no error
    anywhere."""
    used = set(controller.parameters_used())
    declared = {p.name for p in params.parameters_for(graphs.ALL_STATES)}

    assert used == declared, (
        f"only in the script: {sorted(used - declared)}; "
        f"only in the graph: {sorted(declared - used)}")


def test_every_parameter_is_hashed_not_looked_up_by_string():
    """SetFloat(string) does the lookup every call, and this runs every
    frame."""
    for name in controller.parameters_used():
        assert f'StringToHash("{name}")' in controller.SOURCE


def test_the_speed_thresholds_are_reachable():
    """runSpeed has to exceed the Walk-to-Run threshold, or the Run
    state can never be entered however fast the character moves."""
    walk_to_run = 2.0
    line = [l for l in controller.SOURCE.splitlines()
            if "public float runSpeed" in l][0]
    value = float(line.split("=")[1].strip().rstrip("f;").strip())

    assert value > walk_to_run


def test_walking_is_below_the_run_threshold_and_above_the_idle_one():
    speeds = {}
    for field in ("walkSpeed", "crouchSpeed"):
        line = [l for l in controller.SOURCE.splitlines()
                if f"public float {field}" in l][0]
        speeds[field] = float(line.split("=")[1].strip().rstrip("f;").strip())

    assert 0.1 < speeds["walkSpeed"] < 2.0
    assert 0.1 < speeds["crouchSpeed"] < 2.0


# ======================================================
# The input system, which is not a matter of taste here
# ======================================================

def test_it_uses_the_new_input_system():
    assert "using UnityEngine.InputSystem;" in controller.SOURCE
    assert "Keyboard.current" in controller.SOURCE


def test_it_does_not_use_legacy_input():
    """activeInputHandler: 1 means legacy Input throws at runtime."""
    for legacy in ("Input.GetAxis", "Input.GetKey", "Input.GetButton",
                   "Input.mousePosition"):
        assert legacy not in controller.SOURCE, legacy


def test_it_needs_no_input_actions_asset():
    """Reading devices directly means the script works the moment it
    is attached, with no asset to author first."""
    assert "InputActionAsset" not in controller.SOURCE
    assert ".inputactions" not in controller.SOURCE


# ======================================================
# The things that are easy to get subtly wrong
# ======================================================

def test_the_fall_speed_is_captured_before_the_move():
    """Reading it after landing is too late -- the controller has
    already zeroed the vertical speed, so every landing would look
    gentle."""
    source = controller.SOURCE
    assert source.index("_fallSpeedLastFrame = -_verticalSpeed;") < \
        source.index("_controller.Move(")


def test_land_force_is_set_only_on_the_landing_frame():
    """Left at its last value, the next gentle landing plays a flat
    impact."""
    assert "if (grounded && !_wasGrounded)" in controller.SOURCE


def test_grounded_is_not_pinned_to_exactly_zero():
    """Exactly zero makes isGrounded flicker on slopes and steps, and a
    flickering grounded flag fires the Fall transition every few
    frames."""
    assert "_verticalSpeed = -2f;" in controller.SOURCE


def test_camera_relative_movement_is_flattened():
    """A camera looking down would otherwise walk the character into
    the floor."""
    assert "forward.y = 0f" in controller.SOURCE


def test_speed_is_read_from_the_controller_not_from_the_input():
    """Input says what was asked for; velocity says what happened. A
    character walking into a wall should not play a run."""
    assert "_controller.velocity" in controller.SOURCE
    assert "planar.y = 0f;" in controller.SOURCE


def test_it_requires_the_components_it_uses():
    for required in ("CharacterController", "Animator"):
        assert f"[RequireComponent(typeof({required}))]" in controller.SOURCE


# ======================================================
# Delivery
# ======================================================

def test_the_script_goes_under_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "unity_project_root", lambda: tmp_path)

    assert controller.script_path() == \
        "Assets/ARIA/Scripts/AriaCharacterController.cs"
    assert controller.script_path("Scripts") == \
        "Assets/Scripts/AriaCharacterController.cs"


def test_delivering_writes_the_file(tmp_path, monkeypatch, cli):
    monkeypatch.setattr(delivery, "unity_project_root", lambda: tmp_path)

    result = controller.deliver()

    written = tmp_path / "Assets/ARIA/Scripts/AriaCharacterController.cs"
    assert result["success"] is True and result["written"] is True
    assert "class AriaCharacterController" in written.read_text(encoding="utf-8")


def test_delivering_refreshes_so_unity_compiles_it(tmp_path, monkeypatch, cli):
    monkeypatch.setattr(delivery, "unity_project_root", lambda: tmp_path)
    controller.deliver()

    assert "AssetDatabase.Refresh" in cli[0]["args"]["code"]


def test_an_existing_file_is_kept_when_asked(tmp_path, monkeypatch, cli):
    """Somebody will edit this script. Overwriting their tuning without
    being asked would be the last time they trusted the pipeline."""
    monkeypatch.setattr(delivery, "unity_project_root", lambda: tmp_path)
    target = tmp_path / "Assets/ARIA/Scripts/AriaCharacterController.cs"
    target.parent.mkdir(parents=True)
    target.write_text("mine", encoding="utf-8")

    result = controller.deliver(overwrite=False)

    assert result["written"] is False
    assert target.read_text(encoding="utf-8") == "mine"


def test_no_project_means_nowhere_to_put_it(monkeypatch, cli):
    monkeypatch.setattr(delivery, "unity_project_root", lambda: None)

    result = controller.deliver()

    assert result["success"] is False
    assert "nowhere to put it" in result["error"]


# ======================================================
# What playing it actually found
#
# Three complaints, three separate causes:
#   "turning left and right do not work properly"
#   "jumping standing still doesn't work all the time"
#   "he acts as if he hits the ground while in the air and does a roll"
# ======================================================

def test_turn_and_walk_is_the_default_scheme():
    """Without a camera, absolute directions snap the character to
    compass points: A walks west, and then W turns him back north
    rather than walking the way he is facing."""
    assert "steering = Steering.TurnAndWalk" in controller.SOURCE
    assert "transform.Rotate(0f, _move.x * turnSpeed" in controller.SOURCE


def test_the_absolute_scheme_is_still_available():
    """It is the right one WITH a camera, which is the usual
    third-person setup."""
    assert "DirectionIsAbsolute" in controller.SOURCE
    assert "cameraTransform" in controller.SOURCE


def test_grounded_is_buffered_rather_than_read_raw():
    """CharacterController.isGrounded is false on scattered frames
    while standing still. The jump press landed on one of those."""
    assert "groundedGrace" in controller.SOURCE
    assert "Time.time - _lastGroundedAt <= groundedGrace" in controller.SOURCE


def test_the_jump_uses_the_buffered_grounded():
    assert "keyboard.spaceKey.wasPressedThisFrame && Grounded" in \
        controller.SOURCE


def test_rising_counts_as_airborne_immediately():
    """Otherwise the grace period reports grounded for the first tenth
    of a second of every jump."""
    assert "_controller.isGrounded && _verticalSpeed <= 0f" in controller.SOURCE


def test_a_routine_jump_is_a_soft_landing_not_a_roll():
    """MEASURED against the roll it was producing. A 1.2m jump lands at
    about 6.9 m/s; dividing that by 12 gave 0.57, and the Land blend
    puts Falling To Roll at 0.6."""
    source = controller.SOURCE
    soft = float([l for l in source.splitlines()
                  if "public float softLandingSpeed" in l][0]
                 .split("=")[1].strip().rstrip("f;").strip())
    hard = float([l for l in source.splitlines()
                  if "public float hardLandingSpeed" in l][0]
                 .split("=")[1].strip().rstrip("f;").strip())

    jump_impact = 6.9
    force = max(0.0, min(1.0, (jump_impact - soft) / (hard - soft)))
    assert force < 0.35, (
        f"a routine jump maps to {force:.2f}, which reaches the roll at 0.6")
    assert hard > soft, "the landing scale has no span"


def test_the_enum_is_not_under_a_header_attribute():
    """[Header] is valid on a field and not on a type. It was a CS0592
    that left Unity running the previously compiled assembly while
    every file on disk looked correct."""
    lines = controller.SOURCE.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.strip().startswith("[Header("):
            assert not lines[index + 1].strip().startswith(
                ("public enum", "public class", "public struct")), line


# ======================================================
# Delivery has to know whether Unity accepted it
# ======================================================

def test_the_declared_fields_are_read_out_of_the_source():
    declared = controller.declared_fields()

    for expected in ("walkSpeed", "runSpeed", "steering", "softLandingSpeed",
                     "groundedGrace", "hardLandingSpeed"):
        assert expected in declared, expected
    assert "Steering" not in declared, "the enum type is not a field"


def test_a_stale_assembly_is_detected_by_a_missing_field(monkeypatch, cli):
    """This is the actual symptom: the file on disk is correct, the
    component in the scene is the OLD type, and nothing says so."""
    monkeypatch.setattr(controller, "compiled_fields",
                        lambda name=controller.CLASS_NAME: {
                            "checked": True, "reason": "",
                            "fields": ["walkSpeed", "runSpeed"]})

    report = controller.verify_compiled()

    assert report["compiled"] is False
    assert "steering" in report["missing"]


def test_a_fully_compiled_assembly_verifies(monkeypatch, cli):
    monkeypatch.setattr(controller, "compiled_fields",
                        lambda name=controller.CLASS_NAME: {
                            "checked": True, "reason": "",
                            "fields": controller.declared_fields()})

    assert controller.verify_compiled()["compiled"] is True


def test_a_check_that_cannot_run_is_not_a_pass(monkeypatch, cli):
    """An earlier version returned an empty error list when the check
    itself failed, so a broken check read as a clean compile -- which
    is how the check's own first version got past its own delivery."""
    monkeypatch.setattr(controller, "compiled_fields",
                        lambda name=controller.CLASS_NAME: {
                            "checked": False, "fields": [],
                            "reason": "the Editor was not reachable"})

    report = controller.verify_compiled()

    assert report["checked"] is False
    assert report["compiled"] is False


def test_delivering_reports_a_stale_assembly_rather_than_success(
        tmp_path, monkeypatch, cli):
    monkeypatch.setattr(delivery, "unity_project_root", lambda: tmp_path)
    monkeypatch.setattr(controller, "verify_compiled",
                        lambda name=controller.CLASS_NAME: {
                            "checked": True, "compiled": False,
                            "missing": ["steering"], "reason": ""})

    result = controller.deliver(compile_attempts=1)

    assert result["success"] is False
    assert "older build" in result["error"]
    assert result["written"] is True, "the file was still written"


# ======================================================
# Root motion, which moves the character by the clip
# ======================================================

@pytest.fixture
def root_motion_cli(monkeypatch):
    """A CLI whose eval answers the way stop_root_motion's C# does."""
    seen = []

    def fake(command, args=None, **kwargs):
        argv = list(args or [])
        named = {argv[i].lstrip("-"): argv[i + 1]
                 for i in range(0, len(argv) - 1, 2) if argv[i].startswith("--")}
        code = named.get("code", "")
        seen.append(code)

        said = "ok"
        if "applyRootMotion" in code:
            said = "was=True now=False prefabWas=True"
        elif "DestroyImmediate" in code:
            said = "found=2 removed=1"
        elif "GetFields" in code:
            said = ",".join(controller.declared_fields()) + ","

        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": command.replace("cmd ", ""),
                                  "parameters": named,
                                  "result": {"success": True, "result": said},
                                  "target": {}, "success": True}}}

    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation", fake)
    return seen


def test_root_motion_is_turned_off_on_the_prefab_too(root_motion_cli):
    """Turning it off on the scene object alone is undone the next time
    the character is placed -- the same bug arriving again, looking new."""
    result = controller.stop_root_motion("AriaHero")

    assert result["success"] is True
    assert result["prefab_was_on"] is True

    code = [c for c in root_motion_cli if "applyRootMotion" in c][0]
    assert "GetCorrespondingObjectFromSource" in code, (
        "nothing reaches the prefab behind the instance")
    assert "AssetDatabase.SaveAssets" in code, "the prefab edit is not written"


def test_attaching_the_script_turns_root_motion_off(monkeypatch,
                                                    root_motion_cli):
    """Attaching this script IS the statement that physics drives the
    character, so it cannot be a separate step a caller can forget.
    With both moving him, the measured symptom was a jump that landed,
    fell into the air, landed again in mid-air and then dropped."""
    added = controller.attach("AriaHero")

    assert added["success"] is True
    assert added["root_motion_off"] is True
    assert "warning" not in added
    assert any("applyRootMotion = false" in c for c in root_motion_cli)


def test_attaching_says_so_when_root_motion_could_not_be_turned_off(
        monkeypatch, cli):
    """The script being on the object is not the same as the character
    being drivable, and reporting plain success hides the difference."""
    monkeypatch.setattr(controller, "stop_root_motion",
                        lambda target: {"success": False, "ran": True,
                                        "error": "the Editor was not reachable"})

    added = controller.attach("AriaHero")

    assert added["root_motion_off"] is False
    assert "fight the" in added["warning"]


def test_attaching_takes_off_the_copies_an_earlier_attach_left(
        root_motion_cli):
    """ensure_component used to add before it checked, and Unity allows
    many MonoBehaviours of one type, so every attach added another copy
    -- each one calling CharacterController.Move every frame."""
    added = controller.attach("AriaHero")

    assert added["success"] is True
    assert added["duplicates_removed"] == 1
    assert any("DestroyImmediate" in c for c in root_motion_cli), (
        "nothing sweeps the duplicates an earlier attach left behind")
