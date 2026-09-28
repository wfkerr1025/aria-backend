"""ARIA Lite - working on one Blender scene over many steps.

WHY THIS EXISTS
---------------
Every run of blender_actions starts a fresh Blender and forgets it on
exit. That is right for "make me a car" and wrong for the way anybody
actually works in Blender: block it out, look, fix the arm, look
again, try something, take it back. Work like that needs four things
a single run does not have, and this module is those four things:

  A scene that persists. Each step opens the session's scene.blend,
  changes it, and saves it back.

  A version before every change. The file as it stood is copied into
  versions/ first, so `undo` is one step back and nothing is ever lost
  to a step that went wrong -- which is also why clearing the scene is
  allowed here when blender_actions would refuse it.

  Eyes. Unless told not to, every step ends with a render preview of
  what the scene now looks like, so the person -- or Claude, or a
  model -- checks a picture rather than trusting a report.

  A record. log.jsonl holds every step: what was asked, what ran,
  what it made, where the pictures are.

WHO USES IT
-----------
William, through ARIA's chat: typed calls and Blender requests land in
the "chat" session, so "make it red" can follow "build a car".

Claude, from a terminal:

    python -m backend.blender.blender_session run "AddCube('Box')"
    python -m backend.blender.blender_session describe
    python -m backend.blender.blender_session undo
    python -m backend.blender.blender_session actions render_preview

Both see the same folder: <Blender output>/Sessions/<name>/.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from logger import get_logger

logger = get_logger(__name__)

__all__ = ["Session", "answer_command", "picture_markdown", "plan", "summarize", "main"]

DEFAULT_SESSION = "chat"
RENDER_ACTIONS = ("render_preview", "render_image", "compare_reference")

# Actions that change nothing in the scene.
READ_ONLY_ACTIONS = frozenset(RENDER_ACTIONS) | {"describe_scene", "measure_mesh", "measure_rig"}

# Steps whose notes say something worth keeping: measurements, and what
# a brush actually did -- a stroke that moved nothing is a stroke that
# missed, and the picture alone does not always show it.
REPORTING_STEPS = ("measure_mesh", "measure_rig", "describe_scene", "sculpt_stroke",
                   "fit_to_reference", "mirror_shape_key", "transfer_weights", "find_landmarks", "auto_rig", "add_clip")

Work = Union[str, Sequence[Dict[str, Any]]]


def plan(work: Work) -> List[Dict[str, Any]]:
    """Actions from whatever form they came in.

    A list of {"action", "params"} dicts, the same as JSON text, or
    typed calls -- AddCube("Box", size=2) -- one per line. Anything
    else is refused with the reason, before Blender starts.
    """
    if not isinstance(work, str):
        return [{"action": str(step.get("action")), "params": dict(step.get("params") or {})}
                for step in work]

    text = work.strip()
    if not text:
        raise ValueError("there is nothing to run")
    if text[0] in "[{":
        data = json.loads(text)
        return plan([data] if isinstance(data, dict) else data)

    from backend.blender import blender_typed_calls as typed

    actions = typed.plan_blender_calls(text)
    if actions is None:
        raise ValueError(
            "that is not a list of Blender calls. Write one per line, e.g.\n"
            "    AddCube(\"Box\", size=2)\n"
            "    RenderPreview(look=\"clay\")\n"
            "`actions` lists every call.")
    return actions


def _unknown_parameters(actions: Sequence[Dict[str, Any]]) -> List[str]:
    """Parameter names no template reads -- said before Blender starts."""
    problems = []
    for index, step in enumerate(actions, 1):
        name = step["action"]
        if name not in templates.TEMPLATES:
            continue            # build_script names unknown actions itself
        accepted = set(templates.parameters(name))
        for key in step.get("params") or {}:
            if key not in accepted:
                problems.append(
                    f"step {index} ({name}) has no {key!r} parameter -- it takes: "
                    f"{', '.join(sorted(accepted)) or 'nothing'}")
    return problems


class Session:
    """One scene, worked on step by step, with every version kept."""

    def __init__(self, name: str = DEFAULT_SESSION, root: Optional[Path] = None) -> None:
        cleaned = re.sub(r"[^\w.-]", "_", str(name or DEFAULT_SESSION)).strip("._") or DEFAULT_SESSION
        self.name = cleaned
        base = Path(root) if root else blender_actions.output_dir() / "Sessions"
        self.folder = base / cleaned
        self.scene = self.folder / "scene.blend"
        self.versions = self.folder / "versions"
        self.renders = self.folder / "renders"
        self.log = self.folder / "log.jsonl"

    # -- the record ---------------------------------------------------

    def history(self) -> List[dict]:
        if not self.log.is_file():
            return []
        entries = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def _record(self, entry: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")

    def _next_step(self) -> int:
        return len(self.history()) + 1

    # -- versions -----------------------------------------------------

    def _snapshot(self, step: int) -> Optional[Path]:
        if not self.scene.is_file():
            return None
        self.versions.mkdir(parents=True, exist_ok=True)
        copy = self.versions / f"step_{step:04d}_before.blend"
        shutil.copy2(self.scene, copy)
        return copy

    def undo(self) -> dict:
        """Put the scene back the way it was before the last change."""
        kept = sorted(self.versions.glob("step_*_before.blend")) if self.versions.is_dir() else []
        if not kept:
            if self.scene.is_file() and self.history():
                # The first step started from nothing; undoing it is an
                # empty scene, kept aside rather than deleted.
                aside = self.versions / f"undone_{int(time.time())}.blend"
                self.versions.mkdir(parents=True, exist_ok=True)
                shutil.move(str(self.scene), aside)
                self._record({"step": self._next_step(), "undo": True, "to": "empty scene",
                              "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                return {"success": True, "text": "Undone. The scene is empty again "
                                                 f"(the old one is kept at {aside})."}
            return {"success": False, "text": "Nothing to undo."}
        latest = kept[-1]
        if self.scene.is_file():
            shutil.move(str(self.scene), self.versions / f"undone_{int(time.time())}.blend")
        shutil.move(str(latest), self.scene)
        self._record({"step": self._next_step(), "undo": True, "to": latest.name,
                      "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        return {"success": True, "text": f"Undone: the scene is back to {latest.name}."}

    def reset(self) -> dict:
        """Start again from an empty scene. The old one is kept."""
        if not self.scene.is_file():
            return {"success": True, "text": "The scene is already empty."}
        step = self._next_step()
        self._snapshot(step)
        self.scene.unlink()
        self._record({"step": step, "reset": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        return {"success": True, "text": "Started a new empty scene (the old one can be undone back)."}

    # -- working ------------------------------------------------------

    def run(self, work: Work, *, preview: Optional[str] = "material",
            views: Optional[Sequence[str]] = None, size: int = 512,
            on_output: Optional[Callable[[str, str], None]] = None,
            timeout: Optional[int] = None, allow_python: bool = False) -> dict:
        """Do the work on the scene, save it, and look at the result.

        preview: "material", "clay", "final", or None for no picture.
        A step that fails leaves the scene exactly as it was.
        """
        try:
            actions = plan(work)
        except Exception as refused:
            return self._refused(str(refused))

        # Typed calls check their own argument names as they are parsed;
        # JSON is where a misspelt key would otherwise vanish silently.
        # Plans handed over as lists come from ARIA's own mapper and
        # recipes, which are tested where they are written.
        is_json = isinstance(work, str) and work.strip()[:1] in "[{"
        problems = _unknown_parameters(actions) if is_json else []
        if problems:
            return self._refused("Nothing ran.\n" + "\n".join(problems))

        step = self._next_step()
        self.renders.mkdir(parents=True, exist_ok=True)

        # Pictures asked for without somewhere to put them go to renders/.
        for index, entry in enumerate(actions, 1):
            if entry["action"] in RENDER_ACTIONS and not (entry["params"].get("path") or "").strip():
                entry["params"]["path"] = str(
                    self.renders / f"step_{step:04d}_{index:02d}_{entry['action']}.png")

        # Saved BEFORE the preview, so a picture that fails never costs
        # the work it was a picture of.
        # A step that only looks -- renders, comparisons, measurements --
        # is not saved and keeps no version: undo is for changes, and a
        # stack of identical versions from looking would bury them.
        looks_only = all(a["action"] in READ_ONLY_ACTIONS for a in actions)
        full = list(actions)
        if not looks_only:
            full.append({"action": "save_file", "params": {"path": str(self.scene)}})
        wants_picture = preview and not any(a["action"] in RENDER_ACTIONS for a in actions)
        if wants_picture:
            full.append({"action": "render_preview", "params": {
                "path": str(self.renders / f"step_{step:04d}.png"),
                "look": preview, "views": list(views or []), "size": size,
                "skip_empty": True}})

        stamp = lambda: ((self.scene.stat().st_mtime_ns, self.scene.stat().st_size)
                         if self.scene.is_file() else None)
        before = stamp()
        snapshot = None if looks_only else self._snapshot(step)
        self.folder.mkdir(parents=True, exist_ok=True)

        result = blender_actions.run_actions(
            full, blend_file=str(self.scene) if self.scene.is_file() else None,
            on_output=on_output, timeout=timeout,
            # A version was just kept, so clearing and saving back is
            # undoable here -- the loss that guard exists for cannot happen.
            allow_clearing_saved_file=True, allow_python=allow_python)

        # A step that succeeded saved the scene, so its version is kept.
        # Only a failed one is checked for whether it got as far as the
        # save -- and by exact time and size, not the timestamp alone:
        # two saves inside one timestamp tick looked like "no change" and
        # the version that undo needed was thrown away (seen as a test
        # that failed only when the whole suite ran fast).
        if snapshot is not None and not result.get("success") and stamp() == before:
            snapshot.unlink(missing_ok=True)     # nothing changed; nothing to undo to

        outcome = result.get("result") or {}
        entry = {
            "step": step,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "actions": actions,
            "success": bool(result.get("success")),
            "error": result.get("error"),
            "created": outcome.get("created") or [],
            "exported": [p for p in outcome.get("exported") or [] if p != str(self.scene)],
            "renders": outcome.get("renders") or [],
            "comparisons": outcome.get("comparisons") or [],
            "python": outcome.get("python") or [],
            "notes": [s for s in outcome.get("steps") or []
                      if s.get("step") in REPORTING_STEPS or s.get("skipped")],
        }
        if "scene" in outcome:
            entry["scene"] = outcome["scene"]
        self._record({k: v for k, v in entry.items() if k != "scene"})

        entry["ran"] = bool(result.get("ran"))
        entry["session"] = self.name
        entry["scene_file"] = str(self.scene)
        if not result.get("success"):
            entry["output_tail"] = "\n".join((result.get("output") or "").strip().splitlines()[-15:])
        entry["text"] = summarize(entry)
        return entry

    def describe(self) -> dict:
        """What is in the scene now. Changes nothing, saves nothing."""
        if not self.scene.is_file():
            return {"success": True, "scene": {"objects": []},
                    "text": f"Session {self.name!r} is empty."}
        result = blender_actions.run_actions(
            [{"action": "describe_scene", "params": {}}], blend_file=str(self.scene))
        scene = (result.get("result") or {}).get("scene")
        if not result.get("success") or scene is None:
            return {"success": False, "text": f"Could not read the scene: {result.get('error')}"}
        return {"success": True, "scene": scene, "text": describe_text(scene)}

    def look(self, look: str = "material", views: Optional[Sequence[str]] = None,
             size: int = 640, objects: Optional[Sequence[str]] = None,
             show_landmarks: bool = False) -> dict:
        """Render the scene without changing it."""
        if not self.scene.is_file():
            return {"success": False, "text": f"Session {self.name!r} is empty -- nothing to look at."}
        self.renders.mkdir(parents=True, exist_ok=True)
        path = self.renders / f"look_{time.strftime('%Y%m%d_%H%M%S')}_{look}.png"
        params: Dict[str, Any] = {"path": str(path), "look": look, "views": list(views or []),
                                  "size": size, "show_landmarks": show_landmarks}
        if objects:
            params["objects"] = list(objects)
        result = blender_actions.run_actions(
            [{"action": "render_preview", "params": params}], blend_file=str(self.scene))
        renders = (result.get("result") or {}).get("renders") or []
        if not result.get("success"):
            return {"success": False, "text": f"The render failed: {result.get('error')}",
                    "output_tail": (result.get("output") or "")[-2000:]}
        return {"success": True, "renders": renders,
                "text": "Rendered:\n" + "\n".join(f"- {p}" for p in renders[:1])}

    def match(self, obj: str, references: Dict[str, str], *, fits: int = 2,
              rounds: int = 0, good_enough: float = 0.97, size: int = 512,
              on_output: Optional[Callable[[str, str], None]] = None) -> dict:
        """Sculpt `obj` toward reference pictures, round by round.

        references: {view: picture path}, e.g. {"front": "...", "right": "..."}.
        Each round is one Blender launch that changes the model and
        compares again. The first `fits` rounds reshape the outline slice
        by slice (fit_to_reference); the `rounds` after them apply the
        gentle grabs the last comparison suggested, for features the
        outline fit cannot make. A round that makes
        the match worse is undone and the loop stops -- the scene is
        never left worse than the best it reached. Stops early at
        `good_enough`, or when nothing sizeable is left to fix.
        """
        if not self.scene.is_file():
            return {"success": False, "text": f"Session {self.name!r} is empty -- nothing to match."}
        views = {str(v).lower(): str(p) for v, p in references.items()}
        bad = [v for v in views if v not in templates.SCULPT_VIEWS]
        if bad or not views:
            return {"success": False, "text": (
                f"Views to match must be among {', '.join(sorted(templates.SCULPT_VIEWS))}"
                + (f" -- not {', '.join(bad)}." if bad else "."))}
        missing = [p for p in views.values() if not Path(p).is_file()]
        if missing:
            return {"success": False, "text": "No picture at: " + ", ".join(missing)}

        def compare_steps() -> List[dict]:
            return [{"action": "compare_reference", "params": {
                "object": obj, "view": view, "reference": path, "size": size}}
                for view, path in views.items()]

        def score_of(step: dict) -> float:
            found = step.get("comparisons") or []
            return sum(c["score"] for c in found) / len(found) if found else 0.0

        def fit_steps() -> List[dict]:
            return [{"action": "fit_to_reference", "params": {
                "object": obj, "view": view, "reference": path, "size": size}}
                for view, path in views.items()]

        first = self.run(compare_steps(), preview=None, on_output=on_output)
        if not first.get("success"):
            return first
        scores = [score_of(first)]
        latest = first
        stopped = "ran every round"
        # Proportions first, by fitting whole slices -- that cannot dent
        # anything. Then, only if asked, gentle grabs for what is left.
        # Grabs alone were measured: the score rose to 96% while the face
        # filled with bowls.
        plan = ["fit"] * max(0, int(fits)) + ["detail"] * max(0, int(rounds))
        for kind in plan:
            if scores[-1] >= good_enough:
                stopped = "the match was good enough"
                break
            if kind == "fit":
                work = fit_steps()
            else:
                work = [s for c in latest.get("comparisons") or []
                        for d in c.get("differences") or [] for s in d.get("strokes") or []]
                if not work:
                    stopped = "nothing sizeable was left to fix"
                    break
            step = self.run(work + compare_steps(), preview=None, on_output=on_output)
            if not step.get("success"):
                stopped = f"a round failed ({step.get('error')}) and was left out"
                break
            if score_of(step) < scores[-1] - 0.002:
                self.undo()
                stopped = (f"a round made it worse ({score_of(step):.0%}), so it was undone")
                break
            scores.append(score_of(step))
            latest = step

        pictures = [c["picture"] for c in latest.get("comparisons") or []]
        text = (f"Matched {obj} to {len(views)} reference view(s) over {len(scores) - 1} round(s): "
                f"outline match {scores[0]:.0%} -> {scores[-1]:.0%}. Stopped because {stopped}.\n\n"
                + "\n\n".join(comparison_text(c) for c in latest.get("comparisons") or []))
        return {"success": True, "scores": scores, "renders": pictures,
                "comparisons": latest.get("comparisons") or [], "text": text}

    def _refused(self, reason: str) -> dict:
        return {"success": False, "ran": False, "error": reason, "renders": [],
                "created": [], "session": self.name,
                "text": f"I did not run anything. {reason}"}


# ======================================================
# Chat: pictures and the few things said about a scene rather than to it
# ======================================================

def file_url(path: Union[str, Path]) -> str:
    """A local file as file:///D:/... with spaces escaped, for a chat image."""
    from urllib.parse import quote

    return "file:///" + quote(str(Path(path).resolve()).replace("\\", "/"), safe="/:")


