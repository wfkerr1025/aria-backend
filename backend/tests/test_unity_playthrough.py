# backend/tests/test_unity_playthrough.py
#
# The playthrough runner, played against a stand-in bridge that keeps a
# tiny game in memory: a score, a console, a screen of text, and a log.
# Nothing here runs Unity; the runner's real run is checked against the
# bridge lab project, and a green run here says nothing about that.

from __future__ import annotations

import json

import pytest

from aria import unity_editor_bridge as ueb
from aria import unity_playthrough as up


class FakeBridge:
    """Just enough of UnityEditorBridge for a runner to play a game through."""

    def __init__(self, project_root=None):
        self.project_root = project_root
        self.playing = False
        self.frame = 0
        self.score = 0
        self.errors: list[dict] = []
        self.sent: list[tuple] = []
        self.report = None
        self.real_save_safe = True
        self.refuse: set[str] = set()
        self.test_save = ""
        self.starts_on_real_save = False
        self.pressed_play_by_hand = False

    def _record(self, name, *args, **kwargs):
        self.sent.append((name, args, kwargs))
        if name in self.refuse:
            raise ueb.UnityBridgeError(f"{name} refused")

    def ping(self):
        if self.playing:
            self.frame += 10
        return {"isPlaying": self.playing, "frame": self.frame if self.playing else 0,
                "testSave": self.test_save if self.playing else ""}

    def set_play_mode(self, playing=True, **kwargs):
        self._record("set_play_mode", playing, **kwargs)
        if playing and self.pressed_play_by_hand:
            # Someone pressed Play between the runner looking and asking: the
            # real bridge answers "Already playing." and starts nothing.
            self.playing = True
            return {"changed": False}
        changed = self.playing != bool(playing)
        if self.playing and not playing:
            self.report = {"startedAt": f"run{len(self.sent)}", "realSaveSafe": self.real_save_safe}
        if playing and changed:
            real = self.starts_on_real_save or kwargs.get("test_save") is False
            self.test_save = "" if real else "ARIA/testsave"
        self.playing = bool(playing)
        return {"changed": changed}

    def last_play(self):
        return self.report

    def run_console(self, *lines, **kwargs):
        self._record("run_console", *lines)
        ran = []
        for line in lines:
            if line == "boom":
                raise ueb.UnityBridgeError("'boom' threw InvalidOperationException: console boom.")
            if line.startswith("score "):
                self.score += int(line.split()[1])
            ran.append({"line": line, "output": f"score: {self.score}" if line.startswith("score") else "ok"})
        return {"ran": ran}

    def send_input(self, *actions, **kwargs):
        self._record("send_input", *actions)
        for action in actions:
            if action.get("click") == "Canvas/Add":
                self.score += action.get("times", 1)
            if action.get("key") == "e":
                self.errors.append({"type": "exception", "message": "InvalidOperationException: E throws"})
        return {"queued": len(actions)}

    def wait_for_input(self, timeout=None):
        return {"inputPending": 0}

    def read_screen(self, targets=None, **kwargs):
        self._record("read_screen")
        controls = [
            {"label": "Add", "path": "Canvas/Row/Button", "center": [100.0, 50.0], "interactable": True},
            {"label": "Add", "path": "Canvas/Top/Button", "center": [100.0, 400.0], "interactable": True},
            {"label": "Pull", "path": "Canvas/Machine/Button", "center": [300.0, 200.0],
             "interactable": self.score < 5},
            {"label": None, "path": "HUD/MinerButton", "center": [20.0, 20.0], "interactable": True},
        ]
        return {"lines": [f"Score: {self.score}", "Add"], "controls": controls}

    def get_field(self, target, component, field_name):
        self._record("get_field", target, component, field_name)
        return {"value": float(self.score)}

    def get_log(self, since=None, **kwargs):
        return {"entries": list(self.errors), "errors": len(self.errors)}

    def set_time(self, time_scale=None, **kwargs):
        self._record("set_time", time_scale, **kwargs)
        return {"timeScale": time_scale if time_scale is not None else 1.0}

    def wait_for_steps(self, timeout=None):
        return {"frame": self.frame}

    def screenshot(self, path=None, **kwargs):
        self._record("screenshot", path, **kwargs)
        return {"path": path, "burst": bool(kwargs.get("count"))}

    def wait_for_burst(self, timeout=None):
        return {"taken": 4, "sheet": "ARIA/playthroughs/x/spin_sheet.png"}


