"""A turn resolves its provider once, and nothing re-asks for the answer.

THE FAILURE THIS EXISTS FOR
---------------------------
Read out of a real run, in Automatic mode, on a turn whose user message
was 61 characters long:

    [complexity_router] Local model tier selected      chosen: nemo-12b-q5
    [auto_selector]     decision: cloud                model_id: gpt-4
    [provider_router]   Model selection: automatic     resolved: gpt-4
    [provider_router]   WARNING Unknown model_id in explicit resolution: gpt-4
    [provider_router]   Model selection: explicit      resolved: nemo-12b-q5
    ... eight seconds ...
    [streaming_engine]  stream_token  model_id: nemo-12b-q5  token: ' LOCAL'

Three models named in eight milliseconds, and the WARNING is the router
complaining about a model id it had produced itself one millisecond
earlier.

make_generator resolved, wrote the ANSWER into request.model_id, and
handed that to StreamingEngine, which resolved a second time -- with the
first resolution's output as an EXPLICIT request. In Local Mode that is
merely redundant. In Automatic it is destructive, because Automatic can
answer "cloud", and a cloud model is named by provider rather than
registered as an id (see model_registry.model_violates_mode_separation:
"This registry has no individually-registered cloud models"). So the
second resolution could not find gpt-4, and fell back to a local model.

What that cost, all at once: every Automatic cloud escalation silently
became a local turn, routing_history recorded a cloud escalation that
never happened, and the search classifier -- one token, nobody reads it
-- spent eight seconds loading the 12B.
"""

from __future__ import annotations

import pytest

from backend.core import generation
from backend.core.provider_router import ProviderRouter


class Recorder:
    """Every resolve this generation performs, in order."""

    def __init__(self, monkeypatch, answer=None):
        self.calls = []
        self.answer = answer
        real = ProviderRouter.resolve

        def resolve(inner, model_id, prompt=None, history=()):
            self.calls.append(model_id)
            if self.answer is not None:
                return self.answer
            return real(inner, model_id, prompt, history)

        monkeypatch.setattr(ProviderRouter, "resolve", resolve)


class FakeStreaming:
    """StreamingEngine's first act, which is to resolve request.model_id."""

    seen = None

    def stream(self, request, send_packet):
        FakeStreaming.seen = getattr(request, "model_id", "MISSING")
        ProviderRouter().resolve(FakeStreaming.seen, "prompt")
        send_packet({"type": "stream_token", "token": "LOCAL"})


@pytest.fixture
def streaming(monkeypatch):
    FakeStreaming.seen = None
    monkeypatch.setattr(generation, "StreamingEngine", FakeStreaming)
    return FakeStreaming


def test_a_streaming_generation_resolves_exactly_once(monkeypatch, streaming):
    recorder = Recorder(monkeypatch)

    generation.make_generator(None, "automatic", stream_sink=lambda packet: None)("q")

    assert recorder.calls == [None], (
        "the second entry is the router being asked to resolve its own answer")


def test_the_callers_model_id_reaches_the_request_unchanged(monkeypatch, streaming):
    """None is the meaningful value, not a missing one.

    It is what tells ProviderRouter to use its mode-based branches
    instead of the explicit-model one, so overwriting it with a resolved
    id is how Cloud and Automatic routing got turned back into an
    explicit request for a model nobody asked for.
    """
    Recorder(monkeypatch)

    generation.make_generator(None, "automatic", stream_sink=lambda packet: None)("q")

    assert streaming.seen is None


def test_a_pinned_model_still_reaches_the_request(monkeypatch, streaming):
    Recorder(monkeypatch)

    generation.make_generator("nemo-12b-q5", "local",
                              stream_sink=lambda packet: None)("q")

    assert streaming.seen == "nemo-12b-q5"


def test_a_cloud_decision_is_no_longer_re_asked_as_an_explicit_model(monkeypatch, streaming):
    """The exact shape of the measured failure.

    A router that answers "cloud, gpt-4" must not then be asked to
    resolve "gpt-4", because the registry holds no cloud model ids and
    the answer comes back as whatever local model is the default.
    """
    recorder = Recorder(monkeypatch, answer=(object(), "gpt-4"))

    generation.make_generator(None, "automatic", stream_sink=lambda packet: None)("q")

    assert "gpt-4" not in recorder.calls
    assert recorder.calls == [None]
