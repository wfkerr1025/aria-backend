# backend/tests/plugin_and_cache_tests.py
#
# Regression tests for Phase 2:
#   - plugins/base_plugin.py's new get_tools()/get_models()/
#     get_diagnostics() capability hooks (safe defaults; existing
#     shipped plugins never override them).
#   - backend/core/plugin_registry.py: hot-loading at import-time via
#     plugins/plugin_manager.py (the richest of the three
#     pre-existing, non-interoperating plugin loaders found during
#     research — see that module's docstring), tool registration from
#     a plugin, sandboxed diagnostics/model aggregation, reload.
#   - backend/core/multi_tier_cache.py: memory tier, disk/persistent
#     tier, read-through promotion, TTL expiry, warm-up verification.
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/plugin_and_cache_tests.py

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# PART 1 — plugins/base_plugin.py capability hooks
# ============================================================
def test_base_plugin_capability_hooks_have_safe_empty_defaults():
    from plugins.base_plugin import BasePlugin

    plugin = BasePlugin(manifest={"name": "Test"}, plugin_path="/fake/path")
    assert plugin.get_tools() == []
    assert plugin.get_models() == []
    assert plugin.get_diagnostics() == {}


def test_real_shipped_plugins_still_use_the_safe_defaults():
    """
    None of the four real shipped plugins (blender/unity/unreal/
    wordpress) override the new hooks — confirming the additive change
    to BasePlugin didn't require touching any of them.
    """
    from backend.core import plugin_registry as pr

    pr.reload_all()
    for name in ("Blender", "Unity", "Unreal", "WordPress"):
        plugin = pr._get_manager().loaded_plugins.get(name)
        assert plugin is not None, f"expected {name} to load"
        assert plugin.get_tools() == []
        assert plugin.get_models() == []


# ============================================================
# PART 2 — backend/core/plugin_registry.py
# ============================================================
def test_load_all_discovers_the_real_shipped_plugins():
    from backend.core import plugin_registry as pr

    result = pr.reload_all()
    assert set(result["loaded"]) == {"Blender", "Unity", "Unreal", "WordPress"}
    assert result["failed"] == {}


def test_list_plugins_reports_identity_fields():
    from backend.core import plugin_registry as pr

    pr.reload_all()
    plugins = pr.list_plugins()
    unity = next(p for p in plugins if p["name"] == "Unity")
    assert unity["version"] == "0.1.0"
    assert unity["author"] == "William"


def test_a_plugin_declaring_a_tool_gets_it_registered_and_reachable():
    """
    Simulates a plugin that overrides get_tools() (none of the real
    shipped ones do) by monkeypatching one loaded instance's method —
    proves the wiring from BasePlugin.get_tools() through to a live,
    executable tool_registry entry actually works end to end.
    """
    from backend.core import plugin_registry as pr
    from backend.core.tool_registry import ToolSchema, PERMISSION_SAFE, execute_tool, list_tools

    pr.reload_all()
    manager = pr._get_manager()
    unity = manager.loaded_plugins["Unity"]

    fake_schema = ToolSchema(name="_plugin_test_tool", description="from a plugin", permission=PERMISSION_SAFE)
    unity.get_tools = lambda: [(fake_schema, lambda: "plugin tool ran")]

    result = pr._register_plugin_tools("Unity", unity)
    try:
        assert result == ["_plugin_test_tool"]
        assert "_plugin_test_tool" in {t["name"] for t in list_tools()}

        exec_result = execute_tool("_plugin_test_tool", {}, allowed_permissions={PERMISSION_SAFE})
        assert exec_result.ok is True
        assert exec_result.value == "plugin tool ran"
    finally:
        pr.unload_plugin_tools("Unity")


def test_unload_plugin_tools_removes_them_from_the_registry():
    from backend.core import plugin_registry as pr
    from backend.core.tool_registry import ToolSchema, PERMISSION_SAFE, list_tools

    pr.reload_all()
    manager = pr._get_manager()
    unity = manager.loaded_plugins["Unity"]
    fake_schema = ToolSchema(name="_plugin_test_tool_2", description="x", permission=PERMISSION_SAFE)
    unity.get_tools = lambda: [(fake_schema, lambda: None)]

    pr._register_plugin_tools("Unity", unity)
    assert "_plugin_test_tool_2" in {t["name"] for t in list_tools()}

    removed = pr.unload_plugin_tools("Unity")
    assert removed == 1
    assert "_plugin_test_tool_2" not in {t["name"] for t in list_tools()}


def test_get_plugin_diagnostics_is_sandboxed_against_a_hanging_plugin():
    from backend.core import plugin_registry as pr

    pr.reload_all()
    manager = pr._get_manager()
    unity = manager.loaded_plugins["Unity"]
    unity.get_diagnostics = lambda: (time.sleep(30), {})[1]  # would hang without a real timeout

    start = time.perf_counter()
    diagnostics = pr.get_plugin_diagnostics()
    elapsed = time.perf_counter() - start

    assert elapsed < pr._PLUGIN_HOOK_TIMEOUT_SECONDS + 2, "a hanging plugin hook must not block the whole aggregation"
    assert "error" in diagnostics["Unity"]