def picture_markdown(renders: Sequence[str], caption: str = "Preview") -> str:
    """The sheet as an image the chat window shows, plus where it is on disk.

    The chat allows file:/// images from a local drive and nothing else
    (see webui/components/chat/chat.js), so this renders inline in ARIA
    and is still a readable path anywhere it does not.
    """
    if not renders:
        return ""
    first = renders[0]
    return f"![{caption}]({file_url(first)})\n\n{first}"


_UNDO = re.compile(r"\b(?:undo|take (?:that|it) back|go back a step|revert (?:that|it))\b", re.I)
_RESET = re.compile(r"\b(?:start (?:over|again|fresh)|new (?:blender )?scene|empty (?:the )?scene)\b", re.I)
_DESCRIBE = re.compile(
    r"\b(?:what(?:'s| is) in (?:the|my|this) (?:blender )?scene|list (?:the |all )?objects|"
    r"describe (?:the|my|this) (?:blender )?scene)\b", re.I)
_LOOK = re.compile(r"\b(?:show me|let me see|render|preview|look at|picture of)\b", re.I)
_CLAY = re.compile(r"\b(?:clay|sculpt(?:ing)? view|shape only|form only|matcap)\b", re.I)
_FINAL = re.compile(r"\b(?:final|beauty|with (?:the |its )?(?:own )?lights)\b", re.I)


