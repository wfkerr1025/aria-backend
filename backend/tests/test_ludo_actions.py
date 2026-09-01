"""Asking Ludo.ai for things, without a model deciding to spend money.

WHAT MAKES THIS LAYER DIFFERENT FROM THE BLENDER ONE
----------------------------------------------------
Blender is free to run and can be run again. Every Ludo generation
carries an `x-credit-action` in the spec and spends credits the user
cannot get back. So the failure modes that matter here are not just
"it said it worked and nothing happened" -- they include "it worked
twice", "it spent a credit to be told the enum was wrong", and "a
sentence was read too eagerly and became a charge".

NO TEST HERE TOUCHES THE REAL API
---------------------------------
An autouse fixture replaces requests.post/get inside ludo_client and
fails loudly if anything reaches for the network. A suite that quietly
billed the developer would be a suite nobody could run twice.

The endpoints, parameters and enums these tests assert were read from
https://api.ludo.ai/api-documentation/swagger.json (Ludo.ai API 0.9.9)
before the code was written, not recalled. Auth was verified against
the live free endpoint: GET /auth/validate-api-key answers 204 with an
empty body for a valid key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.ludo import ludo_actions as actions
from backend.ludo import ludo_asset_pipeline as pipeline
from backend.ludo import ludo_client as client
from backend.ludo import ludo_nl_mapping as mapping


# ======================================================
# Fixtures
# ======================================================

class FakeResponse:
    def __init__(self, status=200, payload=None, body=b"", chunks=None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = body or (self.text.encode() if self.text else b"")
        self._chunks = chunks or []

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture(autouse=True)
def no_real_api(monkeypatch):
    """Nothing in this suite may reach Ludo.ai. It costs money."""
    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to call the real Ludo.ai API, which spends credits")

    monkeypatch.setattr(client.requests, "post", refuse)
    monkeypatch.setattr(client.requests, "get", refuse)
    monkeypatch.setenv(client.ENV_KEY, "test-key-not-a-real-one")


@pytest.fixture
def posts(monkeypatch):
    """Record every POST and answer it with a canned result."""
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "body": json or {}, "headers": headers or {}})
        if "/assets/image" in url:
            return FakeResponse(200, {"url": "https://cdn.ludo.ai/a/img.png"})
        if "/assets/3d-model/rig" in url:
            return FakeResponse(200, {"model_url": "https://cdn.ludo.ai/a/rig.glb"})
        if "/assets/3d-model" in url:
            return FakeResponse(200, {"model_url": "https://cdn.ludo.ai/a/m.glb"})
        if "/assets/sprite/animate" in url:
            return FakeResponse(200, {"spritesheet_url": "https://cdn.ludo.ai/a/s.png",
                                      "num_frames": 16})
        if "/assets/video" in url:
            return FakeResponse(200, {"url": "https://cdn.ludo.ai/a/v.mp4"})
        if "/audio/" in url:
            return FakeResponse(200, {"url": "https://cdn.ludo.ai/a/s.mp3",
                                      "type": "sfx"})
        return FakeResponse(200, {"url": "https://cdn.ludo.ai/a/x.bin"})

    monkeypatch.setattr(client.requests, "post", fake_post)
    return sent


# ======================================================
# The client
# ======================================================

def test_the_key_goes_in_a_header_never_a_url(posts):
    actions.generate_image("a barrel")

    call = posts[0]
    assert client.AUTH_HEADER in call["headers"]
    assert call["headers"][client.AUTH_HEADER].startswith(client.AUTH_SCHEME + " ")
    assert "test-key-not-a-real-one" not in call["url"]


def test_the_auth_scheme_is_the_measured_one(posts):
    """The docs page renders the header name as "Authentication:".
    Only "Authorization: ApiKey <key>" is read -- measured against the
    live endpoint, which answers 204 for a valid key."""
    assert client.AUTH_HEADER == "Authorization"
    assert client.AUTH_SCHEME == "ApiKey"


def test_every_call_carries_a_request_id(posts):
    """The API uses request_id to recognise a repeat and not charge
    twice. It is the difference between a retry that is free and a
    retry that is not."""
    actions.generate_image("a barrel")

    assert posts[0]["body"].get("request_id")


def test_two_calls_do_not_share_a_request_id(posts):
    actions.generate_image("a barrel")
    actions.generate_image("a crate")

    assert posts[0]["body"]["request_id"] != posts[1]["body"]["request_id"]


def test_an_unknown_endpoint_is_refused_before_any_request(posts):
    """A caller cannot reach a path whose schema nobody read."""
    with pytest.raises(client.LudoError):
        client.call("teleport", {})

    assert posts == []


@pytest.mark.parametrize("path", ["image", "model_3d", "sprite_animate",
                                  "music", "sound_effect", "voice", "video",
                                  "model_3d_rig", "job", "validate"])
def test_the_endpoint_paths_are_the_ones_the_spec_lists(path):
    """Read from the OpenAPI spec, not remembered. Four earlier
    guesses about the Unity CLI were wrong and each cost a round of
    "it said it worked and nothing happened"."""
    real = {
        "image": "/assets/image", "model_3d": "/assets/3d-model",
        "sprite_animate": "/assets/sprite/animate", "music": "/audio/music",
        "sound_effect": "/audio/sound-effect", "voice": "/audio/voice",
        "video": "/assets/video", "model_3d_rig": "/assets/3d-model/rig",
        "job": "/assets/jobs/{id}", "validate": "/auth/validate-api-key",
    }
    assert client.ENDPOINTS[path] == real[path]


def test_a_value_outside_an_enum_is_refused_without_spending(posts):
    """Sending "8k" as a texture size would cost a credit to be told
    no. The allowlist is checked here first."""
    result = actions.generate_model("a barrel", texture_size=8192)

    assert result["success"] is False
    assert posts == []


def test_an_absent_choice_takes_the_default(posts):
    """None means "not specified", which is how every optional
    parameter works and must not be an error."""
    assert client.choice(None, ("a", "b"), "field", "a") == "a"
    assert client.choice("", ("a", "b"), "field", "a") == "a"


def test_a_choice_is_matched_without_regard_to_case():
    assert client.choice("low poly", client.ART_STYLES, "style") == "Low Poly"


def test_a_queued_generation_is_polled_to_completion(monkeypatch):
    """A 202 means "queued, go poll", and a client that returned it as
    the answer would hand back a job id where an asset was expected."""
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: FakeResponse(
                            202, {"id": "job-1", "status": "queued",
                                  "task_type": "image", "created_at": 0}))
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(url)
        return FakeResponse(200, {"id": "job-1", "status": "succeeded",
                                  "task_type": "image", "created_at": 0,
                                  "result": {"url": "https://cdn/x.png"}})

    monkeypatch.setattr(client.requests, "get", fake_get)

    result = client.call("image", {"prompt": "x", "image_type": "generic"})

    assert result == {"url": "https://cdn/x.png"}
    assert "/assets/jobs/job-1" in seen[0]


def test_polling_uses_the_servers_own_long_poll(monkeypatch):
    """The endpoint waits up to 60 seconds for a terminal state. The
    server knows when the answer is ready better than a timer here."""
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResponse(200, {"id": "j", "status": "succeeded",
                                  "task_type": "image", "created_at": 0,
                                  "result": {"url": "https://cdn/x.png"}})

    monkeypatch.setattr(client.requests, "get", fake_get)
    client.poll_job("j")

    assert captured["params"]["wait"] >= 1


def test_a_failed_job_is_reported_not_returned_empty(monkeypatch):
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, {"id": "j", "status": "failed",
                                  "task_type": "image", "created_at": 0,
                                  "error": {"message": "the prompt was refused"}}))

    with pytest.raises(client.LudoError) as raised:
        client.poll_job("j")

    assert "the prompt was refused" in str(raised.value)


def test_a_job_that_never_finishes_gives_back_its_id(monkeypatch):
    """A generation may outlive the wait. Losing the job id would mean
    a paid-for asset nobody can find."""
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, {"id": "job-7", "status": "running",
                                  "task_type": "image", "created_at": 0}))

    with pytest.raises(client.LudoError) as raised:
        client.poll_job("job-7", total_seconds=1)

    assert "job-7" in str(raised.value)


def test_a_refusal_carries_the_reason(monkeypatch):
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: FakeResponse(
                            400, {"message": "insufficient credits"}))

    result = actions.generate_image("a barrel")

    assert result["success"] is False
    assert "insufficient credits" in result["error"]


def test_an_unreachable_api_is_an_answer_not_a_crash(monkeypatch):
    def explode(*a, **k):
        raise client.requests.RequestException("no route to host")

    monkeypatch.setattr(client.requests, "post", explode)

    result = actions.generate_image("a barrel")

    assert result["success"] is False
    assert "Could not reach Ludo.ai" in result["error"]


@pytest.mark.parametrize("payload,expected", [
    ({"model_url": "a"}, "a"),
    ({"spritesheet_url": "b"}, "b"),
    ({"url": "c"}, "c"),
    ({"video_url": "d"}, "d"),
    ({"num_frames": 4}, None),
    ("not a dict", None),
])
def test_the_asset_url_is_found_whatever_it_is_called(payload, expected):
    """Model3DResult says model_url, SpriteResult says spritesheet_url,
    ImageResult and AudioResult say url. Nothing can just look for
    "url" -- read from the spec's own definitions."""
    assert client.asset_url(payload) == expected


