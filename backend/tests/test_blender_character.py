"""A picture in, a playable character out -- the rules around it.

Run for real on 2026-09-28: a Ludo concept of a dwarf (1 credit), turned
into a model (1 credit), cleaned, rigged, T-posed, given Idle and Walk and
sent to Unity, where he is a Humanoid with all 19 bones and walks upright
to his own clips and Mixamo's. Each fix below was found on that dwarf;
these tests hold them without spending credits or starting Blender.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from backend.blender import blender_actions
from backend.blender import blender_character as character
from backend.blender import blender_script_templates as templates
from backend.blender import blender_session


def script(action, **params):
    return templates.build_script([{"action": action, "params": params}])


# ======================================================
# The picture
# ======================================================

def test_a_link_goes_to_ludo_as_it_is():
    assert character._picture_for_ludo("https://x.test/a.png") == "https://x.test/a.png"


def test_a_file_goes_as_a_data_uri(tmp_path):
    picture = tmp_path / "hero.png"
    picture.write_bytes(b"\x89PNG fake")
    uri = character._picture_for_ludo(str(picture))
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"\x89PNG fake"


def test_no_picture_there_is_said(tmp_path):
    with pytest.raises(FileNotFoundError):
        character._picture_for_ludo(str(tmp_path / "missing.png"))


# ======================================================
# The pipeline, with Ludo and Blender stood in for
# ======================================================

class FakeSession(blender_session.Session):
    def __init__(self, root):
        super().__init__("t", root=root)
        self.runs, self.resets = [], 0
        self.objects = [{"name": "world", "type": "EMPTY"},
                        {"name": "geometry_0", "type": "MESH", "parent": "world"}]

    def reset(self):
        self.resets += 1
        return {"success": True}

    def describe(self):
        return {"success": True, "scene": {"objects": self.objects}}

    def run(self, work, **_):
        actions = list(work)
        self.runs.append([a["action"] for a in actions])
        notes = []
        if any(a["action"] == "auto_rig" for a in actions):
            notes = [{"step": "auto_rig", "bones": ["Hips"] * 19, "arms": True}]
        return {"success": True, "renders": ["r.png"], "notes": notes}


@pytest.fixture
def ludo(monkeypatch):
    from backend.ludo import ludo_actions, ludo_client

    asked = {}

    def model(prompt, **kwargs):
        asked.update(kwargs, prompt=prompt)
        return {"success": True, "url": "https://ludo.test/m.glb", "cost": {"calls": 1}}

    monkeypatch.setattr(ludo_actions, "generate_model", model)
    monkeypatch.setattr(ludo_client, "download", lambda url, path: (
        Path(path).parent.mkdir(parents=True, exist_ok=True), Path(path).write_bytes(b"glb"),
        {"success": True, "path": path})[-1])
    return asked


def test_the_stages_run_in_order_in_a_new_scene(tmp_path, ludo, monkeypatch):
    from backend.blender import blender_to_unity

    sent = {}
    monkeypatch.setattr(blender_to_unity, "send", lambda session, **kw: (
        sent.update(kw), {"success": True, "text": "in Unity", "pictures": ["u.png"]})[-1])
    picture = tmp_path / "Dwarf.png"
    picture.write_bytes(b"png")
    session = FakeSession(tmp_path)
    outcome = character.picture_to_character(str(picture), session=session)

    assert outcome["success"] and outcome["name"] == "Dwarf" and sent["name"] == "Dwarf"
    assert ludo["image"].startswith("data:image/png;base64,") and ludo["texture_type"] == "simple"
    assert session.resets == 1                                   # the old scene is kept to undo to
    imported, cleaned, rigged = session.runs
    assert imported == ["clear_scene", "import_model"]
    # Out of Ludo's empty FIRST; where it stands baked in LAST (AutoRig swapped
    # the arms and legs of a mesh left 0.9 m up).
    assert cleaned[0] == "flatten_hierarchy" and cleaned[-2:] == ["apply_transforms", "rename_object"]
    # T-posed before any clip: Unity takes the rest pose AS the T-pose.
    assert rigged[:3] == ["find_landmarks", "auto_rig", "t_pose"] and rigged.index("add_clip") > 2
    assert outcome["pictures"] == ["r.png", "r.png", "u.png"]


def test_a_description_costs_two_calls_and_says_so(tmp_path, ludo, monkeypatch):
    from backend.ludo import ludo_actions

    monkeypatch.setattr(ludo_actions, "generate_character", lambda d, **k: {
        "success": True, "url": "https://ludo.test/m.glb", "cost": {"calls": 2},
        "concept_url": "https://ludo.test/c.png"})
    outcome = character.picture_to_character(description="a dwarf miner", session=FakeSession(tmp_path),
                                             send=False)
    assert "2 paid call(s)" in outcome["text"] and "c.png" in outcome["text"]


def test_a_slow_model_is_collected_not_abandoned(tmp_path, monkeypatch, ludo):
    from backend.ludo import ludo_actions, ludo_client

    monkeypatch.setattr(ludo_actions, "generate_model", lambda *a, **k: {
        "success": False, "pending": True, "job_id": "j1"})
    monkeypatch.setattr(ludo_client, "collect", lambda job, **k: {"url": "https://ludo.test/late.glb"})
    picture = tmp_path / "x.png"
    picture.write_bytes(b"png")
    outcome = character.picture_to_character(str(picture), session=FakeSession(tmp_path), send=False)
    assert outcome["success"]


def test_the_open_blender_is_not_started_over(tmp_path):
    outcome = character.picture_to_character("https://x.test/a.png", session=blender_session.LiveSession(root=tmp_path))
    assert not outcome["success"] and "new scene" in outcome["text"]


# ======================================================
# What Blender is told
# ======================================================

def test_the_export_writes_the_skeleton_at_rest_with_a_rest_take_first():
    text = script("export_fbx", objects=["Rig"], path="C:/x.fbx")
    # Unity reads a model's pose from frame 0 of the FIRST take: Idle sorted
    # first and a dwarf's avatar was built with his arms hanging.
    assert 'bpy.data.actions.new("!Rest")' in text and "bpy.data.actions.remove(_r)" in text
    assert "matrix_basis = mathutils.Matrix.Identity(4)" in text


def test_packed_maps_are_written_beside_the_fbx():
    text = script("export_fbx", objects=["Rig"], path="C:/x.fbx")
    assert "packed_file" in text and '"_" + _role.lower() + ".png"' in text


def test_t_pose_refuses_a_rig_that_already_has_clips():
    text = script("t_pose", armature="Body_Rig")
    assert "already has clips" in text and "armature_apply" in text


def test_the_rig_trusts_the_body_box_over_lopsided_hip_marks():
    text = script("auto_rig", object="Body")
    assert "box_x" in text and "shoulder_mark" in text


def test_apply_transforms_can_bake_where_it_stands():
    assert "location=True" in script("apply_transforms", location=True)
    assert "location=False" in script("apply_transforms")


# ======================================================
# Chat
# ======================================================

@pytest.fixture
def pipeline(monkeypatch):
    from backend.ludo import ludo_client

    calls = []
    monkeypatch.setattr(ludo_client, "api_key", lambda: "k")
    monkeypatch.setattr(character, "picture_to_character", lambda picture=None, **kw: (
        calls.append((picture, kw.get("description"), kw.get("name"))),
        {"success": True, "text": "done", "pictures": []})[-1])
    return calls


@pytest.mark.parametrize("said, picture, description, name", [
    (r"turn D:\Art\wick.png into a playable character", r"D:\Art\wick.png", None, None),
    (r"make a playable character from D:\My Art\hero one.jpg called Wick", r"D:\My Art\hero one.jpg",
     None, "Wick"),
    ("make a playable character of a dwarf miner with a lantern", None, "a dwarf miner with a lantern", None),
    ("turn https://x.test/a.webp into a game character", "https://x.test/a.webp", None, None),
])
def test_what_chat_hands_the_pipeline(pipeline, said, picture, description, name):
    assert blender_actions.answer_request(said)["ran"]
    assert pipeline == [(picture, description, name)]


def test_a_question_about_it_spends_nothing(pipeline):
    assert blender_actions.answer_request("how do I make a playable character in Blender?") is None
    assert pipeline == []


def test_nothing_to_work_from_is_asked_for(pipeline):
    answer = blender_actions.answer_request("make me a playable character")
    assert not answer["ran"] and "picture" in answer["text"] and pipeline == []


def test_without_ludo_it_says_so(monkeypatch):
    from backend.ludo import ludo_client

    def missing():
        raise RuntimeError("no key")
    monkeypatch.setattr(ludo_client, "api_key", missing)
    answer = blender_actions.answer_request(r"turn D:\a.png into a playable character")
    assert not answer["ran"] and "Ludo" in answer["text"]