def runner(bridge, **options):
    options.setdefault("sleep", lambda seconds: None)
    options.setdefault("start_timeout", 5)
    return up.PlaythroughRunner(bridge, **options)


# ======================================================
# Checking a script before anything starts
# ======================================================

def test_a_good_script_has_no_problems():
    script = {"name": "ok", "steps": [
        {"console": "score 1", "expect": "score"},
        {"click": "Canvas/Add", "times": 2},
        {"expectField": {"target": "Game", "component": "Score", "field": "value", "atLeast": 3}},
        {"until": {"text": "Score: 3"}, "timeout": 2},
        {"expectNoErrors": True},
    ]}
    assert up.validate(script) == []


@pytest.mark.parametrize("step, complaint", [
    ({"clik": "Canvas/Add"}, "exactly one of"),
    ({"click": "a", "press": "b"}, "exactly one of"),
    ({"wait": 1, "timout": 5}, "does not take timout"),
    ({"wait": "soon"}, "wait is a number"),
    ({"step": 0}, "whole number of frames"),
    ({"expectField": {"target": "Game", "field": "x"}}, "exactly one of equals"),
    ({"expectField": {"target": "Game", "field": "x", "equals": 1, "atMost": 2}}, "exactly one of equals"),
    ({"until": {"text": "a", "noText": "b"}}, "until is"),
    ({"console": ["a", 3]}, "console is a line"),
])
def test_a_misspelt_step_is_refused_rather_than_skipped(step, complaint):
    """A check that is silently ignored is a check that quietly never happened."""
    problems = up.validate({"steps": [step]})
    assert any(complaint in problem for problem in problems), problems


def test_an_invalid_script_never_starts_the_game():
    bridge = FakeBridge()
    result = runner(bridge).run({"name": "bad", "steps": [{"clik": "x"}]})

    assert not result.ok and "was not run" in result.problem
    assert bridge.sent == [], "nothing was sent to Unity"


def test_the_seed_must_be_one_the_bridge_knows():
    assert up.validate({"seed": "clean", "steps": [{"wait": 1}]})


# ======================================================
# Running
# ======================================================

def test_a_passing_run_starts_fresh_plays_every_step_and_stops(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge).run({"name": "smoke", "steps": [
        {"console": "score 1", "expect": "score: 1"},
        {"click": "Canvas/Add", "times": 2},
        {"expectText": "Score: 3"},
        {"expectField": {"target": "Game", "component": "Score", "field": "value", "equals": 3}},
        {"timeScale": 4},
        {"step": 5},
        {"screenshot": "end state"},
        {"burst": "spin", "count": 4, "everySeconds": 0.1},
        {"expectNoErrors": True},
    ]})

    assert result.ok, result.summary()
    assert [step.ok for step in result.steps] == [True] * 9
    assert bridge.sent[0] == ("set_play_mode", (True,), {"seed": "fresh"}), "a playthrough starts fresh"
    assert bridge.sent[-1][0] == "set_play_mode" and bridge.sent[-1][1] == (False,)
    assert result.real_save_safe is True
    assert any(path.endswith("end_state.png") for path in result.screenshots)
    assert any(path.endswith("spin_sheet.png") for path in result.screenshots)

    report = tmp_path / result.report_path
    assert report.exists() and "PASSED" in report.read_text(encoding="utf-8")
    assert json.loads(report.with_name("report.json").read_text(encoding="utf-8"))["ok"] is True


def test_the_first_failure_stops_the_run_and_photographs_the_moment(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge).run({"name": "fails", "steps": [
        {"click": "Canvas/Add"},
        {"expectText": "Score: 99", "label": "the score reached 99"},
        {"click": "Canvas/Add"},
    ]})

    assert not result.ok
    assert len(result.steps) == 2, "the step after the failure did not run"
    failed = result.failed
    assert failed.index == 2 and failed.label == "the score reached 99"
    assert "Score: 1" in failed.message, "the failure says what the screen did say"
    assert failed.evidence["screenshot"].endswith("failed_step2.png")
    assert "Step 2 (the score reached 99)" in result.summary()


