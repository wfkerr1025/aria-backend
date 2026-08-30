# backend/tests/test_model_roles.py
#
# What each installed model is for.
#
# The table is a statement about THIS machine, so most of this file is
# about keeping it honest against the machine rather than against itself:
# a role table whose keys name models that are not installed routes every
# turn to nothing, silently, while every test that uses the same family
# names still passes.
#
# That is not hypothetical. The 12B is registered as `nemo-12b-q5` and is
# called `mistral-nemo-12b` everywhere else, including in the
# specification this table implements -- so the obvious prefix match
# finds it zero times and the whole heavy-reasoning route is dead code.
# test_every_family_resolves_to_something_installed is the test that
# would have caught it.

from __future__ import annotations

import pytest

from backend.config import model_roles as mr


# ======================================================
# The four, exactly as specified
# ======================================================
@pytest.mark.parametrize("family,role,chat,tools,supervise", [
    ("qwen2.5-0.5b", "router", False, False, False),
    ("phi-3-mini-4k-instruct-q4", "supervisor", True, False, True),
    ("mistral-7b", "chat_tools", True, True, False),
    ("mistral-nemo-12b", "heavy_reasoning", True, True, False),
])
def test_the_table_says_what_it_should(family, role, chat, tools, supervise):
    described = mr.MODEL_ROLES[family]

    assert described["role"] == role
    assert described["can_chat"] is chat
    assert described["can_tools"] is tools
    assert described["can_supervise"] is supervise


def test_the_table_holds_exactly_these_four():
    # A fifth entry added without a decision about where it routes is how
    # a table stops describing the machine.
    assert set(mr.MODEL_ROLES) == {
        "qwen2.5-0.5b", "phi-3-mini-4k-instruct-q4",
        "mistral-7b", "mistral-nemo-12b",
    }


def test_the_smallest_model_is_never_allowed_to_talk():
    # The whole reason the table exists. It is genuinely useful for
    # classification and genuinely unable to hold a conversation, and
    # both facts have to be written down or it gets used for the wrong
    # one.
    assert mr.can_chat("qwen2.5-0.5b") is False
    assert mr.can_tools("qwen2.5-0.5b") is False


def test_exactly_one_model_supervises():
    supervisors = [f for f, d in mr.MODEL_ROLES.items() if d["can_supervise"]]
    assert supervisors == ["phi-3-mini-4k-instruct-q4"]


# ======================================================
# The keys resolve to models that are really here
# ======================================================
@pytest.mark.parametrize("family", sorted(MODEL_FAMILIES := set(mr.MODEL_ROLES)))
def test_every_family_resolves_to_something_installed(family):
    from backend.core.model_registry import get_model_ids

    resolved = mr.installed_model_for(family)

    assert resolved is not None, (
        f"{family!r} matches no installed model. The heavy-reasoning route "
        f"was dead code for exactly this reason -- see ALIASES."
    )
    assert resolved in get_model_ids()


def test_the_alias_is_what_makes_the_twelve_b_reachable():
    # Written down as its own test because it is the one entry that does
    # not follow the prefix convention, and a future cleanup that
    # "tidies" ALIASES away would silently disable heavy reasoning.
    assert not "nemo-12b-q5".startswith("mistral-nemo-12b")
    assert mr.installed_model_for("mistral-nemo-12b") == "nemo-12b-q5"


def test_a_registry_id_with_a_quantisation_suffix_still_matches():
    # Families, not ids: a re-quantised rebuild of the same model is the
    # same model as far as this table is concerned.
    assert mr.describe_role("mistral-7b-q4km") == "chat_tools"
    assert mr.describe_role("qwen2.5-0.5b-instruct-q4_k_m") == "router"
    assert mr.describe_role("nemo-12b-q5") == "heavy_reasoning"


