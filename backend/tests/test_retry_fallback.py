"""Trying again, when trying again is honest.

A retry is only correct when the SAME request, sent again, could
plausibly succeed. That is much narrower than "it failed", and the gap
between the two is where retry logic goes wrong: a 401 retried three
times is refused three times, and a paid generation retried three
times is charged three times.

THE THREE TIERS DECIDE THIS TOO
-------------------------------
    killable      a retry starts genuinely fresh
    abandonable   a retry runs CONCURRENTLY with the attempt it
                  replaced -- two threads, one result, which is why
                  the counts are small
    already spent a retry is a SECOND CHARGE

THERE WAS NO FallbackManager
----------------------------
The brief said to integrate with one. Nothing by that name exists in
this codebase, and nothing else did the job either: a provider failure
reached streaming_engine's `except Exception`, became a stream_error
packet, and the turn was over. backend/core/fallback_chain.py is that
missing piece.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from backend.core import fallback_chain, retry_policy, tool_jobs, tool_registry
from backend.core.tool_jobs import ToolJobSet, ToolJobSpec
from backend.websocket.handlers import WebSocketHandler


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        try:
            self.sent.append(json.loads(message)
                             if isinstance(message, str) else message)
        except ValueError:
            self.sent.append({"type": "raw", "message": message})

    async def close(self):
        pass


@pytest.fixture
def handler():
    return WebSocketHandler(FakeWebSocket())


@pytest.fixture
def job_set():
    return ToolJobSet()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tool(monkeypatch):
    """Register a tool that fails a set number of times, then works."""
    made = []
    counts = {}

    def make(name, family, error="Read timed out", fail_times=0, seconds=0.0):
        counts[name] = 0

        def handler():
            counts[name] += 1
            if seconds:
                time.sleep(seconds)
            if counts[name] <= fail_times:
                raise RuntimeError(error)
            return f"{name}-ok"

        tool_registry.register_tool(
            tool_registry.ToolSchema(name=name, description="t", parameters={},
                                     timeout_seconds=5.0, family=family),
            handler)
        made.append(name)
        return name

    make.counts = counts
    yield make

    for name in made:
        tool_registry.unregister_tool(name)


# ======================================================
# Reading a failure
# ======================================================

@pytest.mark.parametrize("message", [
    "Read timed out", "connection reset by peer", "503 Service Unavailable",
    "502 Bad Gateway", "429 too many requests", "408 Request Timeout",
    "temporarily unavailable", "the model is still loading",
    "remote end closed connection",
])
def test_a_transient_failure_is_worth_another_go(message):
    assert retry_policy.classify(message) == retry_policy.TRANSIENT


@pytest.mark.parametrize("message", [
    "401 Unauthorized", "403 Forbidden", "invalid api key",
    "404 not found", "no such model", "context length exceeded",
    "invalid request", "malformed payload", "permission denied",
])
def test_a_permanent_failure_is_not(message):
    assert retry_policy.classify(message) == retry_policy.PERMANENT


@pytest.mark.parametrize("message", [
    "insufficient credits", "insufficient credit", "out of credits",
    "quota exceeded", "payment required",
    "Ludo.ai refused: HTTP 402 -- insufficient credits",
])
def test_running_out_of_money_is_never_transient(message):
    """The single most important thing on the list not to retry.

    "insufficient credits" -- the exact string Ludo returns -- was
    classified UNKNOWN during the build, because the pattern said
    `credit\\b` and the word is plural."""
    assert retry_policy.classify(message) == retry_policy.PERMANENT


def test_an_unrecognised_failure_is_not_retried():
    """The safe direction. A retry that cannot help still spends the
    turn's budget, and for a spending family its money."""
    verdict = retry_policy.classify("something nobody has seen before")

    assert verdict == retry_policy.UNKNOWN
    assert not retry_policy.PROVIDER_POLICY.allows(verdict)


def test_a_permanent_reason_beside_a_transient_word_stays_permanent():
    """Cloud bodies say "try again later" under a 401 all the time."""
    assert retry_policy.classify(
        "401 Unauthorized -- please try again later") == retry_policy.PERMANENT


# ======================================================
# The policies themselves
# ======================================================