# ======================================================
# No key, no plugin
# ======================================================

def test_no_key_is_a_refusal_that_never_calls(monkeypatch, tmp_path, posts):
    monkeypatch.delenv(client.ENV_KEY, raising=False)
    from backend.plugins import plugin_settings
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    result = actions.generate_image("a barrel")

    assert result["ran"] is False
    assert posts == []


def test_a_switched_off_plugin_is_refused(monkeypatch, tmp_path, posts):
    monkeypatch.delenv(client.ENV_KEY, raising=False)
    from backend.plugins import plugin_settings
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({"ludo": {
        "id": "ludo", "name": "Ludo.ai", "version": "1.0.0", "enabled": False,
        "logo": "", "configPage": "ludo-config", "api_key": "something"}}),
        encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))

    result = actions.generate_image("a barrel")

    assert result["ran"] is False
    assert "switched off" in result["error"]
    assert posts == []


# ======================================================
# The actions
# ======================================================

def test_a_model_is_two_calls_because_there_is_no_text_to_3d(posts):
    """`/assets/3d-model` requires an `image`. An image is generated
    first and then converted, and BOTH cost credits -- which is why
    the steps are reported rather than hidden."""
    result = actions.generate_model("a wooden barrel")

    assert result["success"] is True
    assert result["steps"] == ["image", "model_3d"]
    assert [p["url"].rsplit("/api", 1)[-1] for p in posts] == \
        ["/assets/image", "/assets/3d-model"]


def test_the_generated_image_is_what_gets_converted(posts):
    actions.generate_model("a wooden barrel")

    assert posts[1]["body"]["image"] == "https://cdn.ludo.ai/a/img.png"


def test_polycount_is_the_apis_target_num_faces(posts):
    """The spec's word and the API's word differ; the mapping is here
    so nothing downstream has to know both."""
    actions.generate_model("a barrel", polycount=8000)

    assert posts[1]["body"]["target_num_faces"] == 8000


def test_a_model_defaults_to_a_real_time_budget(posts):
    """The API's own default is 50000, which is a film asset. A game
    prop that arrives unusable is a credit spent for nothing."""
    actions.generate_model("a barrel")

    assert posts[1]["body"]["target_num_faces"] == actions.DEFAULT_FACES
    assert actions.DEFAULT_FACES < 50_000


def test_a_character_is_framed_so_the_conversion_has_something_to_use(posts):
    """A portrait crop makes a bust, not a character."""
    actions.generate_character("a knight")

    assert "full body" in posts[0]["body"]["prompt"]


def test_an_environment_is_an_image_not_a_mesh(posts):
    """Converting a scene to one mesh gives a diorama nobody can walk
    through. "Create a forest" wants a backdrop."""
    result = actions.generate_environment("a pine forest")

    assert result["kind"] == "image"
    assert len(posts) == 1
    assert posts[0]["body"]["image_type"] == "fixed_background"


