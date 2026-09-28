"""Marketing renders -- a lit hero shot and a turntable, for a store page.

Run for real on 2026-09-28 on the Ludo dwarf, cheering: a 1920x1080
hero shot in ~10 s (EEVEE) under warm and dramatic light, and a 48-frame
turntable in ~8 s, made into an MP4 and a GIF. What each fix was for is
said where it was made; these hold them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.blender import blender_script_templates as templates
from backend.blender import blender_session


def script(action, **params):
    return templates.build_script([{"action": action, "params": params}])


@pytest.mark.parametrize("lighting", templates.LIGHTINGS)
@pytest.mark.parametrize("backdrop", templates.BACKDROPS)
def test_every_look_builds(lighting, backdrop):
    compile(script("hero_shot", path="C:/h.png", lighting=lighting, backdrop=backdrop), "h", "exec")


def test_the_studio_is_a_matte_sweep_and_leaves_nothing_behind():
    text = script("hero_shot", path="C:/h.png")
    assert "(math.pi / 2.0) * step / 12.0" in text                     # the curve up into the wall
    assert '"Specular IOR Level"' in text                             # matte, or it went light grey
    assert "_restore.undo()" in text and "finally:" in text


def test_a_transparent_backdrop_keeps_the_shadow():
    text = script("hero_shot", path="C:/h.png", backdrop="transparent")
    assert "film_transparent" in text and "is_shadow_catcher = True" in text


def test_cycles_is_asked_for_by_name():
    assert "'cycles', 128)" in script("hero_shot", path="C:/h.png", engine="cycles")
    assert "'eevee', 64)" in script("hero_shot", path="C:/h.png")


def test_the_turntable_refreshes_its_pivot_before_parenting():
    # Parented to a stale pivot, the camera flew off and all 48 frames were empty.
    text = script("turntable", folder="C:/t", frames=24)
    assert "bpy.context.view_layer.update()\n    for _o in [_cam] + _lamps:" in text
    assert "range(24)" in text


def test_a_session_gives_a_turntable_a_folder_and_keeps_no_version(tmp_path):
    session = blender_session.Session("s", root=tmp_path)
    actions = [{"action": "turntable", "params": {}}, {"action": "hero_shot", "params": {}}]
    session._place_pictures(actions, 3)
    assert actions[0]["params"]["folder"].endswith("step_0003_01_turntable")
    assert actions[1]["params"]["path"].endswith("step_0003_02_hero_shot.png")
    assert {"turntable", "hero_shot"} <= blender_session.READ_ONLY_ACTIONS


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_frames_become_an_mp4_and_a_gif(tmp_path):
    from PIL import Image

    folder = tmp_path / "spin_turntable"
    folder.mkdir()
    for i in range(1, 7):
        Image.new("RGB", (64, 64), (40 * i, 60, 90)).save(folder / f"frame_{i:04d}.png")
    made = blender_session.turntable_movie(folder)
    assert made["success"] and made["frames"] == 6
    assert Path(made["mp4"]).stat().st_size > 0 and Path(made["gif"]).stat().st_size > 0


def test_no_frames_is_said(tmp_path):
    assert not blender_session.turntable_movie(tmp_path)["success"]


@pytest.mark.parametrize("said, turntable, hero", [
    ("make a turntable of him in Blender", True, False),
    ("spin him around in Blender", True, False),
    ("take a hero shot of him in Blender", False, True),
    ("make a Steam capsule render in Blender", False, True),
    ("turn the table into stone in Blender", False, False),
])
def test_what_is_heard_as_marketing(said, turntable, hero):
    assert bool(blender_session._TURNTABLE.search(said)) is turntable
    assert bool(blender_session._HERO.search(said)) is hero


class Scene:
    def __init__(self, objects):
        self.objects, self.ran = objects, []

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, actions, **_):
        self.ran.append(actions)
        return {"success": True, "renders": ["h.png"], "notes": []}


def test_the_words_choose_the_look():
    session = Scene([{"name": "Dwarf", "type": "MESH"}, {"name": "Dwarf_LOD1", "type": "MESH"},
                     {"name": "Door", "type": "MESH"}])
    blender_session.answer_command(
        "take a dramatic hero shot of the Dwarf on a transparent background in high quality, portrait, in Blender",
        session)
    [[shot]] = session.ran
    assert shot["action"] == "hero_shot"
    assert shot["params"] == {"lighting": "dramatic", "backdrop": "transparent", "engine": "cycles",
                              "objects": ["Dwarf"], "width": 1080, "height": 1350}
