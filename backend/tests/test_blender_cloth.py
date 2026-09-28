"""Cloth -- a cape that hangs and moves, baked into bones for Unity.

Run for real on 2026-09-28 on the Ludo dwarf: a cape hung from his back,
simulated through Idle and Walk (and a Jump added after), swinging in
Blender and -- once Unity was told to keep curves for bones that are not
the Humanoid's (0 of them survived before; 200 after) -- in Unity, in its
own colour rather than the dwarf's skin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.blender import blender_script_templates as templates
from backend.blender import blender_session

IMPORTER = Path(__file__).resolve().parents[2] / "Assets" / "ARIA" / "Editor" / "ARIACharacterImport.cs"


def script(action, **params):
    return templates.build_script([{"action": action, "params": params}])


def test_a_cape_is_a_grid_with_its_own_bones():
    text = script("add_cape", armature="Body_Rig", color=[0.1, 0.2, 0.6], length=0.8)
    compile(text, "cape", "exec")
    assert "_cape_build(_rig, 0.8, (0.1, 0.2, 0.6)" in text
    assert repr((templates.CAPE_ROWS, templates.CAPE_COLS, templates.CAPE_CHAINS, templates.CAPE_SEGMENTS)) in text
    assert '"Cape_%d_%d"' in text and 'eb["Chest"]' in text


def test_a_bad_colour_is_refused_before_blender():
    with pytest.raises(templates.BadValue):
        script("add_cape", armature="R", color=["blue", (0.1, 0.2, 0.3)])


def test_the_cloth_runs_two_cycles_and_keeps_the_second():
    text = script("bake_cloth", armature="Body_Rig")
    compile(text, "cloth", "exec")
    assert "last = start + 2 * span if cyclic else end" in text
    assert "keep = start + span if cyclic else start" in text
    assert 'modifiers.new("CYCLES")' in text                       # the clip loops while it runs
    assert "bag.fcurves.remove(fc)" in text                       # a second bake starts clean


def test_a_jump_settles_first_and_plays_once():
    # Run twice, a one-shot clip's second pass began with the cape still
    # flying from the first landing (seen 2026-09-28: a crumple mid-jump).
    text = script("bake_cloth", armature="Body_Rig")
    assert 'cyclic = bool(getattr(action, "use_cyclic", True))' in text
    assert "first = start if cyclic else start - span" in text


def test_the_cape_bends_smoothly_and_misses_the_arms():
    # 5 x 4 bones on one segment each moved as planks in a jump; the arms
    # swinging back through the cape flung it sideways.
    assert templates.CAPE_CHAINS * templates.CAPE_SEGMENTS == 42
    assert templates.CAPE_COLS % (templates.CAPE_CHAINS - 1) == 0
    assert templates.CAPE_ROWS % templates.CAPE_SEGMENTS == 0
    built = script("add_cape", armature="Body_Rig", color=[0.1, 0.2, 0.6])
    assert "for g, wd in downs:" in built                          # blended down the length
    baked = script("bake_cloth", armature="Body_Rig")
    assert "_cape_collider(body)" in baked and '"UpperArm", "LowerArm", "Hand"' in baked
    assert "0.25 * before[key] + 0.5 * raw[f][key] + 0.25 * after[key]" in baked


def test_a_new_clip_asks_for_the_cape_only_if_there_is_one():
    text = script("bake_cloth", armature="Body_Rig", clips=["Walk"], if_cape=True)
    compile(text, "cloth", "exec")
    assert 'skipped="no cape"' in text and "_names = ['Walk']" in text


def test_unity_keeps_the_capes_curves_and_its_own_colour():
    source = IMPORTER.read_text(encoding="utf-8")
    assert "static bool MaskExtraBones" in source and "ConfigureClipFromMask" in source
    assert "kept.ConfigureMaskFromClip(ref mask)" in source         # not lost on the next import
    assert "slots[m].name == sidecar.material" in source            # the cape is not given his skin


@pytest.mark.parametrize("said, cape, move", [
    ("give him a cape in Blender", True, False),
    ("give him a long blue cloak in Blender", True, False),
    ("make the cape move in Blender", False, True),
    ("simulate the cloth in Blender", False, True),
    ("make him wave in Blender", False, False),
])
def test_what_is_heard_as_a_cape(said, cape, move):
    assert bool(blender_session._CAPE_MOVE.search(said)) is move
    assert bool(blender_session._CAPE.search(said) and not move) is cape


class Scene:
    where, undo_hint = "in Blender", "undo"

    def __init__(self, objects, fail_bake=False):
        self.objects, self.ran, self.fail_bake = objects, [], fail_bake

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, actions, **_):
        self.ran.append([a["action"] for a in actions])
        if self.fail_bake and any(a["action"] == "bake_cloth" for a in actions):
            return {"success": False, "error": "'Dwarf_Rig' has no clips to move the cape through"}
        return {"success": True, "renders": [], "notes": [{"step": "bake_cloth", "clips": {"Walk": 80}}]}


RIGGED = [{"name": "Dwarf", "type": "MESH", "parent": "Dwarf_Rig"}, {"name": "Dwarf_Rig", "type": "ARMATURE"}]


def test_a_cape_is_baked_into_his_clips_at_once():
    session = Scene(RIGGED)
    answer = blender_session.answer_command("give him a red cape in Blender", session)
    assert answer["ran"] and session.ran == [["add_cape", "bake_cloth", "render_preview"]]
    assert "Walk" in answer["text"]


def test_without_clips_the_cape_just_hangs():
    session = Scene(RIGGED, fail_bake=True)
    answer = blender_session.answer_command("give him a cape in Blender", session)
    assert session.ran[-1] == ["add_cape"] and "only hangs" in answer["text"]


def test_moving_a_cape_that_is_not_there_says_so():
    answer = blender_session.answer_command("make the cape move in Blender", Scene(RIGGED))
    assert not answer["success"] and "no cape" in answer["text"]