def test_a_texture_resolution_is_refused_rather_than_ignored(posts):
    """`/assets/image` has no size parameter at all. Accepting a
    number and returning whatever came back would be a setting that
    does nothing, which is worse than one that says so."""
    result = actions.generate_texture("cracked stone", resolution=2048)

    assert result["success"] is False
    assert "no resolution setting" in result["error"]
    assert posts == []


def test_a_texture_without_a_resolution_works(posts):
    result = actions.generate_texture("cracked stone")

    assert result["success"] is True


def test_a_sprite_sheet_is_two_calls_and_a_checked_frame_count(posts):
    """`/assets/sprite/animate` needs an `initial_image`, and `frames`
    is an enum -- 4, 9, 16, 25, 36, 49, 64 -- not a free number."""
    result = actions.generate_sprite_sheet("a walking knight", frames=16)

    assert result["success"] is True
    assert result["steps"] == ["image", "sprite_animate"]
    assert posts[1]["body"]["initial_image"] == "https://cdn.ludo.ai/a/img.png"
    assert posts[1]["body"]["frames"] == 16


def test_a_frame_count_the_api_would_reject_is_caught_first(posts):
    result = actions.generate_sprite_sheet("a knight", frames=17)

    assert result["success"] is False
    assert posts == []


def test_a_voice_with_no_words_asks_instead_of_inventing(posts):
    """`/audio/voice` requires voice_description AND text. A voice
    with nothing to say is not something this can make up."""
    result = actions.generate_voice("a gravelly old wizard")

    assert result["success"] is False
    assert result["ran"] is False
    assert "no words" in result["error"]
    assert posts == []


def test_a_voice_with_words_is_sent_with_both_fields(posts):
    actions.generate_voice("a gravelly old wizard", text="You shall not pass")

    assert posts[0]["body"]["voice_description"] == "a gravelly old wizard"
    assert posts[0]["body"]["text"] == "You shall not pass"


def test_a_rig_defaults_to_bone_names_unity_can_map(posts):
    """"unity" is not one of the API's joint_naming options. Mixamo is
    the one Unity's humanoid avatar mapper handles best."""
    actions.rig_model("https://cdn.ludo.ai/a/m.glb")

    assert posts[0]["body"]["joint_naming"] == "mixamo"


@pytest.mark.parametrize("call", [
    lambda: actions.generate_image(""),
    lambda: actions.generate_model("   "),
    lambda: actions.generate_sprite_sheet(""),
    lambda: actions.generate_audio(""),
    lambda: actions.generate_music(""),
    lambda: actions.generate_voice("", text="hi"),
])
def test_nothing_to_make_is_refused_before_paying_for_it(posts, call):
    result = call()

    assert result["success"] is False
    assert result["ran"] is False
    assert posts == []


def test_an_unexpected_failure_is_an_answer_not_an_exception(monkeypatch):
    """An exception would hand the turn back to a model, which is the
    one outcome this layer exists to prevent."""
    monkeypatch.setattr(client, "call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = actions.generate_image("a barrel")

    assert result["success"] is False
    assert "went wrong" in result["error"]


# ======================================================
# Files
# ======================================================

def test_a_download_writes_the_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"glTF", b"rest"]))
    target = tmp_path / "m.glb"

    result = client.download("https://cdn/x.glb", str(target))

    assert result["success"] is True
    assert target.read_bytes() == b"glTFrest"


def test_a_half_written_download_is_never_left_in_place(monkeypatch, tmp_path):
    """A truncated file with the right name is one Unity will try to
    import. It is written to a .part and moved at the end."""
    def explode(*a, **k):
        raise client.requests.RequestException("connection reset")

    monkeypatch.setattr(client.requests, "get", explode)
    target = tmp_path / "m.glb"

    result = client.download("https://cdn/x.glb", str(target))

    assert result["success"] is False
    assert not target.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_an_empty_body_is_not_an_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[]))

    result = client.download("https://cdn/x.glb", str(tmp_path / "m.glb"))

    assert result["success"] is False
    assert "empty" in result["error"]


def test_an_enormous_download_is_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "MAX_DOWNLOAD_BYTES", 32)
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"x" * 64]))

    result = client.download("https://cdn/x.glb", str(tmp_path / "m.glb"))

    assert result["success"] is False
    assert not (tmp_path / "m.glb").exists()


@pytest.mark.parametrize("suffix,head,valid", [
    (".glb", b"glTF\x02\x00\x00\x00", True),
    (".glb", b"<!DOCTYPE html><html>", False),
    (".png", b"\x89PNG\r\n\x1a\n", True),
    (".png", b"not a png at all!!!!", False),
    (".mp3", b"ID3\x03\x00\x00\x00\x00\x00", True),
    (".wav", b"RIFF\x00\x00\x00\x00WAVE", True),
])
def test_an_asset_is_validated_by_its_bytes(tmp_path, suffix, head, valid):
    """A download that failed often lands as an error page with the
    right name. That passes "is it there?" and fails an import much
    later, somewhere less obvious."""
    target = tmp_path / f"a{suffix}"
    target.write_bytes(head + b"\x00" * 64)

    assert actions.validate_asset(str(target))["valid"] is valid


def test_an_empty_file_is_not_valid(tmp_path):
    target = tmp_path / "a.glb"
    target.write_bytes(b"")

    assert actions.validate_asset(str(target))["valid"] is False


def test_a_missing_file_is_not_valid(tmp_path):
    assert actions.validate_asset(str(tmp_path / "ghost.glb"))["valid"] is False


def test_an_extension_nothing_knows_is_not_called_invalid(tmp_path):
    """Saying "this is wrong" about a format nobody checked would be a
    claim with nothing behind it."""
    target = tmp_path / "a.weird"
    target.write_bytes(b"whatever")

    answer = actions.validate_asset(str(target))
    assert answer["valid"] is True
    assert answer["checked"] is False


