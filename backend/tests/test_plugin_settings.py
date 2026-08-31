# backend/tests/test_plugin_settings.py
#
# The plugins a user installs, and their settings.
#
# TWO THINGS CALLED "PLUGIN"
# --------------------------
# This repo already had a plugin system when this was written:
# plugins/plugin_manager.py loads plugin CODE from manifest.json, ships
# blender/unity/unreal/wordpress, and answers plugins_list_request. Its
# own docstring records that it was the fourth loader found in the repo
# and that three others already existed.
#
# So this module was deliberately NOT called plugin_manager and its
# packets were deliberately NOT called plugins_list_*. It manages
# SETTINGS -- which integrations a user has turned on, and the path or
# key each one needs. The two are asserted to coexist below, because the
# way this goes wrong is one silently replacing the other.

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend import ipc_router, ipc_schema as schema
from backend.plugins import plugin_settings


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A registry of this session's own, never the user's."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps({
        "unity": {"id": "unity", "name": "Unity Integration", "version": "1.0.0",
                  "enabled": True, "logo": "assets/plugin_logos/unity.png",
                  "configPage": "unity-config", "unity_path": "", "project_path": ""},
        "blender": {"id": "blender", "name": "Blender Integration", "version": "1.0.0",
                    "enabled": False, "logo": "assets/plugin_logos/blender.png",
                    "configPage": "blender-config", "blender_path": ""},
        "ludo": {"id": "ludo", "name": "Ludo.ai Integration", "version": "1.0.0",
                 "enabled": True, "logo": "assets/plugin_logos/ludo.png",
                 "configPage": "ludo-config", "api_key": "", "model": ""},
    }, indent=2), encoding="utf-8")

    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    return path


# ======================================================
# The shipped registry
# ======================================================

# aria_config/plugins.json is BOTH shipped and live.
#
# It is tracked in git and it is what discovery writes to, so on a
# machine that has run ARIA it holds whatever was found there --
# unity_cli, unreal, wordpress -- alongside the three that ship. These
# tests therefore ask what git ships, not what is on disk. An earlier
# version read the working copy and started failing the moment the user
# opened the Plugins page, which is the feature working correctly.
def _shipped_registry() -> dict:
    """The registry as committed, not as the local ARIA left it."""
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    committed = subprocess.run(
        ["git", "show", "HEAD:aria_config/plugins.json"],
        cwd=str(root), capture_output=True, text=True)

    if committed.returncode == 0 and committed.stdout.strip():
        return json.loads(committed.stdout)

    # No git, or a fresh checkout mid-rebase: fall back to the file and
    # ignore anything discovery added, which is what this is really
    # trying to look past.
    on_disk = json.loads((root / "aria_config" / "plugins.json").read_text("utf-8"))
    return {key: value for key, value in on_disk.items()
            if not value.get("discovered") and not value.get("type")}


def test_the_shipped_registry_holds_the_three_core_plugins():
    """Self-Improvement Engine and Diagnostics Enhancer were hard-coded
    into the old page's markup. Nothing is hard-coded now, so this is
    the file that decides."""
    shipped = _shipped_registry()

    assert sorted(shipped) == ["blender", "ludo", "unity"]
    for plugin_id, plugin in shipped.items():
        assert plugin["id"] == plugin_id
        assert plugin["configPage"] == f"{plugin_id}-config"
        assert plugin["logo"].startswith("assets/plugin_logos/")