def test_keep_going_plays_on_after_a_failure(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge, stop_on_failure=False).run({"steps": [
        {"expectText": "nowhere"}, {"click": "Canvas/Add"}, {"expectText": "Score: 1"}]})

    assert [step.ok for step in result.steps] == [False, True, True]
    assert not result.ok


def test_an_error_in_the_console_fails_a_run_whose_steps_all_passed(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge).run({"steps": [{"press": "e"}, {"wait": 0.1}]})

    assert all(step.ok for step in result.steps)
    assert not result.ok, "an exception is a failure even when nothing checked for it"
    assert "E throws" in result.summary()


def test_errors_can_be_allowed_by_a_script_that_expects_them(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge).run({"allowErrors": True, "steps": [{"press": "e"}]})
    assert result.ok and result.errors


def test_a_changed_real_save_fails_the_run(tmp_path):
    bridge = FakeBridge(tmp_path)
    bridge.real_save_safe = False
    result = runner(bridge).run({"steps": [{"wait": 0}]})

    assert not result.ok and result.real_save_safe is False
    assert "REAL save changed" in result.summary()


def test_a_refusal_from_the_bridge_is_a_failed_step_not_a_crash(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge).run({"steps": [{"console": ["score 1", "boom"]}]})

    assert not result.ok
    assert "console boom" in result.failed.message


def test_until_waits_for_the_screen_to_catch_up(tmp_path):
    bridge = FakeBridge(tmp_path)
    reads = iter([["Loading"], ["Loading"], ["Ready"]])
    bridge.read_screen = lambda targets=None, **kwargs: {"lines": next(reads)}

    result = runner(bridge).run({"steps": [{"until": {"text": "Ready"}, "timeout": 5}]})
    assert result.ok, result.summary()


def test_until_gives_up_and_says_what_it_last_saw(tmp_path):
    bridge = FakeBridge(tmp_path)
    clock = iter(range(0, 1000))
    result = runner(bridge, monotonic=lambda: next(clock)).run(
        {"steps": [{"until": {"text": "Ready"}, "timeout": 3}]})

    assert not result.ok and "still not so after 3s" in result.failed.message


def test_keep_playing_leaves_the_game_running_and_says_the_save_was_not_checked(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge, keep_playing=True).run({"steps": [{"wait": 0}]})

    assert bridge.playing
    assert result.real_save_safe is None and "not known" in result.summary()


def test_a_test_session_left_playing_is_stopped_first(tmp_path):
    bridge = FakeBridge(tmp_path)
    bridge.playing, bridge.test_save = True, "ARIA/testsave"
    runner(bridge).run({"steps": [{"wait": 0}]})
    names = [(name, args) for name, args, _ in bridge.sent if name == "set_play_mode"]
    assert names[:2] == [("set_play_mode", (False,)), ("set_play_mode", (True,))]


def test_someone_playing_the_real_save_is_left_alone(tmp_path):
    """Stopping a person's session makes their game save; the run is refused instead."""
    bridge = FakeBridge(tmp_path)
    bridge.playing = True
    result = runner(bridge).run({"steps": [{"wait": 0}]})

    assert not result.ok and "already playing on the real save" in result.problem
    assert bridge.playing, "their session is still running"
    assert not any(name == "set_play_mode" for name, _, _ in bridge.sent), "nothing started or stopped"


def test_play_pressed_by_hand_as_the_run_starts_is_left_alone(tmp_path):
    """The bridge says 'Already playing' and starts nothing; the run must not take that session."""
    bridge = FakeBridge(tmp_path)
    bridge.pressed_play_by_hand = True
    result = runner(bridge).run({"steps": [{"console": "score 1"}]})

    assert not result.ok and "did not begin" in result.problem
    assert result.steps == [], "no step was played on it"
    assert bridge.playing and ("set_play_mode", (False,), {}) not in bridge.sent, "and it was not stopped"


