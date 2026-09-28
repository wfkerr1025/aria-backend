"""Materials that look like something -- procedural, projected, baked for Unity.

Run for real on 2026-09-28: wood, metal, stone, cloth, leather and gold
on spheres (checked close up; gold's roughness narrowed after it came out
blotchy); a wooden ball sent to Unity with its rings baked in; a Ludo
plank texture box-projected onto a crate (one credit) and baked for Unity;
a picture projected from the front, upright. These hold the rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session


def script(action, **params):
    return templates.build_script([{"action": action, "params": params}])


@pytest.mark.parametrize("kind", templates.MATERIAL_KINDS)
def test_every_kind_builds(kind):
    text = script("procedural_material", object="Ball", kind=kind)
    compile(text, kind, "exec")
    assert f"_mk_procedural('Ball_{kind.capitalize()}', '{kind}'" in text


def test_a_colour_tints_the_pattern():
    assert "(0.5, 0.1, 0.1)" in script("procedural_material", object="B", kind="cloth", color=[0.5, 0.1, 0.1])
    with pytest.raises(templates.BadValue):
        script("procedural_material", object="B", kind="cloth", color=[0.5])


@pytest.mark.parametrize("projection", templates.PROJECTIONS)
def test_every_projection_builds(projection):
    compile(script("image_material", object="B", image="C:/p.png", projection=projection), projection, "exec")


def test_a_picture_from_the_front_is_upright():
    # +90 degrees about X sent the picture's up to -Z: upside down.
    assert "(-1.5707963, 0.0, 0.0)" in script("image_material", object="B", image="C:/p.png", projection="front")


def test_baking_shares_one_set_of_maps_and_can_skip_what_unity_reads():
    text = script("bake_material", objects=["Bot_LOD0", "Bot_LOD1"], only_if_needed=True)
    assert "_mk_bake_all([_obj(n) for n in ['Bot_LOD0', 'Bot_LOD1']]" in text and ", True)" in text
    assert '"TEX_NOISE"' in text and 'node.projection != "FLAT"' in text
    with pytest.raises(templates.BadValue):
        script("bake_material")


# ======================================================
# Chat
# ======================================================

@pytest.mark.parametrize("said, kind", [
    ("make it wooden in Blender", "wood"),
    ("make it red wood in Blender", "wood"),
    ("give it a gold finish in Blender", "gold"),
    ("turn the table into stone in Blender", "stone"),
    ("paint Wick metal in my Blender", "metal"),
    ("make him leather", "leather"),
    ("make a wooden table in Blender", None),              # a build, not a dressing
    ("give him a golden crown in Blender", None),          # not a request to gild him
    ("make the nose bigger in Blender", None),
])
def test_what_is_heard_as_a_material(said, kind):
    found = blender_session._MATERIAL_WORD.search(said)
    assert (blender_session._KIND_OF[found.group(1).lower()] if found else None) == kind


class Scene:
    where, undo_hint = "in Blender", "undo"

    def __init__(self, objects):
        self.objects, self.ran = objects, []
        self.folder = Path("C:/nowhere")

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, actions, **_):
        self.ran.append(actions)
        return {"success": True, "renders": []}


GAME = [{"name": "Crate_Game", "type": "EMPTY"},
        {"name": "Crate_LOD0", "type": "MESH", "parent": "Crate_Game"},
        {"name": "Crate_LOD1", "type": "MESH", "parent": "Crate_Game"},
        {"name": "Crate_Sculpt", "type": "MESH"}]


def test_a_material_dresses_every_lod():
    session = Scene(GAME)
    answer = blender_session.answer_command("make it wooden in Blender", session)
    assert answer["ran"]
    [actions] = session.ran
    assert actions[0] == {"action": "procedural_material", "params": {
        "object": "Crate_LOD0", "kind": "wood", "name": "Crate_Wood"}}
    assert actions[1] == {"action": "assign_material", "params": {"object": "Crate_LOD1", "material": "Crate_Wood"}}


def test_a_picture_is_boxed_unless_told_otherwise():
    session = Scene([{"name": "Crate", "type": "MESH"}])
    blender_session.answer_command(r"texture it with D:\Art\planks.png in Blender", session)
    blender_session.answer_command(r"texture it with D:\Art\face.png from the front in Blender", session)
    assert [r[0]["params"]["projection"] for r in session.ran] == ["box", "front"]
    assert session.ran[0][0]["params"]["image"] == r"D:\Art\planks.png"


def test_a_ludo_texture_is_asked_for_and_projected(monkeypatch, tmp_path):
    from backend.ludo import ludo_actions, ludo_client
    from PIL import Image

    asked = []
    monkeypatch.setattr(ludo_actions, "generate_image", lambda prompt, **k: (
        asked.append((prompt, k)), {"success": True, "url": "https://ludo.test/t.webp", "cost": {"calls": 1}})[-1])

    def download(url, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), (120, 90, 60)).save(path, "WEBP")
        return {"success": True, "path": path}
    monkeypatch.setattr(ludo_client, "download", download)
    session = blender_session.Session("t", root=tmp_path)
    ran = []
    monkeypatch.setattr(session, "describe", lambda: {"success": True, "scene": {"objects": [
        {"name": "Crate", "type": "MESH"}]}})
    monkeypatch.setattr(session, "run", lambda actions, **k: (ran.append(actions),
                                                              {"success": True, "renders": []})[-1])
    answer = blender_session.answer_ludo_texture("give it a Ludo texture of mossy cobblestone in Blender", session)
    assert "mossy cobblestone" in asked[-1][0] and asked[-1][1]["image_type"] == "texture"
    picture = ran[-1][0]["params"]["image"]
    assert picture.endswith("mossy_cobblestone.png") and Image.open(picture).format == "PNG"
    assert "1 paid call" in answer["text"]


def test_a_question_about_textures_spends_nothing(monkeypatch):
    from backend.ludo import ludo_actions

    monkeypatch.setattr(ludo_actions, "generate_image", lambda *a, **k: pytest.fail("spent a credit"))
    assert blender_actions.answer_request("how do I get a Ludo texture of moss in Blender?") is None


def test_chat_routes_a_ludo_texture_past_the_other_tool_guard(monkeypatch):
    # Naming Ludo normally hands a sentence away from Blender; this one
    # names it on purpose.
    seen = []
    monkeypatch.setattr(blender_session, "answer_ludo_texture",
                        lambda said, session: (seen.append(said), {"success": True, "text": "ok"})[-1])
    assert blender_actions.answer_request("give it a Ludo texture of moss in Blender")["ran"]
    assert seen == ["give it a Ludo texture of moss in Blender"]


def test_a_colour_word_tints_the_material():
    # _find_color gives (name, rgb); taking the pair as the colour broke
    # "make it red wood" and "give him a blue cape" (found on the cape).
    session = Scene([{"name": "Crate", "type": "MESH"}])
    blender_session.answer_command("make it red wood in Blender", session)
    colour = session.ran[0][0]["params"]["color"]
    assert len(colour) == 3 and all(isinstance(c, float) for c in colour)