def test_every_shipped_logo_is_a_real_png():
    """A card with a broken image is a card that looks broken."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    shipped = _shipped_registry()

    for plugin in shipped.values():
        # The logo path is relative to webui/, which is the web root.
        image = root / "webui" / plugin["logo"]
        assert image.is_file(), f"{image} is missing"
        assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_removed_plugins_are_nowhere():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    shipped = (root / "aria_config" / "plugins.json").read_text(encoding="utf-8").lower()

    assert "self-improvement" not in shipped
    assert "diagnostics enhancer" not in shipped


# ======================================================
# Reading
# ======================================================

def test_a_missing_registry_reads_as_empty_rather_than_failing(tmp_path, monkeypatch):
    """The Plugins page should say it is empty, not fail to open."""
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(tmp_path / "nope.json"))

    assert plugin_settings.load_plugins() == {}
    assert plugin_settings.list_plugins() == []


def test_a_corrupt_registry_reads_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "plugins.json"
    path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))

    assert plugin_settings.load_plugins() == {}


def test_plugins_are_listed_by_name(registry):
    names = [plugin["name"] for plugin in plugin_settings.list_plugins()]

    assert names == sorted(names)


def test_an_unknown_plugin_is_named_in_the_refusal(registry):
    with pytest.raises(plugin_settings.PluginError) as raised:
        plugin_settings.get_plugin("unreal")

    assert "unreal" in str(raised.value)


# ======================================================
# Secrets
# ======================================================

def test_a_key_never_leaves_this_module(registry):
    """list_plugins feeds a page. The page shows whether a key is set."""
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})

    listed = {plugin["id"]: plugin for plugin in plugin_settings.list_plugins()}
    assert listed["ludo"]["api_key"] == "configured"

    for plugin in plugin_settings.list_plugins():
        assert "sk-a-real-looking-key" not in json.dumps(plugin)


def test_an_unset_key_reads_as_unset_rather_than_configured(registry):
    listed = {plugin["id"]: plugin for plugin in plugin_settings.list_plugins()}

    assert listed["ludo"]["api_key"] == ""


def test_the_key_is_still_on_disk_for_the_code_that_needs_it(registry):
    """Redaction is for the page, not for storage."""
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})

    assert plugin_settings.get_plugin("ludo")["api_key"] == "sk-a-real-looking-key"


# ======================================================
# Validation
# ======================================================

def test_an_empty_path_is_not_configured_rather_than_wrong(registry):
    """The state a fresh install is in is not an error."""
    assert plugin_settings.validate_plugin("unity", {"unity_path": ""}) == []


def test_a_path_that_is_not_there_is_refused(registry):
    problems = plugin_settings.validate_plugin(
        "unity", {"unity_path": "C:/nowhere/Unity.exe"})

    assert problems and "nothing exists" in problems[0]


def test_a_relative_path_is_refused(registry):
    problems = plugin_settings.validate_plugin("unity", {"unity_path": "Unity.exe"})

    assert problems and "full path" in problems[0]


def test_a_real_executable_is_accepted(registry, tmp_path):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")

    assert plugin_settings.validate_plugin("unity", {"unity_path": str(editor)}) == []


def test_a_folder_where_a_program_belongs_is_refused(registry, tmp_path):
    problems = plugin_settings.validate_plugin("unity", {"unity_path": str(tmp_path)})

    assert problems and "not a folder" in problems[0]


def test_a_field_the_plugin_does_not_have_is_refused(registry):
    problems = plugin_settings.validate_plugin("unity", {"blender_path": "/x"})

    assert problems and "no field" in problems[0]


def test_every_problem_is_reported_at_once(registry):
    """So a form shows them all rather than one save at a time."""
    problems = plugin_settings.validate_plugin(
        "unity", {"unity_path": "relative", "project_path": "also-relative"})

    assert len(problems) == 2


def test_nothing_is_written_when_anything_is_wrong(registry):
    before = registry.read_text(encoding="utf-8")

    with pytest.raises(plugin_settings.PluginError):
        plugin_settings.update_plugin("unity", {"unity_path": "relative"})

    assert registry.read_text(encoding="utf-8") == before


# ======================================================
# Changing
# ======================================================

def test_enable_and_disable_round_trip(registry):
    plugin_settings.disable_plugin("unity")
    assert plugin_settings.get_plugin("unity")["enabled"] is False

    plugin_settings.enable_plugin("unity")
    assert plugin_settings.get_plugin("unity")["enabled"] is True


def test_disabling_keeps_the_settings(registry, tmp_path):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")
    plugin_settings.update_plugin("unity", {"unity_path": str(editor)})

    plugin_settings.disable_plugin("unity")

    assert plugin_settings.get_plugin("unity")["unity_path"] == str(editor)


def test_identity_is_not_editable(registry):
    """A form that could rewrite an id could rename a plugin into
    another one's slot."""
    plugin_settings.update_plugin("unity", {"id": "ludo", "configPage": "elsewhere"})

    plugin = plugin_settings.get_plugin("unity")
    assert plugin["id"] == "unity"
    assert plugin["configPage"] == "unity-config"


def test_removing_takes_the_settings_with_it(registry):
    assert plugin_settings.remove_plugin("unity") is True
    assert "unity" not in plugin_settings.load_plugins()


def test_removing_twice_is_not_an_error(registry):
    plugin_settings.remove_plugin("unity")

    assert plugin_settings.remove_plugin("unity") is False