_MATCH = re.compile(r"\b(?:match|fit)\b", re.I)
# A picture path, spaces allowed, from the drive letter to the extension.
_PICTURE = re.compile(r"[A-Za-z]:[\\/][^\"<>|\r\n?*]*?\.(?:png|jpe?g|webp)\b", re.I)
_VIEW_WORDS = (("right", ("side", "right", "profile")), ("left", ("left",)),
               ("back", ("back", "rear")), ("top", ("top",)), ("front", ("front",)))


def _pick_model(said: str, session: "Session", verb: str, example: str):
    """The mesh a sentence means: the one it names, or the only one there is.

    Returns (name, its describe_scene entry, None), or (None, None, an
    answer asking which) -- never a guess between two.
    """
    scene = session.describe()
    entries = {o["name"]: o for o in (scene.get("scene") or {}).get("objects") or []
               if o.get("type") == "MESH"}
    if not entries:
        return None, None, {"success": False,
                            "text": f"There is nothing in the Blender scene to {verb} yet."}
    named = [m for m in entries if re.search(r"\b" + re.escape(m) + r"\b", said, re.I)]
    if len(named) == 1 or len(entries) == 1:
        name = (named or list(entries))[0]
        return name, entries[name], None
    first = sorted(entries)[0]
    return None, None, {"success": False, "text": (
        f"Which one should I {verb}? The scene has " + ", ".join(sorted(entries))
        + ". Say its name, e.g. \"" + example.format(first) + "\".")}


