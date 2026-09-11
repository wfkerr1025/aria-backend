"""Scripted playthroughs: a game played by a script, and checked as it goes.

A playthrough is a JSON file of steps -- console lines, key presses,
clicks, waits, and expectations about what the screen says, what a field
holds, and whether anything threw. The runner starts a test session
through the Unity editor bridge (on the bridge's test save, never the
player's), plays the steps in order, and reports which passed, the first
that failed with a screenshot of that moment, every error the console
raised, and whether the real save came through untouched.

Worth little on a game with three systems and a great deal on one with a
dozen: it is what stops a change to one of them quietly breaking another,
without anyone having to play the whole game again to find out.

    python -m aria.unity_playthrough --project "D:/Unity/Projects/Stone Betting" Playthroughs/smoke.json
    python -m aria.unity_playthrough --project "D:/Unity/Projects/Stone Betting" Playthroughs/

A script:

    {
      "name": "smoke",
      "seed": "fresh",           what the test save holds as play begins: fresh, keep or real
      "allowErrors": false,      any console error fails the run unless this is true
      "steps": [
        {"console": "coins 1000", "expect": "coins: "},
        {"press": "space", "times": 3},
        {"click": "Canvas/BuyButton"},
        {"wait": 0.5},
        {"expectText": "Score: 3"},
        {"expectField": {"target": "GameSystems", "component": "Wallet",
                         "field": "coins", "atLeast": 1000}},
        {"until": {"text": "Ready"}, "timeout": 10},
        {"screenshot": "after-buying"},
        {"burst": "charm", "count": 8, "everySeconds": 0.1},
        {"expectNoErrors": true}
      ]
    }

Every step may carry a "label", which the report uses in place of the
step itself. STEP_KINDS lists the steps and what each may carry.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from aria import unity_editor_bridge as ueb

__all__ = [
    "STEP_KINDS",
    "COMPARATORS",
    "PlaythroughResult",
    "PlaythroughRunner",
    "StepResult",
    "load_script",
    "run_playthrough",
    "validate",
]

# Each kind of step, and the other keys it may carry. A key that is not
# here is refused before anything starts: a misspelt "timout" that was
# silently ignored would be a check that quietly never happened.
STEP_KINDS: dict[str, tuple[str, ...]] = {
    "wait": (),
    "waitFrames": ("timeout",),
    "until": ("timeout", "every"),
    "console": ("expect",),
    "press": ("times", "hold", "action"),
    "click": ("times", "hold", "button", "viewport"),
    "clickText": ("nth", "times", "hold", "button"),
    "move": ("viewport",),
    "scroll": (),
    "type": (),
    "input": (),
    "timeScale": (),
    "pause": (),
    "step": ("timeout",),
    "screenshot": ("width", "height"),
    "burst": ("count", "every", "everySeconds", "width", "height", "timeout"),
    "expectText": ("ignoreCase",),
    "expectNoText": ("ignoreCase",),
    "expectField": (),
    "expectControl": (),
    "expectNoErrors": (),
}

# What an expectControl (or an until's "control") may say about a button.
CONTROL_KEYS = ("label", "path", "interactable", "present")

COMPARATORS = ("equals", "notEquals", "atLeast", "atMost", "contains")

REPORTS_FOLDER = "playthroughs"


class StepFailed(AssertionError):
    """A step that ran and found the game other than the script expected."""


# ======================================================
# Reading and checking a script
# ======================================================

def load_script(source: str | Path | dict) -> dict:
    """A script from a dict, a JSON file, or JSON text; its name defaults to the file's."""
    if isinstance(source, dict):
        return dict(source)

    path = Path(source)
    if path.suffix.lower() == ".json" or path.exists():
        script = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(script, dict):
            script.setdefault("name", path.stem)
        return script

    return json.loads(str(source))


def _kind_of(step: dict) -> list[str]:
    return [key for key in step if key in STEP_KINDS]