def test_a_download_never_replaces_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"\x89PNG\r\n\x1a\n"]))
    (tmp_path / "barrel.png").write_bytes(b"mine")

    result = actions.download_asset("https://cdn/x.png", name="barrel")

    assert Path(result["path"]).name != "barrel.png"
    assert (tmp_path / "barrel.png").read_bytes() == b"mine"


def test_the_extension_comes_from_the_url_when_it_says(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"glTF"]))

    result = actions.download_asset("https://cdn/a/thing.glb?sig=abc",
                                    kind="model", name="barrel")

    assert result["path"].endswith(".glb")


def test_saving_an_asset_keeps_what_is_already_there(tmp_path):
    source = tmp_path / "in.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n")
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "in.png").write_bytes(b"mine")

    result = actions.save_asset(str(source), folder=str(folder))

    assert Path(result["path"]).name != "in.png"
    assert (folder / "in.png").read_bytes() == b"mine"


def test_the_output_folder_is_the_shared_one(monkeypatch, tmp_path):
    """Same resolver as Blender: env, then the plugin's own setting,
    then Documents/ARIA/Ludo. Not aria_output beside the source tree."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path / "chosen"))

    assert actions.output_dir() == tmp_path / "chosen"


# ======================================================
# Natural language
# ======================================================

@pytest.mark.parametrize("sentence,action", [
    ("Make a car in Ludo", "generate_image"),
    ("Generate a character in Ludo", "generate_image"),
    ("Create a forest in Ludo", "generate_image"),
    ("Make a sprite sheet in Ludo", "generate_sprite_sheet"),
    ("Generate audio in Ludo", "generate_audio"),
])
def test_the_five_named_sentences_map(sentence, action):
    """The first three are images, not 3D models, and that is the
    deliberate reading -- see test_an_ambiguous_request_takes_the_
    cheaper_road."""
    result = mapping.map_text(sentence)

    assert result is not None, sentence
    assert result["action"] == action


def test_a_sentence_no_vocabulary_covers_still_works():
    """THE FAILURE THIS RULE EXISTS FOR. Measured in chat:

        Create a stylized cartoon girl with bright red hair, a large
        pink bow, a pink dress, big expressive eyes, and a confident
        heroic pose in Ludo

    map_text returned None, because "girl" was not in the character
    word list and nothing else matched either. The best a
    short-circuit could have said was "I do not know how to make
    that" -- about a plain image request, to an API whose entire
    purpose is turning a sentence into a picture.
    """
    result = mapping.map_text(
        "Create a stylized cartoon girl with bright red hair, a large pink "
        "bow, a pink dress, big expressive eyes, and a confident heroic pose "
        "in Ludo")

    assert result is not None
    assert result["action"] == "generate_image"
    assert "red hair" in result["params"]["prompt"]
    assert result["needs"] == []


@pytest.mark.parametrize("sentence", [
    "in Ludo make a xylophone-playing octopus",
    "make a haunted lighthouse in Ludo",
    "Ludo, create a bioluminescent jellyfish queen",
    "in Ludo generate a rusty vending machine",
])
def test_no_sentence_that_asks_for_something_is_refused(sentence):
    """A vocabulary that can fail is the wrong shape for this API.
    Blender needs one because a noun it has never heard of cannot be
    built out of cubes; Ludo has no such limit."""
    result = mapping.map_text(sentence)

    assert result is not None, sentence
    assert result["params"].get("prompt")


def test_an_ambiguous_request_takes_the_cheaper_road():
    """An image is one generation; the 3D pipeline is two, because
    Ludo has no text-to-3D. Guessing "model" from an ambiguous
    sentence would spend twice what guessing "image" does."""
    plain = mapping.map_text("make a car in Ludo")

    assert plain["action"] == "generate_image"
    assert "one generation" in plain["summary"]


@pytest.mark.parametrize("sentence,action", [
    ("make a 3D car in Ludo", "generate_vehicle"),
    ("make a 3d character in Ludo", "generate_character"),
    ("in Ludo make a game-ready model of a barrel", "generate_model"),
    ("make a mesh of a sword in Ludo", "generate_model"),
])
def test_the_expensive_road_has_to_be_asked_for(sentence, action):
    result = mapping.map_text(sentence)

    assert result["action"] == action
    assert "two generations" in result["summary"]


@pytest.mark.parametrize("sentence,kind", [
    ("in Ludo make a seamless stone texture", "tile"),
    ("generate a forest background in Ludo", "fixed_background"),
    ("in Ludo make an icon for a health potion", "icon"),
    ("make a portrait of a knight in Ludo", "portrait"),
    ("in Ludo make a logo for my studio", "logo"),
    ("Create a cartoon girl in Ludo", "art"),
])
def test_the_kind_of_image_is_inferred(sentence, kind):
    """image_type is not decoration: Ludo composes a sprite
    differently from a background and a background differently from an
    icon."""
    assert mapping.map_text(sentence)["params"]["image_type"] == kind


def test_every_inferred_image_type_is_one_the_api_accepts():
    """An invented type costs a credit to be refused."""
    for _pattern, kind in mapping._IMAGE_TYPE_HINTS:
        assert kind in client.IMAGE_TYPES, kind
    assert "art" in client.IMAGE_TYPES


def test_a_generic_word_does_not_own_a_style():
    """Measured: "a stylized cartoon girl" chose Stylized 3D over
    Western Cartoon, because the longest phrase wins and "stylized" is
    longer than "cartoon" -- so a 2D drawing request picked a 3D
    style."""
    result = mapping.map_text("Create a stylized cartoon girl in Ludo")

    assert result["params"]["style"] == "Western Cartoon"


@pytest.mark.parametrize("sentence", [
    "Make a car",
    "Generate a character",
    "Create a forest",
    "Make a sprite sheet",
    "Generate audio",
    "make me a spaceship",
])
def test_without_naming_ludo_nothing_happens(sentence):
    """The gate matters more here than for Blender: a sentence read
    too eagerly is not an inconvenience, it is a charge."""
    assert mapping.map_text(sentence) is None


@pytest.mark.parametrize("sentence", [
    "make a car in Blender",
    "make a car in Ludo and Blender",
    "make a car in Blender or Ludo",
])
def test_naming_blender_is_not_naming_ludo(sentence):
    """Two tools that both make 3D assets must not both answer one
    sentence, and an ambiguous one is a reason to ask."""
    assert mapping.map_text(sentence) is None


@pytest.mark.parametrize("sentence", [
    "how do I make a car in Ludo?",
    "in Ludo, what does a credit cost?",
    "can you tell me how to use Ludo",
    "what is Ludo.ai?",
    "explain Ludo's sprite sheets",
    "how much does a generation cost in Ludo",
    "the car in Ludo is red",
    "hello",
    "",
])
def test_a_question_never_spends_anything(sentence):
    assert mapping.map_text(sentence) is None


@pytest.mark.parametrize("sentence", [
    "can you make a car in Ludo?",
    "could you generate a character in Ludo",
    "please make a forest in Ludo",
])
def test_a_polite_request_is_still_a_request(sentence):
    assert mapping.map_text(sentence) is not None


@pytest.mark.parametrize("sentence,opening", [
    ("Ludo, make a car", "vocative"),
    ("Ludo: make a car", "vocative colon"),
    ("use Ludo to make a car", "verb"),
    ("make a car with Ludo", "with"),
    ("make a car using Ludo.ai", "the .ai suffix"),
    ("MAKE A CAR IN LUDO", "shouting"),
])
def test_every_way_of_naming_ludo_works(sentence, opening):
    assert mapping.map_text(sentence) is not None, opening


def test_the_kind_word_does_not_become_the_prompt():
    """Measured before the format/subject split existed: "make a
    sprite sheet in Ludo" produced prompt="sprite sheet", which asks
    Ludo for a picture of the words."""
    named = mapping.map_text("Make a sprite sheet of a walking knight in Ludo")

    assert named["params"]["prompt"] == "walking knight"


def test_an_underspecified_request_asks_instead_of_guessing():
    """"Make a sprite sheet in Ludo" -- of WHAT? Answered by asking.
    Guessing costs money and handing it to a model gets an offer to
    help."""
    vague = mapping.map_text("Make a sprite sheet in Ludo")

    assert vague["needs"] == ["prompt"]
    assert "prompt" not in vague["params"]


def test_a_subject_word_stays_in_the_prompt():
    """"car" names the thing, not the format, and must not be
    stripped the way "sprite sheet" is."""
    assert mapping.map_text("Make a car in Ludo")["params"]["prompt"] == "car"


def test_a_style_becomes_one_the_api_accepts():
    """"low poly" is a real art_style; "lowpoly" is not, and would be
    refused after a credit had been spent finding out."""
    result = mapping.map_text("make a low poly red sports car in Ludo")

    assert result["params"]["style"] == "Low Poly"
    assert result["params"]["style"] in client.ART_STYLES
    assert "low poly" not in result["params"]["prompt"].lower()


def test_every_style_word_maps_to_a_real_enum_value():
    """A style the API has never heard of is a credit spent on a 400."""
    for word, value in mapping.STYLE_WORDS.items():
        assert value in client.ART_STYLES, word


def test_every_mapped_action_exists():
    """The strongest thing these two modules owe each other."""
    for _pattern, action, _build, _role in mapping.ACTIONS:
        assert callable(getattr(actions, action, None)), action


def test_every_mapped_parameter_is_one_the_action_takes():
    """A mapping that emits a keyword the function does not accept is
    a TypeError at the worst possible moment."""
    import inspect

    for sentence in ("make a low poly car in Ludo",
                     "make a sprite sheet of a knight with 16 frames in Ludo",
                     "generate music for a boss fight in Ludo",
                     "in Ludo make a texture of cracked stone",
                     "create a forest in Ludo",
                     "make an animation of a waterfall in Ludo",
                     "generate audio of a sword clash in Ludo",
                     "in Ludo generate a voice saying 'hello there'"):
        plan = mapping.map_text(sentence)
        if not plan:
            continue
        signature = inspect.signature(getattr(actions, plan["action"]))
        for name in plan["params"]:
            assert name in signature.parameters, (sentence, plan["action"], name)


def test_the_plan_says_what_it_will_cost():
    """Two-call actions charge twice. A person should see that before
    it happens, not on their invoice."""
    two = mapping.map_text("make a 3D car in Ludo")
    one = mapping.map_text("in Ludo make a texture of stone")

    assert "two generations" in two["summary"]
    assert "one generation" in one["summary"]


def test_the_gate_and_the_mapper_agree():
    for sentence in ("make a car in Ludo", "make a car", "hello",
                     "make a car in Blender"):
        if mapping.map_text(sentence) is not None:
            assert mapping.names_ludo(sentence)
            assert not mapping.names_another_tool(sentence)


def test_asking_for_the_impossible_is_told_apart_from_asking_a_question():
    """"Make me a spaceship in Ludo" must not reach a model, which
    would describe one and sound like it had spent the credits."""
    assert mapping.wants_something_built("make me a spaceship in Ludo") is True
    assert mapping.wants_something_built("in Ludo, what does a credit cost?") is False
    assert mapping.wants_something_built("make me a spaceship") is False


# ======================================================
# Into Unity
# ======================================================

@pytest.fixture
def unity_project(tmp_path, monkeypatch):
    root = tmp_path / "Project"
    (root / "Assets").mkdir(parents=True)
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(
        unity_cli_engine, "_plugin_setting",
        lambda field: str(root) if field == unity_cli_engine.FIELD_PROJECT else "")
    return root


def test_a_generation_that_failed_is_not_a_download_failure(unity_project):
    """Two different sentences, and a person needs the right one."""
    result = pipeline.download_and_deliver(
        {"success": False, "ran": True, "error": "insufficient credits",
         "url": None, "kind": "model"})

    assert result["success"] is False
    assert "insufficient credits" in result["error"]
    assert "download" not in result["steps"]


def test_a_result_with_no_url_says_so(unity_project):
    result = pipeline.download_and_deliver(
        {"success": True, "url": None, "kind": "model"})

    assert result["success"] is False
    assert "did not say where" in result["error"]


def test_a_bad_download_stops_before_unity(unity_project, monkeypatch,
                                           tmp_path):
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(404))
    called = []
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: called.append(a) or {})

    result = pipeline.download_and_deliver(
        {"success": True, "url": "https://cdn/x.glb", "kind": "model"})

    assert result["success"] is False
    assert called == []


def test_an_error_page_with_the_right_name_never_reaches_unity(
        unity_project, monkeypatch, tmp_path):
    """The specific way a download fails that a file-exists check
    cannot catch."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, chunks=[b"<!DOCTYPE html><html>oops</html>"]))
    called = []
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: called.append(a) or {})

    result = pipeline.download_and_deliver(
        {"success": True, "url": "https://cdn/x.glb", "kind": "model"})

    assert result["success"] is False
    assert "not a usable asset" in result["summary"]
    assert called == []


