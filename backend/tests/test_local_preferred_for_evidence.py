# backend/tests/test_local_preferred_for_evidence.py
#
# Automatic Mode prefers a trusted local model for evidence turns.
#
# should_use_local scores a prompt on length and complexity. An
# evidence-bearing prompt is long and complex BECAUSE it is carrying
# evidence, not because the question is hard. Measured on the live
# "Taco Bell's newest menu item?" turn:
#
#     bare question               29 chars   score 100  -> local
#     same question + evidence  5609 chars   score  40  -> cloud
#
# The threshold is 75, so every search turn escalated to cloud the moment
# the lookup succeeded, however capable the installed local models were.
# The better the search worked, the more certain the escalation.
#
# complexity_router already makes this correction one layer down --
# "evidence present; ignoring the length heuristic" -- and picks the
# strongest installed model at or above the evidence floor. Nobody had
# told the success predictor the same thing, so its verdict was reached
# before that ladder was ever consulted.
#
# What this does NOT do: load a model, pin one past the safety gate, or
# touch the evidence floor. It changes which branch is taken, and that
# branch already knows how to choose safely.

from __future__ import annotations

import pytest

from backend.core import auto_selector as sel
from backend.core.complexity_router import prompt_carries_evidence
from backend.core.evidence_routing import model_can_synthesize_evidence
from backend.core.success_predictor import should_use_local

BARE = "Taco Bell's newest menu item?"
WITH_EVIDENCE = (
    BARE + "\n\nTool Results:\n- [Taco Bell newsroom] "
    + "an evidence line about the new item. " * 150
)


@pytest.fixture
def local_only(monkeypatch):
    """A provider for local, and a stand-in for cloud."""
    monkeypatch.setattr(sel, "get_provider", lambda name: f"{name}-provider")
    return sel.AutoSelector()


# ------------------------------------------------------
# The measurement this exists for
# ------------------------------------------------------
def test_the_evidence_is_what_pushed_the_turn_to_cloud():
    """The premise, asserted rather than remembered.

    If the predictor ever stops scoring an evidence prompt below the
    threshold, this override is dead weight and should be reconsidered
    rather than left in place looking meaningful.
    """
    assert should_use_local(BARE) is True
    assert should_use_local(WITH_EVIDENCE) is False


def test_an_evidence_prompt_is_recognised_as_one():
    assert prompt_carries_evidence(WITH_EVIDENCE)
    assert not prompt_carries_evidence(BARE)


# ------------------------------------------------------
# What changed
# ------------------------------------------------------
def test_an_evidence_turn_now_prefers_local(local_only):
    mode, _, model_id = local_only.select_provider(WITH_EVIDENCE)

    assert mode == "local"
    # And the model it landed on is one that may be asked to read
    # evidence -- preferring local is only an improvement if it can.
    assert model_can_synthesize_evidence(model_id)


def test_the_ladder_still_chooses_which_local_model(local_only):
    from backend.core import complexity_router as cr

    _, _, model_id = local_only.select_provider(WITH_EVIDENCE)

    # At or above the floor, not equal to it. Which one depends on free
    # RAM at that instant: the ladder starts at the 12B and steps down
    # to the 7B under pressure, so asserting a specific id makes this
    # test a measurement of the machine. It was written as
    # `== MEDIUM_MODEL_ID`, passed on a busy machine, and failed on an
    # idle one that correctly chose the stronger model.
    #
    # Not pinned here either way: this override decides local-vs-cloud
    # and leaves which-local to the ladder that already knows how.
    floor_index = cr._LADDER.index(cr.MEDIUM_MODEL_ID)
    assert model_id in cr._LADDER[:floor_index + 1]


def test_a_plain_turn_is_untouched(local_only):
    mode, _, model_id = local_only.select_provider(BARE)

    assert mode == "local"
    # Scored 100 on its own merits; the override never ran.
    assert should_use_local(BARE) is True


# ------------------------------------------------------
# When it must not fire
# ------------------------------------------------------
def test_an_untrusted_local_model_does_not_capture_the_turn(monkeypatch, local_only):
    monkeypatch.setattr(sel, "select_local_model_for_prompt",
                        lambda prompt, **k: "qwen2.5-0.5b-instruct-q4_k_m")
    monkeypatch.setattr(sel.key_manager, "list_configured_providers",
                        lambda: {"openai": True})

    mode, _, _ = local_only.select_provider(WITH_EVIDENCE)

    # Preferring local is only an improvement when the local model can
    # do the job. A 0.5B cannot, so the cloud decision stands.
    assert mode == "cloud"


def test_a_prompt_without_evidence_does_not_trigger_it(monkeypatch, local_only):
    monkeypatch.setattr(sel.key_manager, "list_configured_providers",
                        lambda: {"openai": True})

    long_but_evidenceless = "refactor this class. " * 300
    assert not prompt_carries_evidence(long_but_evidenceless)
    assert should_use_local(long_but_evidenceless) is False

    mode, _, _ = local_only.select_provider(long_but_evidenceless)

    # Long and hard is still a reason to escalate. Only the
    # evidence-carrying case is corrected.
    assert mode == "cloud"


def test_no_local_provider_means_no_override(monkeypatch):
    monkeypatch.setattr(sel, "get_provider",
                        lambda name: None if name == "local" else f"{name}-provider")
    monkeypatch.setattr(sel.key_manager, "list_configured_providers",
                        lambda: {"openai": True})

    mode, _, _ = sel.AutoSelector().select_provider(WITH_EVIDENCE)

    assert mode == "cloud"


# ------------------------------------------------------
# What must not have moved
# ------------------------------------------------------
def test_the_evidence_floor_still_decides_which_model(local_only):
    from backend.core import complexity_router as cr

    # The override asks "local or cloud". It does not get a vote on
    # which local model, and must not have acquired one.
    assert cr.evidence_floor_available() is True
    _, _, model_id = local_only.select_provider(WITH_EVIDENCE)
    assert model_id in cr._LADDER[:cr._LADDER.index(cr.MEDIUM_MODEL_ID) + 1]


def test_nothing_here_loads_a_model_or_reaches_the_safety_gate():
    """The constraint that killed the previous attempt at this.

    Pinning a model was implemented, measured and removed once already: a
    concrete model_id puts the turn in front of the safety gate, which on
    a loaded machine refuses turns that would otherwise have run. This
    selects a branch; the loader and the gate run afterwards, unchanged.
    """
    import inspect

    source = inspect.getsource(sel.AutoSelector.select_provider)
    override = source[source.index("Evidence turns prefer local"):]
    override = override[:override.index("Local preferred")]

    for forbidden in ("load_model", "evaluate_safety", "allow_override", "run_in_sandbox"):
        assert forbidden not in override, (
            f"the override reaches {forbidden!r}; it is a branch decision "
            f"and must stay one"
        )


def test_local_mode_is_not_affected():
    """This lives in the Automatic branch only.

    Local Mode never consults AutoSelector -- provider_router's local
    branch goes straight to complexity_router -- so a change here cannot
    reach it, and cannot have introduced a cloud path into a mode whose
    whole promise is that there is none.
    """
    import inspect

    from backend.core import provider_router

    source = inspect.getsource(provider_router.ProviderRouter.resolve)
    local_branch = source[source.index('if mode == "local"'):]
    local_branch = local_branch[:local_branch.index('if mode == "cloud"')]

    # Code, not comments. The local branch's own comment says "no
    # AutoSelector", so a plain substring check reads that sentence and
    # asserts the opposite of what it means.
    code = " ".join(
        line for line in local_branch.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    assert "AutoSelector" not in code
    assert "select_local_model_for_prompt" in code
