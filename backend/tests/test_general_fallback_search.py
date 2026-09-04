# backend/tests/test_general_fallback_search.py
#
# Whether a question needs the web, decided by the model.
#
# The vocabulary in search_intent answers "did the user ask for a lookup".
# It cannot answer "does this question have an answer the model does not
# hold", and these were the measured misses:
#
#     "What is on Taco Bell's menu"      no lookup planned
#     "What time does Costco close"      no lookup planned
#     "population of Denmark"            no lookup planned
#
# A larger word list does not fix it. This is a Unity assistant, and the
# words that would catch those questions -- menu, cost, hours, store,
# update, release, location, capital -- are the words its users type all
# day about software. Measured: a marker list of that kind fired on
# fourteen of fourteen ordinary development requests.
#
# So the model is asked. Every test here supplies its own generator; none
# of them loads a model or reaches a network.

from __future__ import annotations

import pytest

from backend.core import search_activation as sa
from backend.core import turn_orchestrator as _orch
from backend.planning.plan_builder import PlanBuilder

# Captured at import, before conftest's autouse guard replaces it.
REAL_BUILDER = _orch._classifier_generator


@pytest.fixture(autouse=True)
def clean_cache():
    sa.reset_cache()
    yield
    sa.reset_cache()


def saying(verdict):
    """A generator that always answers `verdict`, and counts its calls."""
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return verdict

    generate.calls = calls
    return generate


# The queries from the report, which a word list could not reach.
REAL_WORLD = [
    "What is on Taco Bell's menu",
    "What time does Costco close",
    "population of Denmark",
    "how much does a Model 3 cost",
    "what is the capital of Peru",
    "symptoms of scurvy",
]

# Ordinary development work. Every one of these fired on the marker-list
# approach, which is why that approach is not the one here.
DEV_WORK = [
    "store the value in a variable",
    "update the shader config",
    "release the lock",
    "what is the location of the bug",
    "capital letter check in the parser",
    "add a menu item to the editor window",
    "how much does this cost in frame time",
    "brand the splash screen",
]


# ------------------------------------------------------
# The three tests from the patch, against the real system
# ------------------------------------------------------
def test_general_search_activates_for_real_world_queries():
    assert sa.wants_web_search("What is on Taco Bell's menu", generate=saying("WEB"))


def test_general_search_does_not_activate_for_local_questions():
    assert not sa.wants_web_search("Explain recursion", generate=saying("LOCAL"))


def test_general_search_activates_for_unknown_entities():
    assert sa.wants_web_search("What time does Costco close", generate=saying("WEB"))


# ------------------------------------------------------
# The gap, closed
# ------------------------------------------------------
@pytest.mark.parametrize("query", REAL_WORLD)
def test_a_real_world_question_reaches_the_classifier(query):
    generate = saying("WEB")

    assert sa.wants_web_search(query, generate=generate)
    # It got there by being asked, not by matching a word.
    assert len(generate.calls) == 1
    assert query in generate.calls[0]


@pytest.mark.parametrize("query", DEV_WORK)
def test_ordinary_development_work_is_left_alone(query):
    # The model is asked, and says LOCAL. What matters is that nothing
    # upstream of the model decided these were searches on the strength
    # of a shared word.
    assert not sa.wants_web_search(query, generate=saying("LOCAL"))


@pytest.mark.parametrize("query", REAL_WORLD)
def test_the_planner_follows_the_classifier(query):
    sa.prime(query, saying("WEB"))

    step = PlanBuilder().search_step(query, 1)

    assert step is not None, "routing would search and the planner would not"
    assert step.args["query"].strip()


# ------------------------------------------------------
# What is never delegated
# ------------------------------------------------------
@pytest.mark.parametrize("query", [
    "search my notes for the shader error",
    "what's happening in this repo right now",
    "look up the breaking news in my documents",
])
def test_the_local_scope_veto_is_not_the_models_to_overturn(query):
    generate = saying("WEB")

    assert not sa.wants_web_search(query, generate=generate)
    # A question about the user's own material must not leave the
    # machine, and it must not be sent to a classifier to find that out.
    assert generate.calls == []


@pytest.mark.parametrize("query", [
    "search the web for pytest 8.2",
    "look up the current EUR/USD exchange rate",
    "python latest news",
])
def test_an_explicit_request_does_not_pay_for_an_inference(query):
    generate = saying("LOCAL")

    assert sa.wants_web_search(query, generate=generate)
    # The vocabulary already answered. Confirming it with the model
    # would be latency bought for nothing -- and the model saying LOCAL
    # must not override an instruction the user gave in words.
    assert generate.calls == []


# ------------------------------------------------------
# Failing in the right direction
# ------------------------------------------------------
def test_a_classifier_that_raises_does_not_search():
    def boom(prompt):
        raise RuntimeError("model unavailable")

    assert sa.wants_web_search("What time does Costco close", generate=boom) is False


