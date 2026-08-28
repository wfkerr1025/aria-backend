import subprocess
import os

TESTS = [
]

# Pytest-style suites (test_*.py). These cannot go in TESTS above: running
# one with `py <path>` would import the module, define its test functions,
# and exit 0 without executing a single assertion -- a silent false pass.
# They are invoked through pytest instead, using the same sequential
# run-and-check-returncode pattern as the loop above.
PYTEST_TESTS = [
    "test_aria_memory_phase3.py",
    "test_phase3_tools.py",
    "test_semantic_embeddings.py",
    "test_memory_ranking.py",
    "test_memory_prompting.py",
    "test_semantic_routing.py",
    "test_file_ingestion.py",
    "test_file_search.py",
    "test_file_prompting.py",
    "test_file_summary_wording.py",
    "test_file_chunk_display.py",
    "test_file_sections.py",
    "test_query_deframing.py",
    "test_keyword_deframing.py",
    "test_keyword_scoring.py",
    "test_query_expansion.py",
    "test_domain_expansion.py",
    "test_phase6_synthesis.py",
    "test_phase6_conflicts.py",
    "test_phase6_templates.py",
    "test_phase7_topic_segmentation.py",
    "test_phase7_goal_tracking.py",
    "test_phase7_evidence_selection.py",
    "test_phase7_long_context_synthesis.py",
    "test_phase7_resumption.py",
    "test_phase7_resumption_topic_match.py",
    "test_phase8_planning.py",
    "test_phase9_tools.py",
    "test_phase8_search_weather.py",
    "test_turn_orchestrator.py",
    "test_rest_turn_wiring.py",
    "test_orchestrator_only_routing.py",
    "test_answer_stream.py",
    "test_chat_status_and_scroll.py",
    "test_synthesis_evidence.py",
    "test_evidence_routing.py",
    "test_evidence_aware_resolution.py",
    "test_evidence_bundling.py",
    "test_tool_result_structuring.py",
    "test_structured_evidence_flow.py",
    "test_planner_search_activation.py",
    "test_websearch_multiprovider.py",
    "test_routing_financial_queries.py",
    "test_news_providers.py",
    "test_langsearch_provider.py",
    "test_provider_key_lookup.py",
    "test_search_timeout_budget.py",
    "test_general_fallback_search.py",
    "test_evidence_floor_downgrade.py",
    "test_local_preferred_for_evidence.py",
    "test_tool_orchestrator.py",
    "test_provider_stop_sequences.py",
    "test_upgrade_01_note_embeddings.py",
    "test_upgrade_02_semantic_index.py",
    "test_upgrade_03_chunk_metadata.py",
    "test_upgrade_04_semantic_index_fk.py",
    "test_upgrade_05_context_expiration.py",
    "test_upgrade_06_self_versioning.py",
    "test_upgrade_07_list_notes_tool.py",
    "test_upgrade_08_delete_note_tool.py",
    "test_upgrade_09_list_context_tool.py",
    "test_upgrade_10_list_self_tool.py",
]

# The *_tests.py suites.
#
# These were run by nothing. pytest's default collection is test_*.py and
# *_test.py -- neither of which matches *_tests.py -- and this runner only
# listed the test_*.py files, so thirty-three suites and roughly five
# hundred assertions sat in the repository being collected by no one.
#
# That gap hid real regressions. The Cloud-Mode-with-no-provider gate was
# dropped from the WebSocket path and the suite that covers it never ran;
# so was a conversational model switch answering "Switched to None."
# Naming them here is what makes the rest of this directory mean anything.
LEGACY_TESTS = [
    "absolute_mode_separation_tests.py",
    "auto_balancer_tests.py",
    "auto_mode_removal_tests.py",
    "backend_watchdog_tests.py",
    "cache_and_hardware_snapshot_tests.py",
    "diagnostics_and_rest_expansion_tests.py",
    "hardware_and_performance_tests.py",
    "ipc_schema_and_heartbeat_tests.py",
    "key_persistence_tests.py",
    "logging_server_tests.py",
    "metrics_docs_and_rest_v2_tests.py",
    "mode_truthfulness_tests.py",
    "model_info_and_init_pipeline_tests.py",
    "model_routing_tests.py",
    "model_switching_tests.py",
    "pc_capability_tier_tests.py",
    "plugin_and_cache_tests.py",
    "provider_and_errors_tests.py",
    "rest_chat_and_stream_tests.py",
    "rest_health_and_models_tests.py",
    "routing_and_keys_tests.py",
    "routing_invariants_tests.py",
    "safety_projection_tests.py",
    "sandbox_and_tool_registry_tests.py",
    "self_query_truthfulness_tests.py",
    "skr_and_ipc_tests.py",
    "streaming_v2_and_pipeline_tests.py",
    "warning_system_tests.py",
    "weather_and_tools_truth_tests.py",
]

# Suites that make live network calls. Kept out of the default run so a
# failure here means the code broke rather than that DuckDuckGo was slow.
# Run them deliberately:
#     py -m pytest backend/tests/web_search_tests.py
#
# They also each define a helper literally named test(fn), which pytest
# collects as a test and then errors on for a missing "fn" fixture -- a
# pre-existing artifact of these files predating pytest, not a failure.
NETWORK_TESTS = [
    "open_meteo_geocode_tests.py",
    "weather_fusion_tests.py",
    "web_search_tests.py",
]

for test in TESTS:
    path = os.path.join(os.path.dirname(__file__), test)
    print(f"Running {test}...")
    result = subprocess.run(["py", path])
    if result.returncode != 0:
        print(f"{test} FAILED")
        exit(result.returncode)

for test in PYTEST_TESTS + LEGACY_TESTS:
    path = os.path.join(os.path.dirname(__file__), test)
    print(f"Running {test}...")
    result = subprocess.run(["py", "-m", "pytest", path, "-q"])
    if result.returncode != 0:
        print(f"{test} FAILED")
        exit(result.returncode)

print("All tests passed.")