def test_backoff_grows_and_is_capped():
    policy = retry_policy.RetryPolicy(max_attempts=6, backoff_base=1.0,
                                      backoff_factor=2.0, backoff_cap=4.0)
    waits = [policy.wait_before(n) for n in range(1, 7)]

    assert waits[0] == 0.0, "the first attempt never waits"
    assert waits[1:4] == [1.0, 2.0, 4.0]
    assert all(w <= 4.0 for w in waits), "the cap is not optional"


def test_ludo_is_never_resubmitted():
    """A generation is charged when the request is MADE. A second
    submission is a second charge for the same asset."""
    policy = retry_policy.policy_for("ludo")

    assert policy.max_attempts == 1
    assert policy.resubmit == retry_policy.RESUBMIT_NEVER


def test_polling_a_paid_job_is_free_and_may_be_patient():
    """The other half of the Ludo rule: waiting on work already paid
    for costs nothing, which is why a timeout hands back a job id."""
    poll = retry_policy.policy_for("ludo_poll")

    assert poll.max_attempts > 1
    assert poll.resubmit == retry_policy.RESUBMIT_NEVER


def test_no_policy_guarantees_a_timeout():
    """A policy whose waits alone outlast the turn budget spends the
    whole budget on sleeping and is interrupted before it can help."""
    from backend.core import turn_budget

    shortest = min(turn_budget.BUDGETS.values())
    for name, policy in retry_policy.POLICIES.items():
        assert policy.total_worst_case() < shortest, name
    assert retry_policy.PROVIDER_POLICY.total_worst_case() < shortest


def test_the_abandonable_families_stay_shallow():
    """A retry of a thread-backed tool runs CONCURRENTLY with the
    attempt it replaced. Five attempts is five live threads."""
    for family in ("search", "registry"):
        assert retry_policy.policy_for(family).max_attempts <= 3


# ======================================================
# Waiting, and the three things that outrank it
# ======================================================

def test_a_wait_that_will_not_fit_the_budget_is_abandoned():
    policy = retry_policy.RetryPolicy(max_attempts=3, backoff_base=5.0)

    went = retry_policy.sleep_before_retry(policy, 2, remaining_budget=1.0)

    assert went is False


def test_a_stopped_turn_stops_waiting():
    policy = retry_policy.RetryPolicy(max_attempts=3, backoff_base=5.0)
    stop = threading.Event()
    stop.set()

    started = time.monotonic()
    went = retry_policy.sleep_before_retry(policy, 2, stopped=stop.is_set)

    assert went is False
    assert time.monotonic() - started < 1.0, "it slept anyway"


def test_a_stop_lands_during_a_long_backoff():
    """Slept in small steps, so a stop takes effect within a poll
    interval rather than at the end of an eight-second wait."""
    policy = retry_policy.RetryPolicy(max_attempts=3, backoff_base=5.0)
    stop = threading.Event()
    threading.Timer(0.15, stop.set).start()

    started = time.monotonic()
    went = retry_policy.sleep_before_retry(policy, 2, stopped=stop.is_set,
                                           poll=0.05)
    elapsed = time.monotonic() - started

    assert went is False
    assert elapsed < 2.0, f"waited {elapsed:.1f}s after the stop"


def test_a_wait_that_fits_is_taken():
    policy = retry_policy.RetryPolicy(max_attempts=3, backoff_base=0.05)

    assert retry_policy.sleep_before_retry(
        policy, 2, remaining_budget=30.0) is True


# ======================================================
# Tool-family retry
# ======================================================

def test_a_transient_tool_failure_is_retried(tool, job_set):
    tool("flaky", "search", error="Read timed out", fail_times=2)

    jobs = _run(job_set.run_all([ToolJobSpec(tool="flaky")]))

    assert jobs[0].state == tool_jobs.DONE
    assert jobs[0].attempts == 3
    assert tool.counts["flaky"] == 3


def test_a_hard_tool_failure_is_not_retried(tool, job_set):
    tool("refused", "search", error="401 Unauthorized", fail_times=9)

    jobs = _run(job_set.run_all([ToolJobSpec(tool="refused")]))

    assert jobs[0].state == tool_jobs.FAILED
    assert jobs[0].attempts == 1
    assert tool.counts["refused"] == 1, "it was sent again to be refused again"