@pytest.mark.parametrize("answer", ["", "   ", "I think perhaps you should",
                                    "yes", "42", None])
def test_an_unparseable_verdict_does_not_search(answer):
    # A model that answered with a sentence has not answered the question
    # asked, and reading a verdict out of it would be inventing one.
    assert not sa.wants_web_search("What time does Costco close",
                                   generate=saying(answer))


def test_a_verdict_is_read_from_the_first_word_the_model_commits_to():
    assert sa.wants_web_search("q one", generate=saying("WEB")) is True
    assert sa.wants_web_search("q two", generate=saying("LOCAL")) is False
    assert sa.wants_web_search("q three", generate=saying("web\n")) is True
    assert sa.wants_web_search("q four", generate=saying("Answer: LOCAL")) is False


def test_no_generator_is_exactly_the_deterministic_answer():
    from backend.core.search_intent import mentions_web_search

    for query in REAL_WORLD + DEV_WORK + ["python latest news", "search the web"]:
        assert sa.wants_web_search(query) == mentions_web_search(query), query


def test_the_planner_stays_deterministic_when_nothing_primed_it():
    """The property the planner's own suite pins, still true.

    A turn that never reached the classifier plans exactly what it
    planned before this module existed.
    """
    for query in REAL_WORLD:
        assert PlanBuilder().search_step(query, 1) is None


# ------------------------------------------------------
# One verdict per turn
# ------------------------------------------------------
def test_routing_and_planning_do_not_each_pay_for_an_inference():
    generate = saying("WEB")
    query = "What is on Taco Bell's menu"

    sa.prime(query, generate)
    sa.wants_web_search(query)          # routing
    PlanBuilder().search_step(query, 1)  # planning

    assert len(generate.calls) == 1


def test_routing_and_planning_cannot_reach_different_verdicts():
    """The failure search_intent was written to end.

    A query that routes as a search and then plans no search tells the
    user nothing is wrong and invents the answer. Two independent model
    calls could land on either side of it, so there is only ever one.
    """
    query = "how much does a Model 3 cost"
    flips = iter(["WEB", "LOCAL"])

    sa.prime(query, lambda prompt: next(flips))

    assert sa.wants_web_search(query) is True
    assert PlanBuilder().search_step(query, 1) is not None


def test_the_cache_does_not_grow_without_bound():
    generate = saying("LOCAL")
    for n in range(sa._CACHE_LIMIT * 2):
        sa.wants_web_search(f"question number {n}", generate=generate)

    assert len(sa._CACHE) <= sa._CACHE_LIMIT


# ------------------------------------------------------
# The prompt
# ------------------------------------------------------
def test_the_prompt_asks_about_knowledge_not_phrasing():
    prompt = sa.CLASSIFIER_PROMPT.lower()

    # "Is this a search query" invites the model to think about wording,
    # which is the failure the vocabulary already has.
    assert "information you do not" in prompt
    assert "web" in prompt and "local" in prompt


def test_the_classifier_is_cheap_by_construction():
    # It is asked for one word. Letting it warm up to a sentence costs
    # latency on every turn it runs.
    assert sa.MAX_TOKENS <= 8
    assert sa.TEMPERATURE == 0.0


# ------------------------------------------------------
# The turn actually reaches it
# ------------------------------------------------------
def test_the_orchestrator_primes_the_verdict_before_routing():
    """Without this call the whole module is inert.

    detect_intent and the planner both read the verdict; neither of them
    spends an inference. If nothing primes it, a real-world question
    routes as ordinary chat and no lookup is ever planned -- which is
    exactly the behaviour this replaced.
    """
    import inspect

    from backend.core import turn_orchestrator

    source = inspect.getsource(turn_orchestrator.orchestrate_turn)
    primed = source.index("search_activation.prime")
    routed = source.index("intent = detect_intent")

    assert primed < routed, "the verdict is primed after routing has already read it"


@pytest.fixture
def real_builder(monkeypatch):
    """conftest suppresses the builder suite-wide; this file tests it."""
    from backend.core import turn_orchestrator as orch

    monkeypatch.setattr(orch, "_classifier_generator", REAL_BUILDER)
    return REAL_BUILDER