def test_a_downloaded_asset_is_kept_even_when_it_is_wrong(
        unity_project, monkeypatch, tmp_path):
    """Deleting somebody's file to tidy up an error message is not
    this function's decision -- they paid for it."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"not a model"]))

    result = pipeline.download_and_deliver(
        {"success": True, "url": "https://cdn/x.glb", "kind": "model"})

    assert Path(result["steps"]["download"]["path"]).is_file()


def test_a_good_asset_goes_all_the_way(unity_project, monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"glTF" + b"\x00" * 64]))
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: {"success": True, "output": "",
                                         "json": {"success": True,
                                                  "data": {"globalId": "abc"}},
                                         "error": None})

    result = pipeline.download_and_deliver(
        {"success": True, "url": "https://cdn/x.glb", "kind": "model"},
        name="barrel", place=True)

    assert result["success"] is True
    assert result["asset_path"].startswith("Assets/ARIA/Ludo/")


def test_unity_being_closed_does_not_lose_the_asset(unity_project, monkeypatch,
                                                    tmp_path):
    """It was paid for. "Downloaded but Unity was closed" is true and
    useful; one success flag over four steps is not."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(200, chunks=[b"glTF" + b"\x00" * 64]))
    from backend.unity import unity_cli_engine
    monkeypatch.setattr(unity_cli_engine, "run_invocation",
                        lambda *a, **k: {"success": False, "output": "",
                                         "json": None,
                                         "error": "No Pipeline instance found"})

    result = pipeline.download_and_deliver(
        {"success": True, "url": "https://cdn/x.glb", "kind": "model"},
        name="barrel")

    assert result["steps"]["download"]["success"] is True
    assert Path(result["steps"]["download"]["path"]).is_file()


