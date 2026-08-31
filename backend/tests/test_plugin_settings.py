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
        "ludo": {"id": "ludo", "name": "Ludo.ai Integration", "version": "1.0.0",
                 "enabled": True, "logo": "assets/plugin_logos/ludo.png",
                 "configPage": "ludo-config", "api_key": "", "model": ""},
    }, indent=2), encoding="utf-8")

    monkeypatch.setenv(plugin_settings.ENV_PLUGINS_FILE, str(path))
    return path


# ======================================================
# The shipped registry
# ======================================================

def test_the_shipped_registry_holds_exactly_the_three_plugins():
    """Self-Improvement Engine and Diagnostics Enhancer were hard-coded
    into the old page's markup. Nothing is hard-coded now, so this is
    the file that decides."""
    import pathlib

    shipped = json.loads(
        (pathlib.Path(__file__).resolve().parents[2] / "aria_config" / "plugins.json")
        .read_text(encoding="utf-8"))

    assert sorted(shipped) == ["blender", "ludo", "unity"]
    for plugin_id, plugin in shipped.items():
        assert plugin["id"] == plugin_id
        assert plugin["configPage"] == f"{plugin_id}-config"
        assert plugin["logo"].startswith("assets/plugin_logos/")


def test_every_shipped_logo_is_a_real_png():
    """A card with a broken image is a card that looks broken."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    shipped = json.loads((root / "aria_config" / "plugins.json").read_text(encoding="utf-8"))

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


def test_testing_unity_with_nothing_set_says_so(registry):
    outcome = plugin_settings.test_plugin_connection("unity")

    assert outcome["ok"] is False
    assert "unity path" in outcome["message"].lower()


def test_testing_ludo_does_not_call_ludo(registry):
    """A test that spends the user's quota every time they open the page
    is a test that gets switched off."""
    plugin_settings.update_plugin("ludo", {"api_key": "sk-a-real-looking-key"})

    outcome = plugin_settings.test_plugin_connection("ludo")

    assert outcome["ok"] is True
    assert "has not called" in outcome["message"]


# ======================================================
# The IPC surface
# ======================================================

def test_the_page_can_list_configure_test_and_remove(registry, tmp_path):
    editor = tmp_path / "Unity.exe"
    editor.write_text("x", encoding="utf-8")

    listed = ipc_router.dispatch(
        {"type": schema.PLUGIN_REGISTRY_LIST_REQUEST, "payload": {}})
    assert [p["id"] for p in listed["payload"]["plugins"]] == ["ludo", "unity"]

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