def test_a_session_that_starts_on_the_real_save_is_stopped_before_any_step(tmp_path):
    bridge = FakeBridge(tmp_path)
    bridge.starts_on_real_save = True
    result = runner(bridge).run({"steps": [{"console": "score 1"}]})

    assert not result.ok and "started on the real save" in result.problem
    assert result.steps == [] and not bridge.playing
    assert not any(name == "run_console" for name, _, _ in bridge.sent)


def test_click_text_finds_a_button_by_what_it_says(tmp_path):
    """A game's buttons are all called Button; their text is what tells them apart."""
    bridge = FakeBridge(tmp_path)
    result = runner(bridge).run({"steps": [
        {"clickText": "Add"},
        {"clickText": "Add", "nth": 1, "times": 2},
    ]})

    assert result.ok, result.summary()
    sent = [args for name, args, _ in bridge.sent if name == "send_input"]
    assert sent[0][0]["click"] == [100.0, 400.0], "nth 0 is the top of the screen"
    assert sent[1][0]["click"] == [100.0, 50.0] and sent[1][0]["times"] == 2


def test_click_text_fails_on_a_missing_or_disabled_button(tmp_path):
    bridge = FakeBridge(tmp_path)
    bridge.score = 9
    result = runner(bridge, stop_on_failure=False).run({"steps": [
        {"clickText": "Sell"}, {"clickText": "Pull"}, {"clickText": "Add", "nth": 5}]})

    messages = [step.message for step in result.steps]
    assert "no button reads 'Sell'" in messages[0]
    assert "disabled" in messages[1]
    assert "wanted number 6, found 2" in messages[2]


def test_expect_control_checks_presence_and_state(tmp_path):
    bridge = FakeBridge(tmp_path)
    result = runner(bridge, stop_on_failure=False).run({"steps": [
        {"expectControl": {"label": "Pull", "interactable": True}},
        {"expectControl": {"path": "MinerButton"}},
        {"expectControl": {"path": "MinerButton", "present": False}},
        {"expectControl": {"label": "Cannot afford"}},
        {"until": {"control": {"label": "Pull"}}, "timeout": 1},
    ]})

    assert [step.ok for step in result.steps] == [True, True, False, False, True]
    assert "still on screen" in result.steps[2].message


def test_a_control_check_needs_a_label_or_a_path():
    problems = up.validate({"steps": [{"expectControl": {"interactable": True}}, {"clickText": ""},
                                      {"expectControl": {"label": "x", "colour": "red"}}]})
    assert any("needs a label" in problem for problem in problems)
    assert any("clickText is the text" in problem for problem in problems)
    assert any("does not take colour" in problem for problem in problems)


@pytest.mark.parametrize("value, spec, passed", [
    (3.0, {"equals": 3}, True),
    (3.0, {"notEquals": 3}, False),
    (5.0, {"atLeast": 3}, True),
    (2.0, {"atMost": 1}, False),
    ("Stone Cutter", {"contains": "Cutter"}, True),
    ([1, 2], {"contains": "2"}, True),
    ("n/a", {"atLeast": 1}, False),
    (True, {"equals": 1}, False),
])
def test_field_comparisons(value, spec, passed):
    assert up._compare(value, spec)[0] is passed


# ======================================================
# From a shell
# ======================================================

def test_the_cli_refuses_invalid_scripts_before_touching_unity(tmp_path, capsys):
    script = tmp_path / "bad.json"
    script.write_text(json.dumps({"steps": [{"clik": "x"}]}), encoding="utf-8")

    assert up.main([str(script)]) == 2
    assert "exactly one of" in capsys.readouterr().err


def test_the_cli_runs_a_folder_and_exits_by_the_result(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "Playthroughs"
    folder.mkdir()
    (folder / "a.json").write_text(json.dumps({"steps": [{"click": "Canvas/Add"}]}), encoding="utf-8")
    (folder / "b.json").write_text(json.dumps({"steps": [{"expectText": "Score: 99"}]}), encoding="utf-8")

    project = tmp_path / "Proj"
    (project / "Assets").mkdir(parents=True)
    fake = FakeBridge(project)
    monkeypatch.setattr(up.ueb, "UnityEditorBridge", lambda root=None: fake)

    code = up.main([str(folder), "--project", str(project)])
    out = capsys.readouterr().out
    assert code == 1, "one of the two failed"
    assert "a: PASSED" in out and "b: FAILED" in out