def test_the_unity_half_is_not_a_second_copy():
    """The Pipeline argument names were read out of the package's own
    C#. That evidence is worth exactly one copy, and two would drift."""
    from backend.blender import blender_asset_pipeline
    from backend.unity import unity_delivery

    assert pipeline.trigger_unity_import is unity_delivery.trigger_unity_import
    assert blender_asset_pipeline.trigger_unity_import is unity_delivery.trigger_unity_import


def test_ludo_assets_get_their_own_folder():
    """So a person can see which tool made what, and delete either
    without taking the other with it."""
    assert pipeline.DEFAULT_FOLDER != "Assets/ARIA"
    assert "Ludo" in pipeline.DEFAULT_FOLDER


# ======================================================
# The turn
# ======================================================

def _turn(text):
    from backend.core.turn_types import SessionState, TurnRequest
    return TurnRequest(messages=[{"role": "user", "content": text}],
                       latest_user_text=text, conversation_id="test",
                       session=SessionState())


THE_SENTENCE = ("Create a stylized cartoon girl with bright red hair, a large "
                "pink bow, a pink dress, big expressive eyes, and a confident "
                "heroic pose in Ludo")


def test_the_sentence_from_the_log_generates_something(posts, monkeypatch,
                                                       tmp_path):
    """Measured: this reached phi-3-mini, which replied "As an AI, I
    can't directly create images, but I can guide you through the
    process" and described the character in prose. Nothing was
    generated."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, chunks=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 64]))

    answer = actions.answer_request(THE_SENTENCE)

    assert answer is not None
    assert answer["ran"] is True
    assert "Made it with Ludo.ai" in answer["text"]
    assert Path(answer["paths"][0]).is_file()


def test_the_turn_is_answered_without_a_model(posts, monkeypatch, tmp_path):
    from backend.core import turn_orchestrator

    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, chunks=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 64]))

    result = turn_orchestrator._ludo_reply(_turn(THE_SENTENCE), [])

    assert result is not None
    assert result.model_id == turn_orchestrator.LUDO_MODEL


@pytest.mark.parametrize("sentence", [
    "make a car",
    "make a car in Blender",
    "in Ludo, what does a credit cost?",
    "what is the weather in Paris?",
    "write me a haiku",
    "",
])
def test_ordinary_messages_are_untouched(posts, sentence):
    from backend.core import turn_orchestrator

    assert turn_orchestrator._ludo_reply(_turn(sentence), []) is None
    assert posts == []


def test_a_broken_layer_still_says_nothing_happened(posts, monkeypatch):
    """An exception must not hand the turn back to a model."""
    from backend.core import turn_orchestrator

    monkeypatch.setattr(actions, "answer_request",
                        lambda _t: (_ for _ in ()).throw(RuntimeError("boom")))

    result = turn_orchestrator._ludo_reply(_turn(THE_SENTENCE), [])

    assert result is not None
    assert "nothing happened" in result.text


def test_an_underspecified_request_asks_and_spends_nothing(posts):
    answer = actions.answer_request("Make a sprite sheet in Ludo")

    assert answer["ran"] is False
    assert "of what" in answer["text"].lower()
    assert posts == []


def test_a_refusal_never_claims_to_have_made_something(monkeypatch, tmp_path,
                                                       posts):
    monkeypatch.delenv(client.ENV_KEY, raising=False)
    from backend.plugins import plugin_settings
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "none.json"))

    answer = actions.answer_request(THE_SENTENCE)

    assert answer["ran"] is False
    assert "nothing was spent" in answer["text"]
    assert "Made it" not in answer["text"]
    assert posts == []


def test_a_paid_for_asset_that_will_not_download_keeps_its_url(posts,
                                                               monkeypatch,
                                                               tmp_path):
    """It was generated and charged for. Losing the URL would mean
    paying for something nobody can reach."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(404))

    answer = actions.answer_request(THE_SENTENCE)

    assert answer["ran"] is True
    assert "https://cdn.ludo.ai" in answer["text"]