def test_a_save_leaves_valid_json_even_though_it_is_atomic(registry):
    plugin_settings.disable_plugin("unity")

    reloaded = json.loads(registry.read_text(encoding="utf-8"))
    assert reloaded["unity"]["enabled"] is False
    # And no temporary file left beside it.
    assert list(registry.parent.glob(".plugins-*")) == []


# ======================================================
# What the rest of ARIA reads
# ======================================================

def test_a_configured_path_reaches_unity_ops(registry, tmp_path, monkeypatch):
    """This is why the Unity page is a page and not a form that saves a
    string nobody reads."""
    from backend.core import unity_ops

    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")
    plugin_settings.update_plugin("unity", {"unity_path": str(editor)})
    monkeypatch.delenv(unity_ops.ENV_EDITOR, raising=False)

    assert unity_ops.editor_path() == editor


def test_a_disabled_plugins_path_is_not_used(registry, tmp_path):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")
    plugin_settings.update_plugin("unity", {"unity_path": str(editor)})
    plugin_settings.disable_plugin("unity")

    assert plugin_settings.configured_path("unity", "unity_path") == ""


# ======================================================
# Connection tests
# ======================================================

def test_testing_unity_finds_a_real_executable(registry, tmp_path):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")
    plugin_settings.update_plugin("unity", {"unity_path": str(editor)})

    outcome = plugin_settings.test_plugin_connection("unity")

    assert outcome["ok"] is True
    assert "Unity.exe" in outcome["message"]


def test_testing_unity_asks_unity_ops_rather_than_the_field(registry, monkeypatch):
    """The test has to agree with the thing that runs Unity.

    unity_ops looks in four places -- the environment, this plugin's
    setting, Unity Hub, PATH -- so a test that read the field alone
    would report "not configured" on a machine where Unity runs fine.
    """
    from backend.core import unity_ops

    seen = []

    def found():
        seen.append(True)
        return Path("C:/Unity/Editor/Unity.exe")

    monkeypatch.setattr(unity_ops, "editor_path", found)

    outcome = plugin_settings.test_plugin_connection("unity")

    assert seen, "the test must ask unity_ops, not read the field"
    assert outcome["ok"] is True
    assert "Unity.exe" in outcome["message"]


def test_testing_unity_reports_what_unity_ops_could_not_find(registry, monkeypatch):
    from backend.core import unity_ops

    def missing():
        raise unity_ops.UnityUnavailable("No Unity editor was found.")

    monkeypatch.setattr(unity_ops, "editor_path", missing)

    outcome = plugin_settings.test_plugin_connection("unity")

    assert outcome["ok"] is False
    assert "No Unity editor was found." in outcome["message"]


def test_testing_blender_wants_a_real_file(registry, tmp_path):
    outcome = plugin_settings.test_plugin_connection("blender")
    assert outcome["ok"] is False
    assert "No Blender path" in outcome["message"]

    # Saving a path that is not there is refused before it can be saved.
    with pytest.raises(plugin_settings.PluginError):
        plugin_settings.update_plugin(
            "blender", {"blender_path": str(tmp_path / "not-here.exe")})

    real = tmp_path / "blender.exe"
    real.write_text("binary")
    real.chmod(0o755)
    plugin_settings.update_plugin("blender", {"blender_path": str(real)})

    outcome = plugin_settings.test_plugin_connection("blender")
    assert outcome["ok"] is True
    assert "blender.exe" in outcome["message"]

    # Which leaves one way for the path to go bad: the file moves after
    # it was saved. That is exactly what this test button is for, and it
    # is the case validation cannot catch.
    real.unlink()

    outcome = plugin_settings.test_plugin_connection("blender")
    assert outcome["ok"] is False
    assert "Nothing runnable" in outcome["message"]


# ------------------------------------------------------
# The Ludo.ai test is the only thing in this module that
# leaves the machine. These tests do not: they replace the
# HTTP client, because a unit test that needs the internet
# fails on a train and tells you nothing when it does.
# The live one is opt-in, at the bottom.
# ------------------------------------------------------

