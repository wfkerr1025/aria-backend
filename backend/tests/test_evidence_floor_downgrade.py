# backend/tests/test_evidence_floor_downgrade.py
#
# An install with no model trusted to read evidence asks for less.
#
# Two separate declarations of "trusted with evidence" have to agree, and
# until now one of them could not be consulted:
#
#     evidence_routing.EVIDENCE_MODEL_ALLOWLIST  -- which models may be
#         asked to synthesize from supplied evidence
#     complexity_router.MEDIUM_MODEL_ID          -- the floor an
#         evidence-bearing turn is never stepped below
#
# In Automatic mode model_id is None at routing time, because
# ProviderRouter has not chosen yet. model_can_synthesize_evidence(None)
# answers True -- correctly, since the router usually reaches either a
# cloud provider or a local model at or above the floor.
#
# On an install with neither, that deferral resolved to whatever was
# left. complexity_router logged "falling below the evidence floor", and
# the turn went on to ask full synthesis of a model the allowlist already
# says cannot do it. The deferral was believed over the allowlist because
# it had no id to check.
#
# What is NOT done here is pinning a model. That was implemented,
# measured and removed once already: a concrete model_id puts the turn in
# front of the safety gate, which on a loaded machine refuses turns that
# would otherwise have run. This changes what the model is asked for, not
# which model answers.

from __future__ import annotations

import pytest

from backend.core import complexity_router, evidence_routing as er


def route(*, model_id=None, mode="automatic", cloud=False, floor=True):
    return er.choose_route(
        expects_evidence=True,
        model_id=model_id,
        mode=mode,
        cloud_available=cloud,
        local_evidence_model_available=floor,
    )


# ------------------------------------------------------
# The case this closes
# ------------------------------------------------------
def test_no_trusted_model_anywhere_stops_asking_for_full_synthesis():
    assert route(mode="automatic", cloud=False, floor=False) != er.ROUTE_NORMAL


@pytest.mark.parametrize("mode,expected", [
    # The same table the other fallbacks already use: Local Mode gets a
    # simplified prompt a weak model can follow; anything else with no
    # cloud to escalate to gets the lookup's own words and no model.
    ("local", er.ROUTE_SIMPLIFIED),
    ("automatic", er.ROUTE_RAW_EVIDENCE),
    ("cloud", er.ROUTE_RAW_EVIDENCE),
])
def test_the_downgrade_follows_the_existing_table(mode, expected):
    assert route(mode=mode, cloud=False, floor=False) == expected


def test_local_mode_still_never_routes_to_cloud():
    # Even with no trusted local model and a cloud key sitting right
    # there. A user in Local Mode said their data does not leave the
    # machine, and a better answer is not worth breaking that.
    assert route(mode="local", cloud=True, floor=False) == er.ROUTE_NORMAL
    assert er.ROUTE_CLOUD != route(mode="local", cloud=False, floor=False)


# ------------------------------------------------------
# What must not change
# ------------------------------------------------------
@pytest.mark.parametrize("mode", ["local", "automatic", "cloud"])
def test_an_install_with_a_trusted_model_is_untouched(mode):
    assert route(mode=mode, cloud=False, floor=True) == er.ROUTE_NORMAL


@pytest.mark.parametrize("mode", ["automatic", "cloud"])
def test_a_configured_cloud_provider_is_still_trusted(mode):
    # Cloud is a frontier model; the local floor says nothing about it.
    assert route(mode=mode, cloud=True, floor=False) == er.ROUTE_NORMAL


def test_a_concrete_model_id_is_judged_on_its_own_merits():
    # The floor is about what a deferral can reach. A turn that already
    # named its model is answered by the allowlist, as before.
    assert route(model_id="nemo-12b-q5", floor=False) == er.ROUTE_NORMAL
    assert route(model_id="qwen2.5-0.5b-instruct-q4_k_m", floor=True) != er.ROUTE_NORMAL


def test_a_turn_with_no_evidence_is_never_downgraded():
    assert er.choose_route(
        expects_evidence=False, model_id=None, mode="automatic",
        cloud_available=False, local_evidence_model_available=False,
    ) == er.ROUTE_NORMAL


def test_the_parameter_defaults_to_the_normal_case():
    """Every caller that predates it behaves exactly as it did."""
    assert er.choose_route(
        expects_evidence=True, model_id=None,
        mode="automatic", cloud_available=False,
    ) == er.ROUTE_NORMAL