def test_the_short_circuit_runs_before_any_model_is_chosen():
    import inspect
    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)

    assert source.index("_ludo_reply") < source.index("INTENT_WORKSPACE_QUERY")


def test_naming_blender_hands_the_turn_to_the_other_layer(posts):
    """Two short-circuits that both make assets must not both answer,
    and this one is the one that costs money."""
    from backend.core import turn_orchestrator

    assert turn_orchestrator._ludo_reply(
        _turn("make a car in Blender"), []) is None
    assert posts == []


def test_an_image_result_is_an_array_and_the_others_are_not():
    """MEASURED, AND IT COST A CREDIT. A real generation succeeded and
    asset_url returned None, so a paid-for image was thrown away with
    "there is no URL to download".

    Three endpoints, three shapes, and I had read only one:

        POST /assets/image        -> ARRAY of ImageResult (n can be >1)
        POST /assets/3d-model     -> Model3DResult        (one object)
        POST /audio/sound-effect  -> AudioResult          (one object)
    """
    assert client.asset_url([{"url": "https://cdn/a.png"}]) == "https://cdn/a.png"
    assert client.asset_url({"model_url": "https://cdn/m.glb"}) == "https://cdn/m.glb"
    assert client.asset_url({"url": "https://cdn/s.mp3"}) == "https://cdn/s.mp3"
    assert client.asset_url([]) is None


def test_every_image_in_a_batch_is_kept():
    """`n` asks for more than one picture and each is paid for.
    Returning only the first would quietly discard the rest."""
    batch = [{"url": "https://cdn/a.png"}, {"url": "https://cdn/b.png"}]

    assert client.asset_urls(batch) == ["https://cdn/a.png", "https://cdn/b.png"]


def test_the_image_endpoint_is_exercised_as_an_array(monkeypatch, tmp_path):
    """The fixture above returns a single object for /assets/image,
    which is NOT what the API does -- so this one uses the real shape,
    or the bug that cost a credit could come back unnoticed."""
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: FakeResponse(
                            200, [{"url": "https://cdn.ludo.ai/a/img.png",
                                   "request_id": "r", "created_at": 0}]))

    result = actions.generate_image("a barrel")

    assert result["success"] is True
    assert result["url"] == "https://cdn.ludo.ai/a/img.png"


def test_a_model_conversion_reads_the_array_too(monkeypatch):
    """generate_model feeds the image URL into the 3D call. If the
    array is not unwrapped, the second call is sent image=None."""
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json or {})
        if "/assets/image" in url:
            return FakeResponse(200, [{"url": "https://cdn.ludo.ai/a/img.png"}])
        return FakeResponse(200, {"model_url": "https://cdn.ludo.ai/a/m.glb"})

    monkeypatch.setattr(client.requests, "post", fake_post)

    result = actions.generate_model("a barrel")

    assert result["success"] is True
    assert sent[1]["image"] == "https://cdn.ludo.ai/a/img.png"


def test_a_webp_is_recognised(tmp_path):
    """Measured: a real Ludo image generation came back as .webp,
    which nothing validated -- so it passed as an unknown extension.
    True, but weaker than it needed to be."""
    good = tmp_path / "a.webp"
    good.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 32)
    bad = tmp_path / "b.webp"
    bad.write_bytes(b"<!DOCTYPE html>" + b"\x00" * 32)

    assert actions.validate_asset(str(good))["valid"] is True
    assert actions.validate_asset(str(good))["checked"] is True
    assert actions.validate_asset(str(bad))["valid"] is False


def test_the_reply_does_not_lower_case_the_summary():
    """str.capitalize() lower-cases the remainder, which turned
    "Ludo.ai ... Western Cartoon style" into "ludo.ai ... western
    cartoon style" -- a product name and an API enum, both wrong."""
    said = actions._sentence_case(
        "ask Ludo.ai for an image in the Western Cartoon style")

    assert said.startswith("Ask Ludo.ai")
    assert "Western Cartoon" in said


# ======================================================
# A generation must not freeze the application
#
# THE FAILURE THIS EXISTS FOR
# Measured. A second request went out at 04:14:15 and sixty seconds
# later there was no stream_start -- and, the real tell, no
# heartbeat_ack either. The WebSocket handler reads packets with
#
#     async for raw in self.websocket:
#         await self._dispatch(packet)
#
# so nothing else is read while a turn runs. The first generation had
# already stalled every ack for 25 seconds; a queued job polled for
# the old JOB_TOTAL_SECONDS of 900 would have frozen the whole UI for
# fifteen minutes with no output at all.
# ======================================================

def test_a_chat_turn_does_not_wait_a_quarter_of_an_hour():
    assert client.JOB_TOTAL_SECONDS <= 180
    # A script with its own patience may still wait longer.
    assert client.JOB_MAX_SECONDS > client.JOB_TOTAL_SECONDS


def test_a_slow_job_gives_back_its_id_rather_than_hanging(monkeypatch, posts):
    """The work was charged for when it was queued. Losing the id
    would mean paying for an asset nobody can ever reach."""
    monkeypatch.setattr(client.requests, "post",
                        lambda *a, **k: FakeResponse(
                            202, {"id": "job-99", "status": "queued",
                                  "task_type": "image", "created_at": 0}))
    monkeypatch.setattr(client, "JOB_TOTAL_SECONDS", 2)
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, {"id": "job-99", "status": "running",
                                  "task_type": "image", "created_at": 0}))

    answer = actions.answer_request(
        "make a car in Ludo", on_status=lambda _v: None)

    assert answer["ran"] is True
    assert answer["job_id"] == "job-99"
    assert "job-99" in answer["text"]
    assert "collect" in answer["text"]