# ======================================================
# Everything else
# ======================================================
def test_an_unknown_model_may_chat_but_not_act():
    described = mr.role_of("some-model-nobody-registered")

    # Permissive about talking: a model the user installed on purpose
    # must not be unusable because nobody wrote it down here.
    assert described["can_chat"] is True
    # Conservative about acting: nothing here has any evidence it can
    # hold an action packet, and an action packet is executed.
    assert described["can_tools"] is False
    assert described["can_supervise"] is False
    assert described["role"] == "chat_tools"


def test_the_unknown_default_matches_the_documented_one():
    assert mr.role_of("nobody-has-heard-of-this") == dict(mr.DEFAULT_ROLE)


def test_none_is_the_router_choosing_and_is_fully_capable():
    # None means Cloud or Automatic, where a frontier model answers.
    # Describing that as "cannot use tools" would have the strongest
    # configuration on the machine reporting itself as the weakest.
    described = mr.role_of(None)

    assert described["can_chat"] is True
    assert described["can_tools"] is True


def test_a_lookup_never_raises():
    for value in (None, "", "   ", "a", "x" * 500):
        assert isinstance(mr.role_of(value), dict)


def test_the_table_is_not_handed_out_by_reference():
    # A caller that mutated what it was given would rewrite the roles for
    # every later turn in the process.
    described = mr.role_of("mistral-7b")
    described["can_tools"] = False

    assert mr.can_tools("mistral-7b") is True


def test_this_module_decides_nothing_about_routing():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mr))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    # It says what each model IS. When a turn should use one is
    # backend/chat/model_router.py, and a table that also routed would be
    # a second router disagreeing with the first.
    assert "select_model_for_turn" not in called
    assert "classify_turn" not in called


# ======================================================
# Which model takes which turn
#
# One table, read by model_router when it can name a model and by
# complexity_router's role floor when it defers. Two copies would let
# Local mode and Automatic mode route differently, which is exactly the
# bug class this codebase keeps producing.
# ======================================================
def test_every_turn_kind_has_a_family():
    from backend.chat.model_router import (
        TURN_CHAT, TURN_CLASSIFICATION, TURN_HEAVY, TURN_TOOLS,
    )

    for kind in (TURN_CLASSIFICATION, TURN_CHAT, TURN_TOOLS, TURN_HEAVY):
        assert mr.family_for_turn(kind) in mr.MODEL_ROLES


def test_file_work_goes_to_the_twelve_b():
    # Measured, not assumed. On six file tasks the 7B and the 12B
    # proposed an action equally often and failed in different places:
    # the 7B could not produce one at all for "create a README.md
    # describing this project" -- the only task needing something written
    # from scratch -- and took fifty seconds to fail. The 12B wrote a
    # real README. It costs about 1.3 seconds a turn on this machine.
    assert mr.family_for_turn("tools") == "mistral-nemo-12b"
    assert mr.family_for_turn("heavy_reasoning") == "mistral-nemo-12b"


def test_chat_does_not_go_to_the_twelve_b():
    # The other half of the trade. A greeting must not wake the slowest
    # model on the machine.
    assert mr.family_for_turn("chat") == "phi-3-mini-4k-instruct-q4"


def test_classification_stays_on_the_smallest():
    assert mr.family_for_turn("classification") == "qwen2.5-0.5b"


def test_an_unknown_turn_kind_falls_back_to_chat():
    assert mr.family_for_turn("something-invented-later") == "phi-3-mini-4k-instruct-q4"


def test_both_routing_layers_read_the_same_table():
    import inspect

    from backend.chat import model_router
    from backend.core import complexity_router

    # model_router names a model in Local mode; complexity_router floors
    # the ladder in Automatic. Reading one table is what makes those two
    # agree by construction rather than by being edited together.
    assert "family_for_turn" in inspect.getsource(model_router)
    assert "family_for_turn" in inspect.getsource(complexity_router)


def test_every_family_in_the_routing_table_is_installed():
    for kind in mr.FAMILY_FOR_TURN:
        family = mr.family_for_turn(kind)
        assert mr.installed_model_for(family) is not None, (
            f"{kind} routes to {family}, which is not installed")