def test_ludo_work_is_never_sent_twice(tool, job_set):
    """The money test. Even a TRANSIENT failure does not resubmit,
    because a submission that timed out may still have been charged."""
    tool("paid", "ludo", error="Read timed out", fail_times=9)

    jobs = _run(job_set.run_all([ToolJobSpec(tool="paid")]))

    assert jobs[0].attempts == 1
    assert tool.counts["paid"] == 1, "a paid generation was submitted twice"


def test_a_cli_tool_gets_one_second_chance(tool, job_set):
    """Killable, so a retry starts fresh -- but a Blender render is
    minutes of CPU, so one retry and no more."""
    tool("editor", "cli", error="connection refused", fail_times=1)

    jobs = _run(job_set.run_all([ToolJobSpec(tool="editor")]))

    assert jobs[0].state == tool_jobs.DONE
    assert jobs[0].attempts == 2


def test_retries_stop_when_the_turn_is_stopped(tool, job_set):
    """bind_turn hands the jobs the SAME threading.Event the stop
    button and the deadline set. A retry reading its own flag would be
    a second opinion about whether the turn is over."""
    tool("stubborn", "search", error="Read timed out", fail_times=9)
    stop = threading.Event()
    job_set.bind_turn(stop_flag=stop)

    async def scenario():
        jobs = await job_set.run_all([ToolJobSpec(tool="stubborn")])
        return jobs

    stop.set()
    jobs = _run(scenario())

    assert jobs[0].attempts == 1, "it retried after the turn had ended"


def test_retries_stop_when_the_budget_is_gone(tool, job_set):
    tool("slowpoke", "search", error="Read timed out", fail_times=9)
    job_set.bind_turn(deadline=time.monotonic() + 0.01)

    jobs = _run(job_set.run_all([ToolJobSpec(tool="slowpoke")]))

    assert jobs[0].attempts == 1, "it backed off into a guaranteed timeout"


def test_the_attempt_count_is_reported(tool, job_set):
    """A user told "it failed" deserves to know it was tried three
    times rather than once."""
    tool("counted", "search", error="Read timed out", fail_times=1)

    jobs = _run(job_set.run_all([ToolJobSpec(tool="counted")]))

    assert jobs[0].as_dict()["attempts"] == 2


def test_every_attempt_goes_through_the_one_door(tool, job_set, monkeypatch):
    """Validation, permissions and the sandbox apply to attempt three
    exactly as to attempt one."""
    seen = []
    real = tool_registry.execute_tool
    monkeypatch.setattr(tool_jobs, "execute_tool",
                        lambda name, args=None: seen.append(name) or real(name, args))
    tool("doored", "search", error="Read timed out", fail_times=2)

    _run(job_set.run_all([ToolJobSpec(tool="doored")]))

    assert seen == ["doored", "doored", "doored"]


# ======================================================
# The fallback chain
# ======================================================

def test_the_chain_never_walks_below_the_capability_floor():
    """CHAT_PARAM_FLOOR exists because a 0.5B answers an ordinary
    question by inventing a system prompt and replying to one nobody
    asked. A chain that walked below it would reintroduce the gate's
    failure mode through a side door."""
    from backend.core import chat_capability_gate

    for mode in fallback_chain.CHAINS:
        for model in fallback_chain.chain_for(mode):
            assert not chat_capability_gate.too_weak_for_chat(model), \
                f"{model} is below the floor and is in the {mode} chain"


def test_the_smallest_installed_model_is_in_no_chain():
    """Named explicitly, because this is the one that matters."""
    for mode in fallback_chain.CHAINS:
        assert "qwen2.5-0.5b-instruct-q4_k_m" not in fallback_chain.chain_for(mode)


def test_the_chain_walks_in_order():
    first = fallback_chain.next_model("nemo-12b-q5")
    second = fallback_chain.next_model(first, tried=["nemo-12b-q5"])

    assert first == "mistral-7b-q4km"
    assert second == "phi-3-mini-4k-instruct-q4"