def test_still_running_is_not_reported_as_a_failure():
    """It is not a failure -- the work is happening. Saying "it did
    not work out" about something being paid for right now is the
    wrong sentence."""
    assert issubclass(client.LudoStillRunning, client.LudoError)
    error = client.LudoStillRunning("job-7", 120)
    assert error.job_id == "job-7"
    assert "Nothing is lost" in str(error)


def test_progress_is_reported_while_it_runs(posts, monkeypatch, tmp_path):
    """The UI has no other sign of life: the receive loop is parked
    and heartbeat acks stop, so a silent thirty seconds is
    indistinguishable from a crash."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, chunks=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 64]))
    seen = []

    actions.answer_request("make a car in Ludo", on_status=seen.append)

    assert "generating" in seen
    assert "downloading" in seen


def test_a_status_callback_that_raises_never_loses_the_turn(posts, monkeypatch,
                                                            tmp_path):
    """Progress reporting is decoration. A turn must not be lost to it."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, chunks=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 64]))

    def explode(_value):
        raise RuntimeError("the UI fell over")

    answer = actions.answer_request("make a car in Ludo", on_status=explode)

    assert answer["ran"] is True


# ======================================================
# Collecting what outran the turn
# ======================================================

@pytest.mark.parametrize("sentence,job", [
    ("collect 7f3a9c21b4e in Ludo", "7f3a9c21b4e"),
    ("in Ludo collect job 7f3a9c21b4e", "7f3a9c21b4e"),
    ("fetch 7f3a9c21b4e from Ludo", "7f3a9c21b4e"),
])
def test_a_collect_request_is_recognised(sentence, job):
    assert mapping.collect_request(sentence) == job


@pytest.mark.parametrize("sentence", [
    "make a car in Ludo",
    "collect the mail",
    "in Ludo, get me a coffee",
    "collect 7f3a9c21b4e in Blender",
])
def test_only_a_real_collect_request_collects(sentence):
    assert mapping.collect_request(sentence) is None


def test_collecting_costs_nothing(monkeypatch, tmp_path, posts):
    """It was charged for when it was queued, which is what makes a
    timeout an inconvenience rather than a loss."""
    monkeypatch.setenv("ARIA_LUDO_OUTPUT", str(tmp_path))
    monkeypatch.setattr(client.requests, "get",
                        lambda url, **k: (
                            FakeResponse(200, {"id": "job-99",
                                               "status": "succeeded",
                                               "task_type": "image",
                                               "created_at": 0,
                                               "result": [{"url": "https://cdn/a.png"}]})
                            if "/assets/jobs/" in url
                            else FakeResponse(200, chunks=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 64])))

    answer = actions.answer_request("collect abc123def456 in Ludo")

    assert posts == [], "collecting must not POST anything"
    assert "Collected job" in answer["text"]
    assert Path(answer["paths"][0]).is_file()


def test_collecting_something_still_running_says_so(monkeypatch, posts):
    monkeypatch.setattr(client, "JOB_TOTAL_SECONDS", 2)
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, {"id": "job-99xyz", "status": "running",
                                  "task_type": "image", "created_at": 0}))

    answer = actions.answer_request("collect abc123def456 in Ludo")

    assert answer["ran"] is False
    assert "Still going" in answer["text"]
    assert posts == []


def test_collecting_is_checked_before_generating(posts):
    """"collect abc123 in Ludo" has no make-verb, so map_text returns
    None -- and the turn would have gone to a model that cannot fetch
    anything. Worse, a looser reading could have GENERATED instead,
    which costs money to answer a request that was already paid for."""
    import inspect

    source = inspect.getsource(actions.answer_request)

    # Compared at the CALL SITES, not anywhere the names appear: the
    # comment above the collect branch mentions map_text, which made
    # an index comparison on the bare names read backwards.
    assert (source.index("collect_request(said)")
            < source.index("mapping.map_text(said)"))


def test_a_jobs_result_is_unwrapped_whatever_shape_it_is():
    """A job's `result` is whatever its endpoint would have returned
    directly -- an image job holds a LIST, a 3D job holds an object.
    Unwrapping only the dict meant a collected image job came back as
    the whole job record and asset_url found nothing in it: "there is
    no asset in it to fetch", about a generation that had succeeded
    and been paid for."""
    image_job = {"id": "j", "status": "succeeded",
                 "result": [{"url": "https://cdn/a.png"}]}
    model_job = {"id": "j", "status": "succeeded",
                 "result": {"model_url": "https://cdn/m.glb"}}

    assert client.asset_url(client._result_of(image_job)) == "https://cdn/a.png"
    assert client.asset_url(client._result_of(model_job)) == "https://cdn/m.glb"


def test_the_deadline_can_be_changed_at_run_time(monkeypatch):
    """Read at the call, not captured as a default argument. A default
    binds at import, so the deadline could not be changed by a setting
    or by a test -- which is how a two-second test spent two minutes."""
    monkeypatch.setattr(client, "JOB_TOTAL_SECONDS", 1)
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: FakeResponse(
                            200, {"id": "j", "status": "running",
                                  "task_type": "image", "created_at": 0}))

    started = __import__("time").monotonic()
    with pytest.raises(client.LudoStillRunning):
        client.poll_job("j")

    assert __import__("time").monotonic() - started < 20


def test_polling_never_becomes_a_hot_loop(monkeypatch):
    """Against the real API each GET blocks server-side, so the loop
    paces itself. Against anything that answers immediately there was
    no sleep at all, and this pinned a core for the whole deadline."""
    monkeypatch.setattr(client, "JOB_TOTAL_SECONDS", 2)
    asks = []
    monkeypatch.setattr(client.requests, "get",
                        lambda *a, **k: asks.append(1) or FakeResponse(
                            200, {"id": "j", "status": "running",
                                  "task_type": "image", "created_at": 0}))

    with pytest.raises(client.LudoStillRunning):
        client.poll_job("j")

    # Two seconds at a half-second floor is a handful of asks, not
    # thousands.
    assert len(asks) < 20, f"{len(asks)} requests in two seconds"
    assert client.MIN_POLL_SECONDS > 0