def _answer_sculpt(said: str, sculpt: dict, session: "Session") -> dict:
    """"Make the nose bigger in Blender" -- landmarks, strokes, a picture."""
    target, entry, problem = _pick_model(said, session, "sculpt",
                                         "make the {}'s nose bigger")
    if problem:
        return problem
    actions: List[dict] = []
    have = set(entry.get("landmarks") or [])
    needed = {s["landmark"] for s in sculpt["strokes"]}
    expression = sculpt.get("expression")
    marked = None
    subdivided = 0
    if needed - have:
        # A body when it stands tall or a body part is asked for; a head
        # otherwise. Body marks include the face, so a head marked first
        # and then asked about its arms gets the body marks added.
        dims = entry.get("dimensions") or [1.0, 1.0, 1.0]
        tall = dims[2] > 1.8 * max(dims[0], dims[1], 1e-9)
        wants_body = any(n.rsplit("_", 1)[0] in templates.BODY_LANDMARK_SIZES
                         or n in templates.BODY_LANDMARK_SIZES for n in needed)
        marked = "body" if tall or wants_body else "head"
        # Too few vertices and a brush makes shelves, not curves: the
        # 12,500-face base body came out with boxy shoulders (measured, in
        # the clay preview). So a sparse model is subdivided once before
        # it is first marked -- quads and UVs kept -- and not after, so
        # landmarks already placed stay on their vertices.
        faces = entry.get("faces") or 0
        if not have and 0 < faces < 40000 and not (entry.get("shape_keys") or expression):
            actions.append({"action": "apply_subdivision",
                            "params": {"object": target, "levels": 1, "apply": True}})
            actions.append({"action": "smooth_shade", "params": {"object": target}})
            subdivided = faces
        actions.append({"action": "find_landmarks", "params": {"object": target, "kind": marked}})
    expression = sculpt.get("expression")
    keys = entry.get("shape_keys") or []
    if expression:
        actions.append({"action": "add_shape_key", "params": {"object": target, "name": expression}})
    elif keys:
        return {"success": False, "text": (
            f"I did not sculpt anything. {target} has expressions ({', '.join(keys[1:])}), and "
            f"reshaping the face under them would leave them behind. Sculpt the face before "
            f"adding expressions, or ask for an expression.")}
    for stroke in sculpt["strokes"]:
        params = {"object": target, **stroke}
        if expression:
            params["shape_key"] = expression
        actions.append({"action": "sculpt_stroke", "params": params})
    if expression:
        actions.append({"action": "set_shape_key",
                        "params": {"object": target, "name": expression, "value": 1.0}})

    outcome = session.run(actions, preview="clay", views=["front", "right", "three_quarter"])
    if not outcome.get("success"):
        return outcome
    lines = []
    if expression:
        lines.append(f"Added {sculpt['summary']} to {target} as a shape key called "
                     f"{expression}, dialled all the way in. Say \"set {expression.lower()} to "
                     f"50% in Blender\" to soften it; the neutral face is untouched underneath.")
    else:
        lines.append(f"Sculpted {target}: {sculpt['summary']}.")
    if subdivided:
        lines.append(f"It had only {subdivided:,} faces, so I subdivided it once first "
                     f"(about {subdivided * 4:,}) to give the brushes enough surface to shape.")
    if marked == "head":
        lines.append("I marked the face first (nose, chin, eyes, cheeks, mouth, jaw, brow, "
                     "ears). Say \"show me the landmarks in Blender\" to check where they are.")
    elif marked == "body":
        lines.append("I marked the body first (shoulders, chest, waist, hips, arms, legs, and "
                     "the face). Say \"show me the landmarks in Blender\" to check where they are.")
    lines.append(picture_markdown(outcome.get("renders") or []))
    lines.append("Say \"undo in Blender\" to take this back.")
    outcome["text"] = "\n\n".join(line for line in lines if line)
    return outcome