def test_a_model_is_never_tried_twice():
    """Including the model the turn STARTED on, which may not be in
    the chain at all -- an override, or a cloud provider."""
    following = fallback_chain.next_model(
        "some-cloud-model", tried=["nemo-12b-q5", "mistral-7b-q4km"])

    assert following == "phi-3-mini-4k-instruct-q4"


def test_an_exhausted_chain_says_so():
    """None is a real answer. A user who has silently had three models
    tried on their behalf should be told that is what happened."""
    assert fallback_chain.next_model(
        "phi-3-mini-4k-instruct-q4",
        tried=["nemo-12b-q5", "mistral-7b-q4km"]) is None


def test_a_tool_turn_does_not_fall_back_to_a_weaker_model():
    """"Smaller" is right for a chat turn and WRONG for one that needs
    tool calls: a model that cannot hold a tool call does not degrade
    the turn, it answers the wrong question confidently."""
    chat = fallback_chain.chain_for(fallback_chain.MODE_CHAT)
    heavy = fallback_chain.chain_for(fallback_chain.MODE_HEAVY)

    assert len(heavy) < len(chat)
    assert "phi-3-mini-4k-instruct-q4" not in heavy


def test_the_mode_follows_what_the_turn_needs():
    assert fallback_chain.mode_for_turn(needs_tools=True) == fallback_chain.MODE_HEAVY
    assert fallback_chain.mode_for_turn() == fallback_chain.MODE_CHAT
    assert fallback_chain.mode_for_turn(conserving=True) == fallback_chain.MODE_SAFE


def test_the_chain_returns_an_id_and_never_loads_it():
    """It must not switch a model around the safety gate. Returning an
    id means the caller re-enters the same path a first attempt takes,
    so mode separation and _evaluate_safety run over a fallback
    exactly as over an original choice."""
    # Anchored on what the module can REACH, not on the words
    # appearing in it -- its own docstring explains at length that it
    # must not switch a model around the safety gate, and a text
    # search matched that explanation. The third time this trap has
    # been walked into in this codebase; the fix is always the same.
    import sys

    module = sys.modules[fallback_chain.__name__]
    reachable = {name for name in dir(module) if not name.startswith("__")}

    for forbidden in ("load_model", "switch_model", "evaluate_safety",
                      "ProviderRouter", "StreamingEngine"):
        assert forbidden not in reachable, f"fallback_chain can reach {forbidden}"

    # It returns ids, and an id is a string.
    assert isinstance(fallback_chain.next_model("nemo-12b-q5"), str)


# ======================================================
# The error envelope
# ======================================================

def test_a_turn_error_carries_what_was_tried(handler):
    handler._turn_models_tried = ["nemo-12b-q5", "mistral-7b-q4km"]

    _run(handler._report_turn_error("fallback_exhausted", 2, "503 unavailable"))

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_error"][0]
    assert told["reason"] == "fallback_exhausted"
    assert told["attempts"] == 2
    assert told["fallbacksTried"] == ["nemo-12b-q5", "mistral-7b-q4km"]
    assert "503" in told["detail"]


def test_a_turn_error_carries_the_same_caveat_as_stop_and_timeout(handler):
    """The facts are identical: Python cannot kill the worker thread,
    no provider takes a cancellation signal, and a Ludo credit is
    spent when the request is made. A failure undoes none of that."""
    _run(handler._report_turn_error("provider_failed", 1, "boom"))

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_error"][0]
    assert WebSocketHandler.ENDING_CAVEAT in told["message"]


def test_a_turn_error_closes_the_bubble(handler):
    """Otherwise the client waits forever for a stream_end that the
    failed turn will never send."""
    handler._turn_stream_id = "req-7"

    _run(handler._report_turn_error("provider_failed", 1, "boom"))

    ends = [m for m in handler.websocket.sent if m.get("type") == "stream_end"]
    assert len(ends) == 1
    assert ends[0]["requestId"] == "req-7"


def test_a_partial_answer_is_said_to_be_partial(handler):
    """Once tokens are on screen the turn cannot start over without
    either duplicating what was read or discarding it. The user is
    told which they are looking at."""
    _run(handler._report_turn_error("provider_failed", 1, "boom", partial=True))

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_error"][0]
    assert told["partial"] is True
    assert "already sent" in told["message"]