def test_the_classifier_is_given_a_real_generator(monkeypatch, real_builder):
    """Ordering was never the thing that broke.

    The first version of this wiring read `generator`, an orchestrate_turn
    parameter that both transports leave at its default -- its own
    docstring says it is "unused until the Phase core is inserted". So
    prime() was called on every turn with generate=None, returned the
    deterministic verdict, and a live "Taco Bell's newest menu item?"
    planned no lookup and answered from a 0.5B model's weights.

    The test above passed throughout: it checked that two lines were in
    the right order, not that anything arrived. This one checks what
    actually has to be true.
    """
    from backend.core import turn_orchestrator as orch

    built = []

    def fake_make_generator(model_id, mode, **kwargs):
        built.append({"model_id": model_id, "mode": mode, **kwargs})
        return lambda prompt: "LOCAL"

    monkeypatch.setattr(orch, "make_generator", fake_make_generator)

    class Session:
        mode = "local"
        explicit_model_override = None

    class Request:
        session = Session()
        requested_model_id = None

    generate = real_builder(Request(), lambda: "nemo-12b-q5", None)

    assert generate is not None, "the classifier would be handed None on every turn"
    assert generate("anything") == "LOCAL"
    # The 0.5B, not the chat model. The classifier's whole output is a
    # one-word verdict nobody reads, and it used to load the 12B to
    # produce it -- the single most expensive way to answer the cheapest
    # question in the turn. backend/chat/model_router.py routes a
    # classification turn to the router model, which is the one job the
    # 0.5B is genuinely good at.
    from backend.config.model_roles import installed_model_for

    assert built[0]["model_id"] == installed_model_for("qwen2.5-0.5b")
    # Asked for one word, not a paragraph.
    assert built[0]["max_tokens"] == sa.MAX_TOKENS
    assert built[0]["temperature"] == sa.TEMPERATURE


@pytest.mark.parametrize("mode, expected", [
    # Local already named the 0.5B, and still does.
    ("local", "the classifier's own model"),
    # Automatic used to defer, which sent a one-token verdict through the
    # full ladder: escalated to openai, resolved gpt-4, failed to find it
    # in a registry that holds no cloud ids, and loaded the 12B instead.
    ("automatic", "the classifier's own model"),
    # Cloud is untouched. Naming a local model there would break absolute
    # mode separation, and Automatic is the one mode exempt from it.
    ("cloud", None),
])
def test_which_model_the_classifier_asks_for_in_each_mode(monkeypatch, real_builder,
                                                          mode, expected):
    from backend.chat import model_router
    from backend.core import turn_orchestrator as orch
    from backend.config.model_roles import installed_model_for

    built = []
    monkeypatch.setattr(orch, "make_generator",
                        lambda model_id, turn_mode, **kwargs:
                        built.append(model_id) or (lambda prompt: "LOCAL"))

    class Session:
        explicit_model_override = None

    Session.mode = mode

    class Request:
        session = Session()
        requested_model_id = None

    real_builder(Request(), lambda: None, None)

    wanted = (model_router.classification_model() if expected else None)
    assert built == [wanted]
    if mode != "cloud":
        assert wanted == installed_model_for("qwen2.5-0.5b")


def test_a_generator_the_caller_supplied_is_used_as_is(monkeypatch, real_builder):
    from backend.core import turn_orchestrator as orch

    def boom(*args, **kwargs):
        raise AssertionError("built one when the caller had already supplied it")

    monkeypatch.setattr(orch, "make_generator", boom)
    supplied = saying("WEB")

    assert real_builder(object(), lambda: None, supplied) is supplied


def test_a_model_that_will_not_load_is_not_a_failed_turn(monkeypatch, real_builder):
    from backend.core import turn_orchestrator as orch

    def unavailable(*args, **kwargs):
        raise RuntimeError("no model could be loaded")

    monkeypatch.setattr(orch, "make_generator", unavailable)

    class Session:
        mode = "local"
        explicit_model_override = None

    class Request:
        session = Session()
        requested_model_id = None

    # None, not an exception: the turn goes on and answers without a
    # lookup, which is what it did before the classifier existed.
    assert real_builder(Request(), lambda: None, None) is None


# ------------------------------------------------------
# The generator has to actually reach a provider
# ------------------------------------------------------
def test_the_provider_protocol_is_run_not_infer():
    """A latent bug, recorded rather than fixed.

    generation.make_generator's no-sink branch calls provider.infer().
    infer() is LocalInferenceEngine's method; the provider protocol is
    run()/stream(), and this asserts what all fifteen wrappers actually
    implement. That branch raises AttributeError on its first line.

    It has never executed -- every transport supplies a stream_sink -- so
    nothing has ever depended on it. Repairing it was tried and reverted:
    it changed what Engine B does on turns where no model can load, which
    broke a characterization suite for reasons that had nothing to do
    with search. The classifier passes a discarding sink instead and
    takes the path production takes.

    Pinned against the wrappers themselves, not against memory.
    """
    import pathlib

    # __path__, not __file__: backend.llm.providers is a namespace
    # package, so __file__ is None.
    repo = pathlib.Path(__file__).resolve().parents[2]
    providers_dir = repo / "backend" / "llm" / "providers"
    assert providers_dir.is_dir()

    for path in sorted(providers_dir.glob("*_wrapper.py")):
        source = path.read_text(encoding="utf-8")
        assert "def run(" in source, f"{path.name} has no run()"
        assert "def infer(" not in source, f"{path.name} implements infer()"