_SET_KEY = re.compile(r"\bset (?:the |his |her |its )?([\w ]+?) (?:expression |shape key )?"
                      r"to (\d+(?:\.\d+)?)\s*(%|percent)?", re.I)


def _answer_set_key(said: str, session: "Session") -> Optional[dict]:
    """"Set smile to 50% in Blender" -- only when that shape key exists."""
    found = _SET_KEY.search(said)
    if not found:
        return None
    wanted = found.group(1).strip().lower().replace(" ", "_")
    value = float(found.group(2)) / (100.0 if found.group(3) else 1.0)
    scene = session.describe()
    for o in (scene.get("scene") or {}).get("objects") or []:
        for key in (o.get("shape_keys") or [])[1:]:
            if key.lower() == wanted:
                outcome = session.run([{"action": "set_shape_key", "params": {
                    "object": o["name"], "name": key, "value": value}}],
                    preview="clay", views=["front", "three_quarter"])
                if outcome.get("success"):
                    outcome["text"] = (f"Set {key} on {o['name']} to {value:.0%}.\n\n"
                                       + picture_markdown(outcome.get("renders") or []))
                return outcome
    return None


_RIG = re.compile(r"\b(?:rig(?:s|ged|ging)?|(?:add|give|make)\b.{0,20}\b(?:skeleton|armature|bones))\b",
                  re.I)


def _answer_rig(said: str, session: "Session") -> dict:
    """"Rig him in Blender" -- body landmarks if needed, then a Unity Humanoid skeleton."""
    target, entry, problem = _pick_model(said, session, "rig", "rig the {}")
    if problem:
        return problem
    actions: List[dict] = []
    have = set(entry.get("landmarks") or [])
    if not {"hip_l", "knee_l", "neck", "crown"} <= have:
        actions.append({"action": "find_landmarks", "params": {"object": target, "kind": "body"}})
    actions.append({"action": "auto_rig", "params": {"object": target}})
    outcome = session.run(actions, preview="clay", views=["front", "right", "three_quarter"])
    if outcome.get("success"):
        rig = next((n for n in outcome.get("notes") or [] if n.get("step") == "auto_rig"), {})
        unweighted = rig.get("unweighted") or 0
        lines = [f"Rigged {target}: a {len(rig.get('bones') or [])}-bone skeleton called "
                 f"{rig.get('armature')}, with Unity's Humanoid bone names, so Unity sets it up "
                 f"as a Humanoid on import."]
        if not rig.get("arms", True):
            lines.append("It has no arm bones: the arms could not be told apart from the body "
                         "(held against the sides?).")
        if unweighted:
            lines.append(f"{unweighted} vertices got no weight and will not move with it.")
        lines.append(picture_markdown(outcome.get("renders") or []))
        lines.append("Say \"undo in Blender\" to take it off.")
        outcome["text"] = "\n\n".join(lines)
    return outcome


# Clip words, and the frames that show each one best, from the side that
# shows it best.
_CLIP_WORDS = (
    ("walk", re.compile(r"\b(?:walk(?:s|ing)?|stroll(?:s|ing)?)\b", re.I), ["right"], [1, 7, 13, 19]),
    ("wave", re.compile(r"\bwav(?:e|es|ing)\b", re.I), ["front"], [1, 13, 20, 27]),
    ("jump", re.compile(r"\bjump(?:s|ing)?\b", re.I), ["right"], [1, 9, 17, 27]),
    ("nod", re.compile(r"\bnod(?:s|ding)?\b", re.I), ["right"], [1, 7, 13, 19]),
    ("idle", re.compile(r"\b(?:idle|breath(?:e|es|ing))\b", re.I), ["front"], [1, 25]),
)
_ANIMATE = re.compile(r"\b(?:make|makes|let|have|add|give|animate|animation|cycle|clip)\b", re.I)


def _clip_asked(said: str) -> Optional[str]:
    if not _ANIMATE.search(said):
        return None
    return next((name for name, words, _v, _f in _CLIP_WORDS if words.search(said)), None)