# ------------------------------------------------------
# The registry question
# ------------------------------------------------------
def test_the_floor_check_reads_the_registry_and_nothing_else(monkeypatch):
    installed = set()
    monkeypatch.setattr(complexity_router, "_is_installed",
                        lambda model_id: model_id in installed)

    assert complexity_router.evidence_floor_available() is False

    installed.add(complexity_router.MEDIUM_MODEL_ID)
    assert complexity_router.evidence_floor_available() is True


def test_a_model_below_the_floor_does_not_count(monkeypatch):
    ladder = complexity_router._LADDER
    below = ladder[ladder.index(complexity_router.MEDIUM_MODEL_ID) + 1:]
    assert below, "the floor is the bottom of the ladder; this test proves nothing"

    monkeypatch.setattr(complexity_router, "_is_installed", lambda m: m in below)

    # These are exactly the tiers the router refuses to step down to for
    # an evidence turn. Having one installed is not having a trusted one.
    assert complexity_router.evidence_floor_available() is False


def test_the_strongest_model_counts(monkeypatch):
    monkeypatch.setattr(complexity_router, "_is_installed",
                        lambda m: m == complexity_router.DIFFICULT_MODEL_ID)

    assert complexity_router.evidence_floor_available() is True


def test_this_install_has_a_trusted_model():
    """Recorded so the downgrade below is known to be dormant here."""
    assert complexity_router.evidence_floor_available() is True


# ------------------------------------------------------
# The two declarations of "trusted" have to agree
# ------------------------------------------------------
def test_the_ladder_floor_is_on_the_evidence_allowlist():
    """Otherwise the floor holds at a model synthesis will not trust.

    complexity_router refuses to step below MEDIUM_MODEL_ID for an
    evidence turn; evidence_routing decides whether the model that
    results may be asked to synthesize. If those two ever name different
    models, the router protects a turn that the allowlist then downgrades
    anyway -- and the reason would be invisible in both files.
    """
    assert er.model_can_synthesize_evidence(complexity_router.MEDIUM_MODEL_ID)
    assert er.model_can_synthesize_evidence(complexity_router.DIFFICULT_MODEL_ID)


def test_the_tiers_below_the_floor_are_the_untrusted_ones():
    ladder = complexity_router._LADDER
    for model_id in ladder[ladder.index(complexity_router.MEDIUM_MODEL_ID) + 1:]:
        assert not er.model_can_synthesize_evidence(model_id), (
            f"{model_id} sits below the evidence floor but the allowlist "
            f"trusts it; one of the two tables is wrong"
        )


# ------------------------------------------------------
# The orchestrator actually asks
# ------------------------------------------------------
def test_the_orchestrator_supplies_the_floor_to_the_router(monkeypatch):
    """A flag nothing passes is a flag that does nothing.

    The search classifier was wired to an orchestrate_turn parameter both
    transports leave at its default, and shipped inert. Checking that the
    value arrives, rather than that the call looks right, is what would
    have caught it.
    """
    from backend.core import turn_orchestrator as orch

    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return er.ROUTE_NORMAL

    monkeypatch.setattr(orch.evidence_routing, "choose_route", spy)
    monkeypatch.setattr(orch.complexity_router, "evidence_floor_available",
                        lambda: False)
    monkeypatch.setattr(orch.key_manager, "list_configured_providers", dict)

    from backend.tests.test_turn_orchestrator import turn, local  # noqa: F401

    orch.orchestrate_turn(turn("search the web for pytest release notes"),
                          default_local_model=lambda: "test-local-model")

    assert seen.get("local_evidence_model_available") is False


def test_an_unreadable_registry_does_not_downgrade_the_turn(monkeypatch):
    from backend.core import turn_orchestrator as orch

    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return er.ROUTE_NORMAL

    def boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(orch.evidence_routing, "choose_route", spy)
    monkeypatch.setattr(orch.complexity_router, "evidence_floor_available", boom)
    monkeypatch.setattr(orch.key_manager, "list_configured_providers", dict)

    from backend.tests.test_turn_orchestrator import turn

    orch.orchestrate_turn(turn("search the web for pytest release notes"),
                          default_local_model=lambda: "test-local-model")

    # Assume the normal case, which is what happened before this check
    # existed. A registry that will not read is not evidence of anything.
    assert seen.get("local_evidence_model_available") is True