def validate(script: Any) -> list[str]:
    """Everything wrong with a script, before anything is sent to Unity. Empty means runnable."""
    problems: list[str] = []

    if not isinstance(script, dict):
        return ["a playthrough is a JSON object with a 'steps' list"]

    steps = script.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("'steps' must be a non-empty list")
        steps = []

    seed = script.get("seed", "fresh")
    if seed not in ("fresh", "keep", "real"):
        problems.append(f"seed is fresh, keep or real, not {seed!r}")

    for index, step in enumerate(steps, start=1):
        where = f"step {index}"
        if not isinstance(step, dict):
            problems.append(f"{where} is not an object")
            continue

        kinds = _kind_of(step)
        if len(kinds) != 1:
            named = ", ".join(kinds) if kinds else "none"
            problems.append(f"{where} must be exactly one of {', '.join(STEP_KINDS)} (it names {named})")
            continue

        kind = kinds[0]
        allowed = set(STEP_KINDS[kind]) | {kind, "label"}
        extra = sorted(set(step) - allowed)
        if extra:
            problems.append(f"{where} ({kind}) does not take {', '.join(extra)}")

        value = step[kind]
        if kind in ("wait", "timeScale") and not isinstance(value, (int, float)):
            problems.append(f"{where}: {kind} is a number")
        if kind in ("waitFrames", "step") and not (isinstance(value, int) and value > 0):
            problems.append(f"{where}: {kind} is a whole number of frames above 0")
        if kind == "console" and not (isinstance(value, str) or
                                      (isinstance(value, list) and all(isinstance(v, str) for v in value))):
            problems.append(f"{where}: console is a line or a list of lines")
        if kind == "expectField":
            problems.extend(f"{where}: {problem}" for problem in _field_problems(value))
        if kind == "until":
            if not isinstance(value, dict) or len([k for k in ("text", "noText", "field", "control") if k in value]) != 1:
                problems.append(f"{where}: until is {{\"text\": ...}}, {{\"noText\": ...}}, "
                                f"{{\"field\": {{...}}}} or {{\"control\": {{...}}}}")
            elif "field" in value:
                problems.extend(f"{where}: {problem}" for problem in _field_problems(value["field"]))
            elif "control" in value:
                problems.extend(f"{where}: {problem}" for problem in _control_problems(value["control"]))
        if kind == "expectControl":
            problems.extend(f"{where}: {problem}" for problem in _control_problems(value))
        if kind == "clickText" and not (isinstance(value, str) and value.strip()):
            problems.append(f"{where}: clickText is the text on the button to click")
        if kind == "input" and not (isinstance(value, list) and value and all(isinstance(v, dict) for v in value)):
            problems.append(f"{where}: input is a list of send_input actions")

    return problems


def _field_problems(spec: Any) -> list[str]:
    if not isinstance(spec, dict):
        return ["a field check is an object with target, field and one comparison"]
    problems = []
    if not spec.get("target") or not spec.get("field"):
        problems.append("a field check needs target and field")
    compared = [key for key in COMPARATORS if key in spec]
    if len(compared) != 1:
        problems.append(f"a field check needs exactly one of {', '.join(COMPARATORS)}")
    extra = sorted(set(spec) - {"target", "component", "field", *COMPARATORS})
    if extra:
        problems.append(f"a field check does not take {', '.join(extra)}")
    return problems


def _control_problems(spec: Any) -> list[str]:
    if not isinstance(spec, dict):
        return ["a control check is an object with a label or a path"]
    problems = []
    if not spec.get("label") and not spec.get("path"):
        problems.append("a control check needs a label (its text) or a path (the end of its name)")
    extra = sorted(set(spec) - set(CONTROL_KEYS))
    if extra:
        problems.append(f"a control check does not take {', '.join(extra)}")
    return problems