def test_run_plugin_command_executes_a_real_registered_command():
    from backend.core import plugin_registry as pr

    pr.reload_all()
    manager = pr._get_manager()
    unity = manager.loaded_plugins["Unity"]
    commands = unity.get_commands()
    assert commands, "expected Unity to expose at least one command"

    command_id = next(iter(commands))
    result = pr.run_plugin_command("Unity", command_id)
    assert result is not None or result is None  # just must not raise


def test_run_plugin_command_raises_plugin_error_for_unknown_plugin():
    from backend.core import plugin_registry as pr
    from backend.core.errors import PluginError

    try:
        pr.run_plugin_command("NotARealPlugin", "whatever")
        assert False, "expected a PluginError"
    except PluginError as e:
        assert e.code == "PLUGIN_NOT_FOUND"


# ============================================================
# PART 3 — backend/core/multi_tier_cache.py
# ============================================================
def _isolated_tiers_dir():
    import backend.core.multi_tier_cache as mtc
    tmp = tempfile.TemporaryDirectory()
    original = mtc.TIERS_DIR
    mtc.TIERS_DIR = Path(tmp.name)
    mtc._instances.clear()

    def restore():
        mtc.TIERS_DIR = original
        mtc._instances.clear()
        tmp.cleanup()

    return restore


def test_memory_tier_hit_does_not_touch_disk():
    from backend.core.multi_tier_cache import MultiTierCache

    restore = _isolated_tiers_dir()
    try:
        cache = MultiTierCache("test_ns")
        cache.set("k", "v", persist=False)  # memory only
        assert cache.get("k") == "v"
        assert not cache._disk_path.exists(), "persist=False must never touch disk"
    finally:
        restore()


def test_disk_tier_is_read_through_and_promotes_to_memory():
    from backend.core.multi_tier_cache import MultiTierCache

    restore = _isolated_tiers_dir()
    try:
        cache = MultiTierCache("test_ns")
        cache.set("k", {"nested": 123})
        cache._memory.clear()  # force a disk read

        assert cache.get("k") == {"nested": 123}
        assert "k" in cache._memory, "a disk hit must be promoted back into memory"
    finally:
        restore()


def test_persistent_tier_survives_a_new_instance():
    from backend.core.multi_tier_cache import MultiTierCache

    restore = _isolated_tiers_dir()
    try:
        MultiTierCache("test_ns").set("k", "persisted")
        fresh = MultiTierCache("test_ns")  # simulates a new process/restart
        assert fresh.get("k") == "persisted"
    finally:
        restore()


def test_ttl_expiry_works_in_memory_and_on_disk():
    from backend.core.multi_tier_cache import MultiTierCache

    restore = _isolated_tiers_dir()
    try:
        cache = MultiTierCache("test_ns")
        cache.set("k", "v", ttl_seconds=0.05)
        time.sleep(0.15)
        assert cache.get("k", "expired") == "expired"

        cache._memory.clear()  # also check the disk-tier expiry path
        assert cache.get("k", "expired") == "expired"
    finally:
        restore()


def test_delete_removes_from_both_tiers():
    from backend.core.multi_tier_cache import MultiTierCache

    restore = _isolated_tiers_dir()
    try:
        cache = MultiTierCache("test_ns")
        cache.set("k", "v")
        cache.delete("k")
        assert cache.get("k", "gone") == "gone"

        fresh = MultiTierCache("test_ns")
        assert fresh.get("k", "gone") == "gone"
    finally:
        restore()


def test_get_cache_returns_the_same_instance_for_a_namespace():
    from backend.core.multi_tier_cache import get_cache

    restore = _isolated_tiers_dir()
    try:
        a = get_cache("shared_ns")
        b = get_cache("shared_ns")
        assert a is b
    finally:
        restore()


def test_warm_up_verify_reports_entry_counts_and_flags_corrupt_entries():
    from backend.core.multi_tier_cache import MultiTierCache
    import json

    restore = _isolated_tiers_dir()
    try:
        cache = MultiTierCache("test_ns")
        cache.set("good", "value")

        # Manually corrupt one entry on disk (missing "value" key).
        with open(cache._disk_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["entries"]["bad"] = {"not_value": 1}
        with open(cache._disk_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        summary = cache.warm_up_verify()
        assert summary["disk_entry_count"] == 2
        assert summary["integrity_ok"] is False
        assert "bad" in summary["corrupt_keys"]
        assert "good" not in summary["corrupt_keys"]
    finally:
        restore()


# ============================================================
# RUNNER
# ============================================================
def _all_tests():
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failures = []
    for test in _all_tests():
        name = test.__name__
        try:
            test()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