def _answer_clip(said: str, clip: str, session: "Session") -> dict:
    """"Make him walk in Blender" -- rig first if need be, then the clip, shown in frames."""
    target, entry, problem = _pick_model(said, session, "animate", "make the {} walk")
    if problem:
        return problem
    rig = entry.get("parent")
    actions: List[dict] = []
    rigged_now = False
    if not rig:
        if not {"hip_l", "knee_l", "neck", "crown"} <= set(entry.get("landmarks") or []):
            actions.append({"action": "find_landmarks", "params": {"object": target, "kind": "body"}})
        actions.append({"action": "auto_rig", "params": {"object": target}})
        rig, rigged_now = f"{target}_Rig", True
    speed = 0.6 if re.search(r"\bslow(?:ly)?\b", said, re.I) else (
        1.6 if re.search(r"\b(?:fast|quick(?:ly)?|brisk(?:ly)?)\b", said, re.I) else 1.0)
    actions.append({"action": "add_clip", "params": {"armature": rig, "clip": clip, "speed": speed}})
    views, frames = next((v, f) for n, _w, v, f in _CLIP_WORDS if n == clip)
    frames = [max(1, int(round((f - 1) / speed)) + 1) for f in frames]
    actions.append({"action": "render_preview", "params": {
        "look": "clay", "views": views, "frames": frames, "size": 360}})
    outcome = session.run(actions)
    if outcome.get("success"):
        made = next((n for n in outcome.get("notes") or [] if n.get("step") == "add_clip"), {})
        lines = [f"Made a {clip} clip called {made.get('clip', clip.capitalize())} on {rig}"
                 + (f", at {speed:g}x speed" if speed != 1.0 else "") + "."]
        if rigged_now:
            lines.append(f"{target} had no skeleton, so I rigged it first (Unity Humanoid bone names).")
        if made.get("skipped"):
            lines.append("The rig has no " + ", ".join(made["skipped"]) + ", so those parts hold still.")
        clips = made.get("clips") or []
        if len(clips) > 1:
            lines.append("Clips in the file: " + ", ".join(clips) + ". Exported to FBX they arrive "
                         "in Unity as separate clips.")
        lines.append(picture_markdown(outcome.get("renders") or [], f"{clip} frames"))
        lines.append("Say \"undo in Blender\" to take it back.")
        outcome["text"] = "\n\n".join(lines)
    return outcome


def _answer_match(said: str, session: "Session") -> dict:
    """"Match it to D:\\Refs\\front.png and D:\\Refs\\side.png in Blender"."""
    pictures = _PICTURE.findall(said)
    references: Dict[str, str] = {}
    for path in pictures:
        name = Path(path).stem.lower()
        view = next((v for v, words in _VIEW_WORDS if any(w in name for w in words)), None)
        if view is None or view in references:
            view = next(v for v in ("front", "right", "back", "left", "top")
                        if v not in references)
        references[view] = path

    target, _entry, problem = _pick_model(said, session, "match", "match the {} to ...")
    if problem:
        return problem

    outcome = session.match(target, references)
    if outcome.get("success"):
        views = ", ".join(f"{v}: {Path(p).name}" for v, p in references.items())
        outcome["text"] = (f"Fitted {target} to {views}.\n\n" + outcome["text"] + "\n\n"
                           + "\n\n".join(picture_markdown([c["picture"]], f"{c['view']} view")
                                         for c in outcome.get("comparisons") or []))
    return outcome


def answer_command(text: str, session: Optional["Session"] = None, *,
                   allow_look: bool = True, gated: bool = True) -> Optional[dict]:
    """Undo, start over, describe, or look -- or None if it is none of those.

    `allow_look` is off while a sentence might still be a build request:
    "build a car and show me" is a build, and every build already ends
    with a picture.

    `gated` says the sentence named Blender the strict way ("in
    Blender"). Undo and start-over change the scene, so they need it,
    and they are never taken from a question: "how do I undo in
    Blender?" wants an answer, not a lost step. Describing and looking
    change nothing, so "what's in the Blender scene?" is enough.
    """
    from backend.blender.blender_nl_mapping import _ASKING_ABOUT, _WANTS_EXPLANATION

    said = str(text or "")
    if _WANTS_EXPLANATION.search(said):
        return None
    session = session or Session()
    changes_allowed = gated and not _ASKING_ABOUT.match(said)
    if changes_allowed and _UNDO.search(said):
        return {"ran": True, **session.undo()}
    if changes_allowed and _RESET.search(said):
        return {"ran": True, **session.reset()}
    if changes_allowed and _MATCH.search(said) and _PICTURE.search(said):
        return {"ran": True, **_answer_match(said, session)}
    clip = _clip_asked(said) if changes_allowed else None
    if clip:
        return {"ran": True, **_answer_clip(said, clip, session)}
    if changes_allowed and _RIG.search(said):
        return {"ran": True, **_answer_rig(said, session)}
    if changes_allowed:
        answered = _answer_set_key(said, session) if _SET_KEY.search(said) else None
        if answered is not None:
            return {"ran": True, **answered}
        from backend.blender import blender_sculpt_language as language

        sculpt = language.plan(said)
        if sculpt is not None:
            return {"ran": True, **_answer_sculpt(said, sculpt, session)}
    if _DESCRIBE.search(said):
        return {"ran": True, **session.describe()}
    marks = bool(re.search(r"\blandmarks?\b", said, re.I))
    if (allow_look or marks) and (_LOOK.search(said) or marks):
        look = ("clay" if _CLAY.search(said) or marks else
                "final" if _FINAL.search(said) else "material")
        views = [v for v in templates.PREVIEW_VIEWS
                 if re.search(r"\b" + v.replace("_", "[ -]") + r"\b", said, re.I)]
        if re.search(r"\bside\b", said, re.I) and "right" not in views:
            views.append("right")
        if marks and not views:
            views = ["front", "right", "three_quarter"]
        outcome = session.look(look, views or None, show_landmarks=marks)
        if outcome.get("success"):
            outcome["text"] = (f"Here is the scene ({look}).\n\n"
                               + picture_markdown(outcome.get("renders") or []))
        return {"ran": True, **outcome}
    return None