def _answer(monkeypatch, reply):
    """Stand in for tools.http_fetch, recording what it was asked."""
    calls = []

    def fake(url, *, headers=None, json_body=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        return reply

    import tools.http_fetch as client
    monkeypatch.setattr(client, "http_fetch", fake)
    return calls


def test_testing_ludo_sends_the_key_to_ludo(registry, monkeypatch):
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    calls = _answer(monkeypatch, {"status": "ok", "code": 200, "data": {}})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is True
    assert len(calls) == 1
    assert calls[0]["url"].startswith(plugin_settings.LUDO_API_BASE)
    # Authorization: ApiKey, not Authentication and not Bearer.
    #
    # Both halves were wrong in the first version of this integration,
    # and neither was caught by a test, because the test asserted
    # whatever the code happened to send. Measured against the live
    # endpoint: with the header named "Authentication" the server
    # answers "API Key is required" -- it never sees the key at all.
    assert calls[0]["headers"] == {"Authorization": "ApiKey sk-a-real-looking-key"}
    assert "Authentication" not in calls[0]["headers"]
    # It runs while somebody watches the page, so it must give up quickly.
    assert calls[0]["timeout"] == plugin_settings.LUDO_TIMEOUT_SECONDS


def test_testing_ludo_without_a_key_never_leaves_the_machine(registry, monkeypatch):
    calls = _answer(monkeypatch, {"status": "ok", "code": 200})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is False
    assert not calls, "there was nothing to authenticate with"


def test_a_rejected_key_is_reported_as_a_rejected_key(registry, monkeypatch):
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    _answer(monkeypatch, {"status": "error", "code": 401, "error": "no"})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is False
    assert "rejected that key" in outcome["message"]


def test_an_unknown_route_does_not_blame_the_key(registry, monkeypatch):
    """A moved API is not a bad key.

    If ARIA reports a 404 as a bad key, a user deletes a working key on
    ARIA's advice -- which is worse than saying nothing at all.
    """
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    _answer(monkeypatch, {"status": "error", "code": 404, "error": "Cannot GET"})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is False
    assert "not checked" in outcome["message"]
    assert "rejected" not in outcome["message"].lower()
    assert plugin_settings.ENV_LUDO_PATH in outcome["message"]


def test_the_probe_is_the_endpoint_that_exists_for_it(registry, monkeypatch):
    """/auth/validate-api-key, verified against Ludo.ai's OpenAPI spec.

    It is also the only endpoint that answers this question without
    spending credits -- Ludo.ai meters API calls, so a test that
    generated an image to prove the key worked would bill the user for
    pressing a button.
    """
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    calls = _answer(monkeypatch, {"status": "ok", "code": 200})

    plugin_settings.test_plugin_connection("ludo")

    assert calls[0]["url"] == "https://api.ludo.ai/api/auth/validate-api-key"


def test_a_missing_key_is_not_reported_as_a_rejected_key(registry, monkeypatch):
    """The two 403s mean different things and point at different people.

    "Unauthorized" is the user's key. "API Key is required" means the
    request arrived without one, which can only be ARIA's fault, and
    blaming the key there sends somebody to regenerate a good one.
    """
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    _answer(monkeypatch, {"status": "error", "code": 403,
                          "error": "HTTP 403: {\"message\":\"API Key is required\"}"})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is False
    assert "ARIA's fault" in outcome["message"]
    assert "rejected" not in outcome["message"].lower()


def test_the_offered_models_are_real_ones(registry):
    """These were three invented names before Ludo.ai's spec was read.
    Every one of them would have been refused by every endpoint."""
    offered = set(plugin_settings.FIELD_CHOICES["ludo"]["model"])

    assert {"blitz", "standard", "eagle", "forge", "tango"} <= offered
    assert not any(name.startswith("ludo-") for name in offered)


def test_no_network_is_reported_as_no_network(registry, monkeypatch):
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    _answer(monkeypatch, {"status": "error", "code": None, "error": "dns"})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is False
    assert "Could not reach" in outcome["message"]


def test_the_key_never_appears_in_a_test_result(registry, monkeypatch):
    key = "sk-this-must-not-be-echoed"
    plugin_settings.update_plugin("ludo", {"api_key": key})

    for reply in ({"status": "ok", "code": 200},
                  {"status": "error", "code": 401, "error": key},
                  {"status": "error", "code": None, "error": "boom"}):
        _answer(monkeypatch, reply)
        outcome = plugin_settings.test_plugin_connection("ludo")
        assert key not in str(outcome)


def test_the_endpoint_can_be_pointed_somewhere_else(registry, monkeypatch):
    """Because the route is unverified, it has to be changeable without
    editing code."""
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})
    monkeypatch.setenv(plugin_settings.ENV_LUDO_BASE, "https://example.test/api")
    monkeypatch.setenv(plugin_settings.ENV_LUDO_PATH, "ping")
    calls = _answer(monkeypatch, {"status": "ok", "code": 200})

    plugin_settings.test_plugin_connection("ludo")

    assert calls[0]["url"] == "https://example.test/api/ping"


