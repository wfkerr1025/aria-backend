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
        return {"success": True, "output": "", "error": None,
                "json": {"success": True, "errors": [],
                         "data": {"command": name, "parameters": named,
                                  "result": {"success": True, "result": "ok"},
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