# ======================================================
# The loop, in the handler
# ======================================================

class FakeStreamer:
    """A streaming engine that fails on cue, the way the real one does
    -- by SENDING a stream_error packet rather than raising."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def stream(self, request, send_packet):
        model = getattr(request, "model_id", None)
        self.calls.append(model)
        outcome = self.script.pop(0) if self.script else "ok"

        send_packet({"type": "stream_start", "requestId": f"req-{len(self.calls)}",
                     "modelId": model})
        if outcome == "ok":
            send_packet({"type": "stream_token", "requestId": "req-1",
                         "token": "hello"})
            send_packet({"type": "stream_end", "requestId": "req-1"})
        else:
            send_packet({"type": "stream_error", "requestId": "req-1",
                         "message": outcome})


class FakeRequest:
    def __init__(self, model_id):
        self.model_id = model_id
        self.tools = None


def _drive(handler, script, published=None):
    """Run the fallback loop against a scripted engine."""
    handler.streamer = FakeStreamer(script)
    handler._turn_stream_id = "req-1"
    failure: dict = {}
    published = published if published is not None else []
    sent = []

    def send_packet_sync(packet):
        if packet.get("type") == "stream_error":
            failure["message"] = packet.get("message")
            return
        sent.append(packet)

    async def scenario():
        loop = asyncio.get_running_loop()
        await handler._stream_with_fallback(
            FakeRequest("nemo-12b-q5"), send_packet_sync, failure, published,
            loop)

    _run(scenario())
    return handler.streamer, sent


def test_a_transient_provider_failure_is_retried_on_the_same_model(handler,
                                                                   monkeypatch):
    monkeypatch.setattr(retry_policy, "PROVIDER_POLICY",
                        retry_policy.RetryPolicy(max_attempts=2,
                                                 backoff_base=0.01))

    streamer, _ = _drive(handler, ["Read timed out", "ok"])

    assert streamer.calls == ["nemo-12b-q5", "nemo-12b-q5"]


def test_a_hard_provider_failure_falls_back_instead_of_retrying(handler):
    streamer, _ = _drive(handler, ["401 Unauthorized", "ok"])

    assert streamer.calls == ["nemo-12b-q5", "mistral-7b-q4km"]


def test_fallback_walks_the_chain_until_something_works(handler):
    streamer, _ = _drive(handler, ["401 Unauthorized", "403 Forbidden", "ok"])

    assert streamer.calls == ["nemo-12b-q5", "mistral-7b-q4km",
                              "phi-3-mini-4k-instruct-q4"]


def test_an_exhausted_chain_reports_a_turn_error(handler):
    _drive(handler, ["401 Unauthorized"] * 4)

    told = [m for m in handler.websocket.sent if m.get("type") == "turn_error"]
    assert told, [m.get("type") for m in handler.websocket.sent]
    assert told[0]["reason"] == "fallback_exhausted"
    assert len(told[0]["fallbacksTried"]) >= 2


def test_a_fallback_stays_in_one_bubble(handler):
    """Three attempts must not render three replies.

    Asserted on the DECISION, not on the packets: the suppression
    lives in _stream_inference's send_packet_sync closure, and the
    fake one this file drives the loop with does not have it -- so a
    packet-count assertion here would have been testing the fake.
    reuses_open_bubble() is that decision, extracted so a test can
    reach it, exactly as delivery_stopped() was.
    """
    handler._turn_stream_id = "req-1"

    handler._turn_attempt = 1
    assert handler.reuses_open_bubble() is False, "the first attempt opens it"

    handler._turn_attempt = 2
    assert handler.reuses_open_bubble() is True, "the second reuses it"

    handler._turn_attempt = 5
    assert handler.reuses_open_bubble() is True


def test_nothing_is_reused_before_a_bubble_exists(handler):
    """A retry that failed before any stream_start has nothing to
    write into, and must open one rather than address a null id."""
    handler._turn_stream_id = None
    handler._turn_attempt = 3

    assert handler.reuses_open_bubble() is False


def test_the_loop_counts_its_attempts(handler):
    """What reuses_open_bubble reads. If the counter did not advance,
    every attempt would look like the first and open its own bubble."""
    _drive(handler, ["401 Unauthorized", "401 Unauthorized", "ok"])

    assert handler._turn_attempt >= 1
    assert len(handler._turn_models_tried) >= 2


def test_a_failure_after_tokens_are_shown_is_not_retried(handler):
    """The hardest rule. Once the user has read half an answer, a
    retry can only duplicate it or discard it -- both worse than
    saying it failed."""
    streamer, _ = _drive(handler, ["Read timed out", "ok"],
                         published=["half an answer"])

    assert streamer.calls == ["nemo-12b-q5"], "it started over mid-answer"
    told = [m for m in handler.websocket.sent if m.get("type") == "turn_error"][0]
    assert told["partial"] is True


def test_a_stopped_turn_does_not_fall_back(handler):
    """_end_turn owns the turn from the moment it fires. A fallback
    afterwards would stream into a bubble that is already closed."""
    handler._turn_stop = threading.Event()
    handler._turn_stop.set()

    streamer, _ = _drive(handler, ["Read timed out", "ok"])

    assert streamer.calls == ["nemo-12b-q5"]


def test_an_ending_turn_does_not_fall_back(handler):
    handler._turn_ending = "timeout"

    streamer, _ = _drive(handler, ["503 unavailable", "ok"])

    assert streamer.calls == ["nemo-12b-q5"]


def test_a_successful_turn_never_retries(handler):
    streamer, _ = _drive(handler, ["ok"])

    assert streamer.calls == ["nemo-12b-q5"]
    assert not [m for m in handler.websocket.sent if m.get("type") == "turn_error"]


def test_the_client_is_told_the_model_changed(handler):
    """A reply written by a different model than the one announced is
    a lie told in the transcript."""
    _drive(handler, ["401 Unauthorized", "ok"])

    changed = [m for m in handler.websocket.sent
               if m.get("type") == "active_model_changed"]
    assert changed
    assert changed[0]["modelId"] == "mistral-7b-q4km"
    assert changed[0]["reason"] == "fallback"


def test_a_provider_error_is_held_until_the_chain_gives_up(handler):
    """Forwarding the first stream_error would show an error and then
    stream a successful fallback answer underneath it."""
    _, sent = _drive(handler, ["401 Unauthorized", "ok"])

    assert not [p for p in sent if p.get("type") == "stream_error"]


# ======================================================
# It all still fits the turn
# ======================================================

def test_retries_and_fallbacks_fit_inside_the_shortest_budget():
    """Every wait the provider policy can impose, times every model in
    the longest chain, must still leave room for the work itself."""
    from backend.core import turn_budget

    longest = max(len(fallback_chain.chain_for(m))
                  for m in fallback_chain.CHAINS)
    worst_waiting = retry_policy.PROVIDER_POLICY.total_worst_case() * longest

    assert worst_waiting < turn_budget.BUDGETS["chat"] / 2, (
        f"{worst_waiting:.1f}s of backoff against a "
        f"{turn_budget.BUDGETS['chat']}s budget leaves no room to answer")


def test_the_timeout_still_wins(handler, monkeypatch):
    """If the budget runs out mid-retry, the deadline ends the turn --
    retry does not get to outlive it."""
    from backend.core import turn_budget

    monkeypatch.setitem(turn_budget.BUDGETS, "chat", 0.05)
    started = asyncio.Event()

    async def never_ends(_packet):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(handler, "_handle_chat_request", never_ends)

    async def scenario():
        await handler._dispatch({"type": "chat_request", "turnKind": "chat"})
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(handler.wait_for_turns(), timeout=5)

    _run(scenario())

    kinds = [m.get("type") for m in handler.websocket.sent]
    assert "turn_timeout" in kinds
    assert not handler._turn_lock.locked()


def test_tool_jobs_are_bound_to_the_turn_that_owns_them():
    """Without bind_turn a retry has no idea the turn was stopped, and
    backs off into a bubble that is already closed."""
    import inspect

    source = inspect.getsource(WebSocketHandler._serialised_turn)
    assert "bind_turn(" in source
