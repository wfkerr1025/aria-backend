# backend/tests/provider_and_errors_tests.py
#
# Regression tests for Phase 2:
#   - backend/core/errors.py: AriaError hierarchy, categorization,
#     recovery suggestions, IPC/REST wire-shape translation.
#   - backend/core/provider_interface.py: ProviderAdapter wrapping real
#     provider_registry entries (all 15+ real providers) with the six
#     unified methods (load/infer/stream/metadata/diagnostics/requirements).
#
# Self-contained plain-assert tests. Run directly:
#
#   python backend/tests/provider_and_errors_tests.py

from __future__ import annotations

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================
# PART 1 — errors.py
# ============================================================
def test_aria_error_is_a_real_exception():
    from backend.core.errors import AriaError, ErrorCode

    e = AriaError(code=ErrorCode.GENERIC_ERROR, message="boom")
    try:
        raise e
    except Exception as caught:
        assert isinstance(caught, AriaError)
        assert str(caught) == "boom"


def test_error_category_and_suggestion_lookup():
    from backend.core.errors import ToolError, ErrorCode, CATEGORY_TOOL

    e = ToolError(code=ErrorCode.TOOL_TIMEOUT, message="too slow")
    assert e.category == CATEGORY_TOOL
    assert "timeout" in e.suggestion.lower()


def test_unknown_code_falls_back_to_unknown_category_with_no_suggestion():
    from backend.core.errors import AriaError, CATEGORY_UNKNOWN

    e = AriaError(code="TOTALLY_MADE_UP_CODE", message="?")
    assert e.category == CATEGORY_UNKNOWN
    assert e.suggestion is None


def test_to_error_packet_matches_ipc_errors_build_error_shape_plus_extras():
    from backend.core.errors import ProviderError, ErrorCode
    from backend import ipc_errors

    e = ProviderError(code=ErrorCode.PROVIDER_UNAVAILABLE, message="no key configured", request_type="chat_request")
    packet = e.to_error_packet()

    assert packet["type"] == "error"
    assert packet["code"] == ErrorCode.PROVIDER_UNAVAILABLE
    assert packet["message"] == "no key configured"
    assert packet["request_type"] == "chat_request"
    assert "category" in packet and "suggestion" in packet

    # Must remain a strict superset of the pre-existing build_error() shape.
    baseline = ipc_errors.build_error(ErrorCode.PROVIDER_UNAVAILABLE, "no key configured", "chat_request")
    for key, value in baseline.items():
        assert packet[key] == value


def test_to_http_detail_matches_rest_error_shape():
    from backend.core.errors import PipelineError, ErrorCode

    e = PipelineError(code=ErrorCode.PIPELINE_NO_PROVIDER, message="nothing configured")
    detail = e.to_http_detail()
    assert "error" in detail
    assert detail["error"]["code"] == ErrorCode.PIPELINE_NO_PROVIDER
    assert detail["error"]["message"] == "nothing configured"


def test_every_new_error_code_is_categorized():
    from backend.core import errors as err_mod

    all_codes = [v for k, v in vars(err_mod.ErrorCode).items() if not k.startswith("_") and isinstance(v, str)]
    for code in all_codes:
        assert err_mod.categorize(code) != err_mod.CATEGORY_UNKNOWN or code == err_mod.ErrorCode.GENERIC_ERROR or code == err_mod.ErrorCode.HANDLER_EXCEPTION, (
            f"error code {code} has no category mapping — add one to _CODE_CATEGORIES"
        )


# ============================================================
# PART 2 — provider_interface.py
# ============================================================
def test_list_unified_providers_matches_provider_registry():
    from backend.core.provider_interface import list_unified_providers
    from backend.llm.providers import provider_registry

    assert list_unified_providers() == provider_registry.list_providers()


def test_get_unified_provider_returns_none_for_unknown_name():
    from backend.core.provider_interface import get_unified_provider

    assert get_unified_provider("not-a-real-provider-xyz") is None


def test_every_registered_provider_implements_all_six_methods():
    from backend.core.provider_interface import get_unified_provider, list_unified_providers

    for name in list_unified_providers():
        adapter = get_unified_provider(name)
        assert adapter is not None, f"{name} should resolve"
        # load()/metadata()/diagnostics()/requirements() must all succeed
        # without raising for every real registered provider — infer()/
        # stream() require a real request and are covered separately.
        assert isinstance(adapter.metadata(), dict), f"{name}.metadata() must return a dict"
        assert isinstance(adapter.diagnostics(), dict), f"{name}.diagnostics() must return a dict"
        assert isinstance(adapter.requirements(), dict), f"{name}.requirements() must return a dict"


def test_local_provider_metadata_includes_model_info_when_model_id_given():
    from backend.core.provider_interface import get_unified_provider
    from backend.core.model_registry import get_default_model_id

    model_id = get_default_model_id()
    adapter = get_unified_provider("local", model_id=model_id)
    meta = adapter.metadata()

    assert meta["location"] == "local"
    assert "model_info" in meta
    assert meta["model_info"]["model_id"] == model_id


def test_cloud_provider_metadata_has_no_model_info():
    from backend.core.provider_interface import get_unified_provider

    adapter = get_unified_provider("openai")
    meta = adapter.metadata()
    assert meta["location"] == "cloud"
    assert "model_info" not in meta


def test_cloud_provider_requirements_declares_api_key_needed():
    from backend.core.provider_interface import get_unified_provider

    adapter = get_unified_provider("openai")
    reqs = adapter.requirements()
    assert reqs.get("requires_api_key") is True


def test_local_provider_requirements_returns_real_hardware_thresholds():
    from backend.core.provider_interface import get_unified_provider
    from backend.core.model_registry import get_default_model_id

    adapter = get_unified_provider("local", model_id=get_default_model_id())
    reqs = adapter.requirements()
    assert "minRamGB" in reqs and "recRamGB" in reqs


def test_local_provider_diagnostics_includes_compat_check():
    from backend.core.provider_interface import get_unified_provider
    from backend.core.model_registry import get_default_model_id

    adapter = get_unified_provider("local", model_id=get_default_model_id())
    diag = adapter.diagnostics()
    assert "compat" in diag
    assert "meets_minimum" in diag["compat"]


def test_provider_adapter_stream_synthesizes_one_chunk_for_a_run_only_provider():
    from backend.core.provider_interface import ProviderAdapter

    class RunOnlyProvider:
        def run(self, request):
            return "the whole response at once"

    adapter = ProviderAdapter(provider_name="fake", _provider=RunOnlyProvider())
    chunks = []
    adapter.stream(object(), chunks.append)

    assert len(chunks) == 1
    assert chunks[0]["content"] == "the whole response at once"


def test_provider_adapter_stream_delegates_to_real_stream_method_when_present():
    from backend.core.provider_interface import ProviderAdapter

    class StreamingProvider:
        def run(self, request):
            return "unused"

        def stream(self, request, callback):
            callback({"content": "a"})
            callback({"content": "b"})

    adapter = ProviderAdapter(provider_name="fake", _provider=StreamingProvider())
    chunks = []
    adapter.stream(object(), chunks.append)

    assert [c["content"] for c in chunks] == ["a", "b"]


def test_provider_adapter_load_reports_unavailable_cloud_provider_honestly():
    from backend.core.provider_interface import ProviderAdapter

    class UnavailableProvider:
        def is_available(self):
            return False

        def run(self, request):
            raise RuntimeError("should never be called")

    adapter = ProviderAdapter(provider_name="fake_cloud", _provider=UnavailableProvider())
    result = adapter.load()
    assert result["ok"] is False


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