@pytest.mark.skipif(not os.environ.get("ARIA_LIVE_LUDO_KEY"),
                    reason="set ARIA_LIVE_LUDO_KEY to test against the real API")
def test_the_real_ludo_api_live(registry):
    """Opt-in. Never part of an ordinary run.

    This is how the guessed route gets confirmed or corrected: give it a
    real key and read what comes back.
    """
    plugin_settings.update_plugin(
        "ludo", {"api_key": os.environ["ARIA_LIVE_LUDO_KEY"]})

    outcome = plugin_settings.test_plugin_connection("ludo")

    print("live Ludo.ai result:", outcome["message"])
    assert isinstance(outcome["ok"], bool)


# ======================================================
# The IPC surface
# ======================================================

def test_the_page_can_list_configure_test_and_remove(registry, tmp_path):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")

    listed = ipc_router.dispatch(
        {"type": schema.PLUGIN_REGISTRY_LIST_REQUEST, "payload": {}})
    assert [p["id"] for p in listed["payload"]["plugins"]] == ["blender", "ludo", "unity"]

    fetched = ipc_router.dispatch(
        {"type": schema.PLUGIN_GET_REQUEST, "payload": {"id": "unity"}})
    assert fetched["payload"]["plugin"]["name"] == "Unity Integration"

    saved = ipc_router.dispatch({
        "type": schema.PLUGIN_UPDATE_REQUEST,
        "payload": {"id": "unity", "fields": {"unity_path": str(editor)}}})
    assert saved["payload"]["saved"] is True

    tested = ipc_router.dispatch(
        {"type": schema.PLUGIN_TEST_REQUEST, "payload": {"id": "unity"}})
    assert tested["payload"]["ok"] is True

    removed = ipc_router.dispatch(
        {"type": schema.PLUGIN_REMOVE_REQUEST, "payload": {"id": "unity"}})
    assert removed["payload"]["removed"] is True


def test_a_validation_failure_comes_back_as_a_message_for_the_form(registry):
    answer = ipc_router.dispatch({
        "type": schema.PLUGIN_UPDATE_REQUEST,
        "payload": {"id": "unity", "fields": {"unity_path": "relative"}}})

    assert answer["type"] == "error"
    assert "full path" in answer["message"]


def test_the_ipc_answer_never_carries_a_key(registry):
    ipc_router.dispatch({
        "type": schema.PLUGIN_UPDATE_REQUEST,
        "payload": {"id": "ludo", "fields": {"api_key": "sk-a-real-looking-key"}}})

    for request in (schema.PLUGIN_REGISTRY_LIST_REQUEST, ):
        answer = ipc_router.dispatch({"type": request, "payload": {}})
        assert "sk-a-real-looking-key" not in json.dumps(answer)

    fetched = ipc_router.dispatch(
        {"type": schema.PLUGIN_GET_REQUEST, "payload": {"id": "ludo"}})
    assert "sk-a-real-looking-key" not in json.dumps(fetched)


# ======================================================
# The system that was already here
# ======================================================

def test_the_existing_plugin_loader_still_answers(registry):
    """plugins_list_request belongs to plugins/plugin_manager.py, which
    loads plugin CODE and ships blender/unity/unreal/wordpress.

    This registry took a different packet name on purpose. The way this
    goes wrong is one handler silently replacing the other, and a
    duplicate key in the handler map does exactly that without a word.
    """
    answer = ipc_router.dispatch({"type": schema.PLUGINS_LIST_REQUEST, "payload": {}})

    assert answer["type"] == schema.PLUGINS_LIST_RESULT
    assert "plugins" in answer["payload"]


def test_the_two_systems_have_different_packet_names():
    assert schema.PLUGINS_LIST_REQUEST != schema.PLUGIN_REGISTRY_LIST_REQUEST
    assert schema.PLUGINS_LIST_RESULT != schema.PLUGIN_REGISTRY_LIST_RESULT


def test_no_handler_is_registered_twice():
    """A duplicate key in a dict literal is not an error in Python; the
    later one wins and nothing says so. That is what happened while this
    was being written."""
    import ast
    import pathlib

    source = pathlib.Path(ipc_router.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [ast.unparse(key) for key in node.keys if key is not None]
        duplicates = {key for key in keys if keys.count(key) > 1}
        assert not duplicates, f"registered twice in ipc_router: {sorted(duplicates)}"