# ======================================================
# Saying what happened
# ======================================================

def summarize(entry: dict) -> str:
    """What a step did, for a person reading chat or a terminal."""
    names = ", ".join(a["action"] for a in entry.get("actions") or [])
    if not entry.get("ran", True):
        return f"I did not run Blender. {entry.get('error')}"
    if not entry.get("success"):
        return (f"Step {entry['step']} failed and the scene was left as it was.\n"
                f"{entry.get('error')}\n\n{entry.get('output_tail', '')}").rstrip()

    lines = [f"Step {entry['step']} done ({names})."]
    if entry.get("created"):
        lines.append("Made: " + ", ".join(entry["created"]) + ".")
    if entry.get("exported"):
        lines.append("Exported:\n" + "\n".join(f"- {p}" for p in entry["exported"]))
    renders = entry.get("renders") or []
    if renders:
        lines.append("Picture: " + renders[0]
                     + (f" (+{len(renders) - 1} single views)" if len(renders) > 1 else ""))
    if entry.get("scene"):
        lines.append(describe_text(entry["scene"]))
    for back in entry.get("python") or []:
        if back:
            lines.append("Python result: " + json.dumps(back)[:2000])
    for comparison in entry.get("comparisons") or []:
        lines.append(comparison_text(comparison))
    for note in entry.get("notes") or []:
        if note.get("step") == "sculpt_stroke":
            lines.append(f"Stroke ({note.get('brush')}) on {note.get('object')}: "
                         f"{note.get('vertices_moved')} vertices moved, the most by "
                         f"{note.get('largest_move')} m over {note.get('dabs')} dabs.")
        elif note.get("step") == "auto_rig":
            unweighted = note.get("unweighted") or 0
            lines.append(f"Rigged {note.get('object')} with {note.get('armature')}: "
                         f"{len(note.get('bones') or [])} bones, named for Unity's Humanoid"
                         + ("" if note.get("arms") else " (no arms -- they were not marked)")
                         + (f". {unweighted} of {note.get('vertices')} vertices got no weight and "
                            f"will not move with it." if unweighted else ". Every vertex is weighted."))
        elif note.get("step") == "find_landmarks":
            placed = note.get("placed") or []
            lines.append(f"Marked {len(placed)} landmarks on {note.get('object')}"
                         + (f" (kept {len(note.get('kept') or [])} already set)" if note.get("kept") else "")
                         + "." + "".join(f" Note: {n}." for n in note.get("notes") or []))
        elif note.get("step") == "mirror_shape_key":
            alone = note.get("vertices_without_a_twin") or 0
            lines.append(f"Mirrored {note.get('source')} into {note.get('made')}"
                         + (f" -- {alone} vertices had no twin on the other side and kept still; "
                            f"the mesh is not symmetric there." if alone else "."))
        elif note.get("step") == "transfer_weights":
            unweighted = note.get("unweighted") or 0
            lines.append(f"Weights from {note.get('source')} onto {note.get('target')}: "
                         f"{note.get('groups')} bone groups"
                         + (f", following {note.get('armature')}" if note.get("armature") else
                            " (no armature found -- it will not move with anything yet)")
                         + (f". {unweighted} of {note.get('vertices')} vertices got no weight and "
                            f"will stay behind when the body moves." if unweighted else
                            ". Every vertex is weighted."))
        elif note.get("skipped"):
            lines.append(f"{note.get('step')}: {note['skipped']}.")
    lines.append(f"Scene: {entry.get('scene_file')}")
    return "\n".join(lines)


def typed_call(step: dict) -> str:
    """An action descriptor written back as the typed call that makes it.

    So a suggestion can be copied, edited and run as it stands --
    by a person in chat, or by Claude in a terminal.
    """
    params = dict(step.get("params") or {})
    name = "".join(part.capitalize() for part in str(step.get("action")).split("_"))
    first = next((key for key in ("object", "path", "name") if key in params), None)
    parts = [repr(params.pop(first))] if first else []
    parts += [f"{key}={value!r}" for key, value in params.items()]
    return f"{name}({', '.join(parts)})"


def comparison_text(comparison: dict) -> str:
    """A comparison against a reference, as a person reads it."""
    lines = [f"Against {Path(comparison.get('reference', '')).name} ({comparison.get('view')} view): "
             f"outline match {comparison.get('score', 0):.0%}."]
    lines += [f"- {p[0].upper()}{p[1:]}." for p in comparison.get("proportions") or []]
    differences = comparison.get("differences") or []
    if not differences:
        lines.append("- No sizeable differences in the outline.")
    for d in differences:
        where = f"{d['at'][0]:.0%} across, {d['at'][1]:.0%} down"
        lines.append(f"- {d['kind'].capitalize()} at {where} ({d['area']:.1%} of the model).")
    strokes = [s for d in differences for s in d.get("strokes") or []]
    if strokes:
        lines.append("Strokes that close them:\n" + "\n".join(typed_call(s) for s in strokes))
    lines.append(f"Picture: {comparison.get('picture')}")
    return "\n".join(lines)