def _matching_controls(controls: list[dict], spec: dict) -> list[dict]:
    """Controls whose label is the given text, or whose path ends with the given name, top of the screen first."""
    found = []
    for control in controls:
        if spec.get("label") is not None and (control.get("label") or "").strip() != str(spec["label"]).strip():
            continue
        if spec.get("path") and not str(control.get("path", "")).endswith(str(spec["path"])):
            continue
        found.append(control)
    return sorted(found, key=lambda control: (-(control.get("center") or [0, 0])[1],
                                              (control.get("center") or [0, 0])[0]))


def _control_check(controls: list[dict], spec: dict) -> tuple[bool, str]:
    """Whether a control is there (or not) and interactable (or not), and the sentence that says so."""
    name = spec.get("label") or spec.get("path")
    found = _matching_controls(controls, spec)
    if spec.get("present") is False:
        return not found, f"{name!r} is {'still ' if found else 'not '}on screen"
    if not found:
        return False, f"no control {name!r} on screen"
    if "interactable" in spec:
        state = bool(found[0].get("interactable"))
        wanted = bool(spec["interactable"])
        return state == wanted, f"{name!r} is {'enabled' if state else 'disabled'}"
    return True, f"{name!r} is on screen"


def _compare(value: Any, spec: dict) -> tuple[bool, str]:
    """Whether a field's value passes its check, and the sentence that says so."""
    for key in COMPARATORS:
        if key not in spec:
            continue
        expected = spec[key]

        if key in ("atLeast", "atMost"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False, f"is {value!r}, which is not a number to compare with {expected!r}"
            passed = number >= float(expected) if key == "atLeast" else number <= float(expected)
            word = "at least" if key == "atLeast" else "at most"
            return passed, f"is {value!r}, {'' if passed else 'not '}{word} {expected!r}"

        if key == "contains":
            passed = str(expected) in (json.dumps(value) if isinstance(value, (list, dict)) else str(value))
            return passed, f"is {value!r}, which {'contains' if passed else 'does not contain'} {expected!r}"

        same = _same(value, expected)
        if key == "equals":
            return same, f"is {value!r}{'' if same else f', not {expected!r}'}"
        return not same, f"is {value!r}{', as it should not be' if same else ''}"

    return False, "has no comparison"


def _same(value: Any, expected: Any) -> bool:
    # A flag is not a number, though Python says True == 1: "is the machine
    # built" answered with a count of one would otherwise pass.
    if isinstance(value, bool) or isinstance(expected, bool):
        return isinstance(value, bool) and isinstance(expected, bool) and value == expected
    # The bridge sends every number as a double, so 3 and 3.0 must meet.
    if isinstance(value, (int, float)) and isinstance(expected, (int, float)):
        return float(value) == float(expected)
    return value == expected


# ======================================================
# What a run produced
# ======================================================

@dataclass
class StepResult:
    index: int
    kind: str
    label: str
    ok: bool
    message: str
    seconds: float
    evidence: dict = field(default_factory=dict)


@dataclass
class PlaythroughResult:
    name: str
    ok: bool
    steps: list[StepResult]
    errors: list[dict]
    real_save_safe: bool | None
    screenshots: list[str]
    started_at: str
    seconds: float
    problem: str | None = None
    report_path: str | None = None

    @property
    def failed(self) -> StepResult | None:
        return next((step for step in self.steps if not step.ok), None)

    def summary(self) -> str:
        passed = sum(1 for step in self.steps if step.ok)
        head = f"{self.name}: {'PASSED' if self.ok else 'FAILED'} -- {passed} of {len(self.steps)} step(s) passed"
        lines = [head + f" in {self.seconds:.1f}s."]
        if self.problem:
            lines.append(f"  {self.problem}")
        if self.failed is not None:
            lines.append(f"  Step {self.failed.index} ({self.failed.label}): {self.failed.message}")
        if self.errors:
            first = (self.errors[0].get("message") or "?").splitlines()[0]
            lines.append(f"  The console raised {len(self.errors)} error(s); first: {first[:200]}")
        if self.real_save_safe is False:
            lines.append("  The REAL save changed during the run -- see ARIA/last_play.json.")
        elif self.real_save_safe is None:
            lines.append("  Whether the real save was untouched is not known (the session was left running, "
                         "or its report did not arrive).")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["failed"] = asdict(self.failed) if self.failed else None
        return data

    def to_markdown(self) -> str:
        lines = [f"# Playthrough: {self.name}", "",
                 f"**{'PASSED' if self.ok else 'FAILED'}** at {self.started_at}, {self.seconds:.1f} s.", ""]
        if self.problem:
            lines += [f"Problem: {self.problem}", ""]
        lines += ["| # | Step | Result | Detail |", "|---|---|---|---|"]
        for step in self.steps:
            detail = step.message.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {step.index} | {step.label} | {'ok' if step.ok else 'FAILED'} | {detail} |")
        lines.append("")
        if self.errors:
            lines += ["## Console errors", ""]
            for error in self.errors:
                first = (error.get("message") or "?").splitlines()[0]
                repeat = f" (x{error['repeat']})" if error.get("repeat") else ""
                lines.append(f"- {error.get('type')}: {first}{repeat}")
            lines.append("")
        if self.screenshots:
            lines += ["## Screenshots", ""] + [f"- {path}" for path in self.screenshots] + [""]
        safe = {True: "untouched", False: "CHANGED", None: "not checked"}[self.real_save_safe]
        lines.append(f"Real save: {safe}.")
        return "\n".join(lines) + "\n"


# ======================================================
# Running one
# ======================================================

def _label(step: dict, kind: str) -> str:
    if step.get("label"):
        return str(step["label"])
    value = step[kind]
    shown = json.dumps(value) if not isinstance(value, str) else value
    if len(shown) > 60:
        shown = shown[:57] + "..."
    return f"{kind} {shown}"


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "shot"


class PlaythroughRunner:
    """Plays scripts through one bridge. See the module docstring for the script."""

    def __init__(self, bridge: Any, *, keep_playing: bool = False, stop_on_failure: bool = True,
                 start_timeout: float = 90.0, poll: float = 0.25,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 write_reports: bool = True) -> None:
        self.bridge = bridge
        self.keep_playing = keep_playing
        self.stop_on_failure = stop_on_failure
        self.start_timeout = start_timeout
        self.poll = poll
        self.sleep = sleep
        self.monotonic = monotonic
        self.write_reports = write_reports
        self._shots: list[str] = []
        self._run_folder = ""

    # --- the run ---------------------------------------------------------

    def run(self, source: str | Path | dict) -> PlaythroughResult:
        script = load_script(source)
        name = str(script.get("name") or "playthrough")
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        began = self.monotonic()
        self._shots = []
        self._run_folder = f"ARIA/{REPORTS_FOLDER}/{_safe_name(name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        def finish(ok: bool, steps: list[StepResult], errors: list[dict], safe: bool | None,
                   problem: str | None = None) -> PlaythroughResult:
            result = PlaythroughResult(name=name, ok=ok, steps=steps, errors=errors, real_save_safe=safe,
                                       screenshots=list(self._shots), started_at=started_at,
                                       seconds=self.monotonic() - began, problem=problem)
            if self.write_reports:
                result.report_path = self._write_report(result)
            return result

        problems = validate(script)
        if problems:
            return finish(False, [], [], None, "The script was not run: " + "; ".join(problems))

        try:
            previous = self.bridge.last_play()
            self._start(script.get("seed", "fresh"))
        except (ueb.UnityBridgeError, StepFailed) as error:
            return finish(False, [], [], None, f"The game could not be started: {error}")

        steps: list[StepResult] = []
        for index, step in enumerate(script["steps"], start=1):
            result = self._run_step(index, step)
            steps.append(result)
            if not result.ok:
                self._evidence_of_failure(result)
                if self.stop_on_failure:
                    break

        errors: list[dict] = []
        try:
            log = self.bridge.get_log(session=True, types="errors", limit=50) or {}
            errors = list(log.get("entries") or [])
        except ueb.UnityBridgeError as error:
            errors = [{"type": "bridge", "message": f"could not read the console: {error}"}]

        safe: bool | None = None
        if not self.keep_playing:
            safe = self._stop_and_check(previous)

        ok = all(step.ok for step in steps) and len(steps) == len(script["steps"])
        if errors and not script.get("allowErrors", False):
            ok = False
        if safe is False:
            ok = False
        return finish(ok, steps, errors, safe)

    def _start(self, seed: str) -> None:
        """Start a session on the test save, and touch no other.

        A game already running is stopped first only when it is a test
        session -- one an earlier run left behind. Anything else is someone
        playing their own save: the bridge would refuse to drive it, and
        stopping it makes the game save, so the run is refused and the session
        left alone. The same holds for a Play pressed by hand in the moment
        between looking and asking, which the bridge answers "Already
        playing" rather than starting anything.
        """
        data = self.bridge.ping() or {}
        if data.get("isPlaying"):
            if not data.get("testSave"):
                raise StepFailed("the editor is already playing on the real save -- someone may be "
                                 "playing it; the session was left alone")
            self.bridge.set_play_mode(False)
            self._wait_playing(False)

        answer = self.bridge.set_play_mode(True, seed=seed) or {}
        if answer.get("changed") is False:
            raise StepFailed("the editor was already playing when the run asked to start -- a session "
                             "the run did not begin; it was left alone")

        started = self._wait_playing(True, min_frame=5)
        if not started.get("testSave"):
            # This one the run did start, so stopping it is the run's to do --
            # and at once, before the game has a chance to save over the player.
            self.bridge.set_play_mode(False)
            raise StepFailed("the session started on the real save, not the test save; it was stopped")

    def _wait_playing(self, playing: bool, min_frame: int = 0) -> dict:
        deadline = self.monotonic() + self.start_timeout
        while True:
            try:
                data = self.bridge.ping() or {}
            except ueb.UnityBridgeTimeout:
                data = {}
            if bool(data.get("isPlaying")) == playing and (not playing or data.get("frame", 0) >= min_frame):
                return data
            if self.monotonic() >= deadline:
                raise StepFailed(f"the game was not {'playing' if playing else 'stopped'} after "
                                 f"{self.start_timeout:g}s")
            self.sleep(self.poll)

    def _stop_and_check(self, previous: dict | None) -> bool | None:
        """Stop the game and read the session's report: True if the real save came through untouched."""
        try:
            self.bridge.set_play_mode(False)
            self._wait_playing(False)
        except (ueb.UnityBridgeError, StepFailed):
            return None

        # The report is written as play ends; the one from before this run
        # must not be mistaken for it.
        deadline = self.monotonic() + 10
        while True:
            report = self.bridge.last_play()
            if report and report != previous:
                safe = report.get("realSaveSafe")
                return bool(safe) if safe is not None else None
            if self.monotonic() >= deadline:
                return None
            self.sleep(self.poll)

    def _run_step(self, index: int, step: dict) -> StepResult:
        kind = _kind_of(step)[0]
        label = _label(step, kind)
        began = self.monotonic()
        evidence: dict = {}
        try:
            message = getattr(self, f"_do_{kind}")(step, evidence)
            ok = True
        except StepFailed as failure:
            message, ok = str(failure), False
        except ueb.UnityBridgeError as error:
            message, ok = f"the bridge refused: {error}", False
        return StepResult(index=index, kind=kind, label=label, ok=ok, message=message,
                          seconds=round(self.monotonic() - began, 3), evidence=evidence)

    def _evidence_of_failure(self, result: StepResult) -> None:
        """A picture of the moment a step failed, and what was on screen as text."""
        try:
            shot = self.bridge.screenshot(f"{self._run_folder}/failed_step{result.index}.png") or {}
            if shot.get("path"):
                self._shots.append(shot["path"])
                result.evidence["screenshot"] = shot["path"]
        except ueb.UnityBridgeError:
            pass
        try:
            result.evidence.setdefault("screen", (self.bridge.read_screen() or {}).get("lines"))
        except ueb.UnityBridgeError:
            pass

    def _write_report(self, result: PlaythroughResult) -> str | None:
        root = getattr(self.bridge, "project_root", None)
        if root is None:
            return None
        folder = Path(root) / self._run_folder
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "report.json").write_text(json.dumps(result.to_dict(), indent=2, default=str),
                                                encoding="utf-8")
            (folder / "report.md").write_text(result.to_markdown(), encoding="utf-8")
        except OSError:
            return None
        return str(folder / "report.md")

    # --- the steps -------------------------------------------------------

    def _do_wait(self, step: dict, evidence: dict) -> str:
        self.sleep(float(step["wait"]))
        return f"waited {step['wait']:g}s"

    def _do_waitFrames(self, step: dict, evidence: dict) -> str:
        start = (self.bridge.ping() or {}).get("frame", 0)
        want = start + int(step["waitFrames"])
        deadline = self.monotonic() + float(step.get("timeout", 30))
        while True:
            frame = (self.bridge.ping() or {}).get("frame", 0)
            if frame >= want:
                return f"frame {frame}"
            if self.monotonic() >= deadline:
                raise StepFailed(f"only reached frame {frame} of {want}: is the game advancing?")
            self.sleep(self.poll)

    def _do_until(self, step: dict, evidence: dict) -> str:
        condition = step["until"]
        timeout = float(step.get("timeout", 10))
        every = float(step.get("every", self.poll))
        deadline = self.monotonic() + timeout
        last = ""
        while True:
            passed, last = self._condition(condition, evidence)
            if passed:
                return last
            if self.monotonic() >= deadline:
                raise StepFailed(f"still not so after {timeout:g}s: {last}")
            self.sleep(every)

    def _condition(self, condition: dict, evidence: dict) -> tuple[bool, str]:
        if "control" in condition:
            return _control_check(self._controls(), condition["control"])
        if "field" in condition:
            spec = condition["field"]
            value = self._field(spec)
            passed, said = _compare(value, spec)
            return passed, f"{spec['field']} {said}"
        lines = self._lines()
        evidence["screen"] = lines
        if "text" in condition:
            found = any(condition["text"] in line for line in lines)
            return found, f"{'found' if found else 'no line reads'} {condition['text']!r}"
        found = any(condition["noText"] in line for line in lines)
        return not found, f"{'a line still reads' if found else 'no line reads'} {condition['noText']!r}"

    def _do_console(self, step: dict, evidence: dict) -> str:
        lines = [step["console"]] if isinstance(step["console"], str) else list(step["console"])
        data = self.bridge.run_console(*lines) or {}
        outputs = [str(entry.get("output", "")) for entry in data.get("ran") or []]
        evidence["output"] = outputs
        said = "\n".join(outputs)
        if "expect" in step and str(step["expect"]) not in said:
            raise StepFailed(f"expected {step['expect']!r} in the console's answer, which was: "
                             f"{said[:300]!r}")
        return said.splitlines()[0][:200] if said else "(no output)"

    def _send(self, *actions: dict) -> None:
        self.bridge.send_input(*actions)
        self.bridge.wait_for_input()

    def _do_press(self, step: dict, evidence: dict) -> str:
        self._send(ueb.Inputs.key(step["press"], action=step.get("action"),
                                  hold=step.get("hold"), times=step.get("times")))
        return f"pressed {step['press']}" + (f" x{step['times']}" if step.get("times") else "")

    def _do_click(self, step: dict, evidence: dict) -> str:
        self._send(ueb.Inputs.click(step["click"], button=step.get("button"), hold=step.get("hold"),
                                    times=step.get("times"), viewport=bool(step.get("viewport"))))
        return f"clicked {step['click']}"

    def _do_clickText(self, step: dict, evidence: dict) -> str:
        """Click a button by the text on it.

        A game's buttons are usually all called "Button"; what tells them
        apart is what they say. nth picks among buttons with the same text,
        counting from the top of the screen.
        """
        want = str(step["clickText"])
        found = _matching_controls(self._controls(), {"label": want})
        nth = int(step.get("nth", 0))
        if len(found) <= nth:
            raise StepFailed(f"no button reads {want!r}" + (f" (wanted number {nth + 1}, found {len(found)})"
                                                               if found else ""))
        target = found[nth]
        evidence["at"] = target.get("center")
        evidence["path"] = target.get("path")
        if not target.get("interactable", True):
            raise StepFailed(f"{want!r} is on screen but disabled")
        self._send(ueb.Inputs.click(target["center"], button=step.get("button"), hold=step.get("hold"),
                                    times=step.get("times")))
        return f"clicked {want!r} at {target.get('center')}"

    def _controls(self) -> list[dict]:
        return list((self.bridge.read_screen() or {}).get("controls") or [])

    def _do_expectControl(self, step: dict, evidence: dict) -> str:
        passed, said = _control_check(self._controls(), step["expectControl"])
        if not passed:
            raise StepFailed(said)
        return said

    def _do_move(self, step: dict, evidence: dict) -> str:
        self._send(ueb.Inputs.move(step["move"], viewport=bool(step.get("viewport"))))
        return f"moved to {step['move']}"

    def _do_scroll(self, step: dict, evidence: dict) -> str:
        self._send(ueb.Inputs.scroll(step["scroll"]))
        return f"scrolled {step['scroll']}"

    def _do_type(self, step: dict, evidence: dict) -> str:
        self._send(ueb.Inputs.text(step["type"]))
        return f"typed {step['type']!r}"

    def _do_input(self, step: dict, evidence: dict) -> str:
        self._send(*step["input"])
        return f"sent {len(step['input'])} action(s)"

    def _do_timeScale(self, step: dict, evidence: dict) -> str:
        data = self.bridge.set_time(float(step["timeScale"])) or {}
        return f"time scale {data.get('timeScale', step['timeScale'])}"

    def _do_pause(self, step: dict, evidence: dict) -> str:
        self.bridge.set_time(paused=bool(step["pause"]))
        return "paused" if step["pause"] else "unpaused"

    def _do_step(self, step: dict, evidence: dict) -> str:
        self.bridge.set_time(step=int(step["step"]))
        data = self.bridge.wait_for_steps(timeout=float(step.get("timeout", 30))) or {}
        return f"stepped to frame {data.get('frame')}"

    def _do_screenshot(self, step: dict, evidence: dict) -> str:
        path = f"{self._run_folder}/{_safe_name(str(step['screenshot']))}.png"
        data = self.bridge.screenshot(path, width=step.get("width"), height=step.get("height")) or {}
        written = data.get("path", path)
        self._shots.append(written)
        evidence["screenshot"] = written
        return f"saved {written}"

    def _do_burst(self, step: dict, evidence: dict) -> str:
        path = f"{self._run_folder}/{_safe_name(str(step['burst']))}"
        self.bridge.screenshot(path, count=int(step.get("count", 8)), every=step.get("every"),
                               every_seconds=step.get("everySeconds"),
                               width=step.get("width"), height=step.get("height"))
        manifest = self.bridge.wait_for_burst(timeout=float(step.get("timeout", 60))) or {}
        if manifest.get("error"):
            raise StepFailed(f"the burst stopped: {manifest['error']}")
        sheet = manifest.get("sheet")
        if sheet:
            self._shots.append(sheet)
            evidence["sheet"] = sheet
        return f"{manifest.get('taken', '?')} shots" + (f", sheet {sheet}" if sheet else "")

    def _lines(self) -> list[str]:
        return list((self.bridge.read_screen() or {}).get("lines") or [])

    def _do_expectText(self, step: dict, evidence: dict) -> str:
        lines = self._lines()
        evidence["screen"] = lines
        want = str(step["expectText"])
        fold = (lambda text: text.lower()) if step.get("ignoreCase") else (lambda text: text)
        if any(fold(want) in fold(line) for line in lines):
            return f"found {want!r}"
        # Thirty lines, not a dozen: the HUD alone is a dozen, and a report
        # that stops before the room's own text looks like the room was blank.
        raise StepFailed(f"no visible line reads {want!r}; the screen says {lines[:30]!r}")

    def _do_expectNoText(self, step: dict, evidence: dict) -> str:
        lines = self._lines()
        evidence["screen"] = lines
        want = str(step["expectNoText"])
        fold = (lambda text: text.lower()) if step.get("ignoreCase") else (lambda text: text)
        hits = [line for line in lines if fold(want) in fold(line)]
        if hits:
            raise StepFailed(f"{want!r} is on screen: {hits[:3]!r}")
        return f"{want!r} is not on screen"

    def _field(self, spec: dict) -> Any:
        data = self.bridge.get_field(spec["target"], spec.get("component"), spec["field"]) or {}
        return data.get("value")

    def _do_expectField(self, step: dict, evidence: dict) -> str:
        spec = step["expectField"]
        value = self._field(spec)
        evidence["value"] = value
        passed, said = _compare(value, spec)
        sentence = f"{spec['target']}.{spec.get('component') or ''}.{spec['field']} {said}".replace("..", ".")
        if not passed:
            raise StepFailed(sentence)
        return sentence

    def _do_expectNoErrors(self, step: dict, evidence: dict) -> str:
        log = self.bridge.get_log(session=True, types="errors", limit=10) or {}
        count = int(log.get("errors") or 0)
        if count:
            first = (log.get("entries") or [{}])[0].get("message", "")
            raise StepFailed(f"{count} error(s) so far; first: {first.splitlines()[0][:200] if first else '?'}")
        return "no errors so far"