def describe_text(scene: dict) -> str:
    objects = scene.get("objects") or []
    if not objects:
        return "The scene is empty."
    rows = []
    for o in objects:
        size = " x ".join(f"{v:g}" for v in o.get("dimensions") or [])
        extra = []
        if o.get("faces") is not None:
            extra.append(f"{o['faces']} faces")
            if o.get("evaluated_faces") not in (None, o.get("faces")):
                extra.append(f"{o['evaluated_faces']} after modifiers")
        if o.get("materials"):
            extra.append("materials " + ", ".join(o["materials"]))
        if o.get("modifiers"):
            extra.append("modifiers " + ", ".join(o["modifiers"]))
        if o.get("bones"):
            extra.append(f"{len(o['bones'])} bones")
        if o.get("shape_keys"):
            extra.append("shape keys " + ", ".join(o["shape_keys"]))
        if o.get("parent"):
            extra.append(f"parent {o['parent']}")
        location = ", ".join(f"{v:g}" for v in o.get("location") or [])
        rows.append(f"- {o['name']} ({o['type']}) at ({location}), {size} m"
                    + (f"; {'; '.join(extra)}" if extra else ""))
    return f"{len(objects)} object(s):\n" + "\n".join(rows)


def catalogue(action: Optional[str] = None) -> str:
    """Every action, or one in full. The typed-call name is shown too."""
    from backend.blender import blender_typed_calls as typed

    if action:
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", action).lower() if action[:1].isupper() else action
        return templates.describe_action(key)
    lines = []
    for name in templates.known_actions():
        typed_name = "".join(p.capitalize() for p in name.split("_"))
        if typed_name not in typed.OPERATIONS:
            typed_name = "(not typed)"
        lines.append(f"{typed_name:<22} {name}({', '.join(templates.parameters(name))})")
    return "\n".join(lines)


# ======================================================
# The command line
# ======================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.blender.blender_session",
        description="Work on one Blender scene step by step, with a picture after every step.")
    parser.add_argument("--session", "-s", default=DEFAULT_SESSION, help="session name (default: chat)")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    # --json is accepted after the command too ("run ... --json"), since
    # that is where it gets typed; argparse would otherwise refuse it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the full result as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="run typed calls or JSON actions on the scene")
    run.add_argument("work", nargs="?", help="calls; '-' or omitted reads stdin")
    run.add_argument("--file", "-f", help="read the calls from a file")
    run.add_argument("--look", default="material", choices=sorted(templates.PREVIEW_LOOKS))
    run.add_argument("--views", help="comma-separated, e.g. front,right,three_quarter")
    run.add_argument("--size", type=int, default=512)
    run.add_argument("--no-preview", action="store_true")
    run.add_argument("--timeout", type=int)
    run.add_argument("--allow-python", action="store_true",
                     help="let RunPython(...) steps run (never allowed from chat)")

    look = sub.add_parser("look", parents=[common], help="render the scene without changing it")
    look.add_argument("--look", default="material", choices=sorted(templates.PREVIEW_LOOKS))
    look.add_argument("--views")
    look.add_argument("--size", type=int, default=640)
    look.add_argument("--objects", help="comma-separated names to frame")

    sub.add_parser("describe", parents=[common], help="list what is in the scene")
    sub.add_parser("undo", parents=[common], help="step back to before the last change")
    sub.add_parser("reset", parents=[common], help="start an empty scene (the old one is kept)")
    sub.add_parser("history", parents=[common], help="every step so far")
    match = sub.add_parser("match", parents=[common],
                           help="sculpt an object toward reference pictures, round by round")
    match.add_argument("object")
    match.add_argument("references", nargs="+", metavar="VIEW=PICTURE",
                       help="e.g. front=D:/refs/head_front.png right=D:/refs/head_side.png")
    match.add_argument("--fits", type=int, default=2, help="outline-fitting rounds (default 2)")
    match.add_argument("--rounds", type=int, default=0, help="detail grab rounds after (default 0)")
    match.add_argument("--good-enough", type=float, default=0.97)

    actions = sub.add_parser("actions", parents=[common], help="list every action, or explain one")
    actions.add_argument("name", nargs="?")

    args = parser.parse_args(argv)
    session = Session(args.session)
    split = (lambda text: [p.strip() for p in text.split(",") if p.strip()] if text else None)

    if args.command == "actions":
        print(catalogue(args.name))
        return 0
    if args.command == "run":
        if args.file:
            work = Path(args.file).read_text(encoding="utf-8")
        elif args.work and args.work != "-":
            work = args.work
        else:
            work = sys.stdin.read()
        outcome = session.run(work, preview=None if args.no_preview else args.look,
                              views=split(args.views), size=args.size, timeout=args.timeout,
                              allow_python=args.allow_python)
    elif args.command == "look":
        outcome = session.look(args.look, split(args.views), args.size, split(args.objects))
    elif args.command == "describe":
        outcome = session.describe()
    elif args.command == "match":
        pairs = dict(item.split("=", 1) for item in args.references if "=" in item)
        outcome = session.match(args.object, pairs, fits=args.fits, rounds=args.rounds,
                                good_enough=args.good_enough)
    elif args.command == "undo":
        outcome = session.undo()
    elif args.command == "reset":
        outcome = session.reset()
    else:
        entries = session.history()
        outcome = {"success": True, "history": entries, "text": "\n".join(
            f"{e['step']:>4}  {e.get('time', '')}  "
            + ("undo" if e.get("undo") else "reset" if e.get("reset") else
               ("ok   " if e.get("success") else "FAIL ")
               + ", ".join(a["action"] for a in e.get("actions") or []))
            for e in entries) or "No steps yet."}

    print(json.dumps(outcome, indent=2, default=str) if args.json else outcome.get("text", ""))
    return 0 if outcome.get("success", True) else 1


if __name__ == "__main__":
    sys.exit(main())