def run_playthrough(source: str | Path | dict, bridge: Any | None = None, **options: Any) -> PlaythroughResult:
    """Run one script through a bridge (the module's default bridge when none is given)."""
    return PlaythroughRunner(bridge or ueb.get_bridge(), **options).run(source)


# ======================================================
# From a shell
# ======================================================

def _scripts(paths: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.glob("*.json")))
        else:
            found.append(path)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aria.unity_playthrough",
        description="Play scripted playthroughs through the Unity editor bridge, on its test save.")
    parser.add_argument("scripts", nargs="+", help="playthrough .json files, or folders of them")
    parser.add_argument("--project", help="the Unity project (default: ARIA_UNITY_PROJECT)")
    parser.add_argument("--keep-playing", action="store_true",
                        help="leave the game running after the last script")
    parser.add_argument("--keep-going", action="store_true",
                        help="play every step even after one fails")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    options = parser.parse_args(argv)

    # A failure quotes what the game's screen said, and a game's screen says
    # things a Windows console cannot encode -- a "HIDE ▴" crashed the
    # summary of a run whose reports had already been written. Escaped, not
    # dropped, so the quote still shows what was there.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    scripts = _scripts(options.scripts)
    if not scripts:
        print("No playthrough scripts found.", file=sys.stderr)
        return 2

    invalid = {}
    for path in scripts:
        try:
            problems = validate(load_script(path))
        except (OSError, ValueError) as error:
            problems = [str(error)]
        if problems:
            invalid[str(path)] = problems
    if invalid:
        for path, problems in invalid.items():
            print(f"{path}:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
        return 2

    bridge = ueb.UnityEditorBridge(options.project)
    results = []
    for number, path in enumerate(scripts):
        last = number == len(scripts) - 1
        runner = PlaythroughRunner(bridge, keep_playing=options.keep_playing and last,
                                   stop_on_failure=not options.keep_going)
        results.append(runner.run(path))

    if options.json:
        print(json.dumps([result.to_dict() for result in results], indent=2, default=str))
    else:
        for result in results:
            print(result.summary())
            if result.report_path:
                print(f"  report: {result.report_path}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
