"""Money leaves through one function, so the rules live at that door.

WHY THIS SUITE IS DIFFERENT FROM test_ludo_actions
--------------------------------------------------
That suite asks whether a verb does the right thing. This one asks
what it COST, which is the question with no undo behind it. Ludo
charges at request time: a cancel, a timeout, a crash, a retry and a
user changing their mind all cost exactly as much as a success, and
there is no refund and no balance endpoint to check first.

So every test here is about a call that should NOT have been made, or
about being honest when one was. The one that matters most is the
pair at the bottom: a request refused by a ceiling must report that
nothing was spent, and a request whose connection broke must report
that something may have been -- because guessing the cheap way round
is how an account empties.

NOTHING HERE TOUCHES THE REAL API. It would cost real money.
"""

from __future__ import annotations

import json
import time

import pytest

from backend.ludo import ludo_actions as actions
from backend.ludo import ludo_client as client
from backend.ludo import ludo_spend as spend


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = self.text.encode() if self.text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def a_key(monkeypatch):
    monkeypatch.setenv(client.ENV_KEY, "test-key-not-a-real-one")


@pytest.fixture
def posts(monkeypatch):
    """Every POST, recorded. The list length IS the bill."""
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "body": json or {}})
        if "/assets/3d-model" in url:
            return FakeResponse(200, {"model_url": "https://cdn.ludo.ai/a/m.glb"})
        return FakeResponse(200, {"url": "https://cdn.ludo.ai/a/img.png"})

    monkeypatch.setattr(client.requests, "post", fake_post)
    return sent


# ======================================================
# Reuse: the same ask, twice
# ======================================================

def test_the_same_request_is_not_bought_twice(posts):
    actions.generate_image("a mossy barrel")
    actions.generate_image("a mossy barrel")

    assert len(posts) == 1, "it paid for the same picture twice"


def test_the_second_answer_says_it_was_reused(posts):
    actions.generate_image("a mossy barrel")
    again = actions.generate_image("a mossy barrel")

    assert again["success"] is True
    assert again["cost"] == {"calls": 0, "reused": 1}


def test_the_first_answer_says_what_it_cost(posts):
    first = actions.generate_image("a mossy barrel")

    assert first["cost"] == {"calls": 1, "reused": 0}


def test_a_reused_answer_still_carries_the_asset(posts):
    first = actions.generate_image("a mossy barrel")
    again = actions.generate_image("a mossy barrel")

    assert again["url"] == first["url"]


def test_a_different_request_is_a_different_purchase(posts):
    actions.generate_image("a mossy barrel")
    actions.generate_image("a rusty barrel")

    assert len(posts) == 2


def test_asking_for_a_fresh_one_buys_a_fresh_one(posts):
    client.call("image", {"prompt": "a barrel"})
    client.call("image", {"prompt": "a barrel"}, fresh=True)

    assert len(posts) == 2, "fresh=True must not be served from the cache"


def test_a_stale_result_is_not_handed_over(posts, monkeypatch):
    """An asset URL from Ludo does not live forever. A reuse hit that
    returns a dead link, and reports it as free, is worse than a miss."""
    actions.generate_image("a mossy barrel")
    monkeypatch.setattr(spend, "REUSE_MAX_AGE_SECONDS", -1)

    actions.generate_image("a mossy barrel")
    assert len(posts) == 2


def test_reuse_can_be_switched_off(posts, monkeypatch):
    monkeypatch.setenv(spend.ENV_REUSE, "0")

    actions.generate_image("a mossy barrel")
    actions.generate_image("a mossy barrel")

    assert len(posts) == 2


# ======================================================
# Idempotency: Ludo's own protection, which never used to fire
# ======================================================

def test_the_same_ask_sends_the_same_request_id(posts):
    """request_id is how Ludo recognises a repeat and declines to
    charge for it. It used to be a fresh uuid4 on every call, so that
    mechanism could not once have fired -- every retry was a purchase.
    """
    first = spend.request_key("image", {"prompt": "a barrel"})
    second = spend.request_key("image", {"prompt": "a barrel"})

    assert first == second


def test_the_request_id_sent_is_the_one_derived(posts):
    client.call("image", {"prompt": "a barrel"})

    assert posts[0]["body"]["request_id"] == spend.request_key(
        "image", {"prompt": "a barrel"})


def test_a_different_ask_sends_a_different_id():
    assert spend.request_key("image", {"prompt": "a"}) != \
        spend.request_key("image", {"prompt": "b"})


def test_the_same_ask_written_in_a_different_order_is_the_same_ask():
    assert spend.request_key("image", {"prompt": "a", "art_style": "s"}) == \
        spend.request_key("image", {"art_style": "s", "prompt": "a"})


def test_the_same_ask_to_a_different_endpoint_is_not_the_same_ask():
    assert spend.request_key("image", {"prompt": "a"}) != \
        spend.request_key("video", {"prompt": "a"})


def test_a_fresh_call_does_not_send_the_derived_id(posts):
    client.call("image", {"prompt": "a barrel"}, fresh=True)

    assert posts[0]["body"]["request_id"] != spend.request_key(
        "image", {"prompt": "a barrel"})


# ======================================================
# Ceilings
# ======================================================

def test_a_run_stops_at_its_ceiling(posts, monkeypatch):
    monkeypatch.setenv(spend.ENV_RUN_CALLS, "3")

    for index in range(3):
        client.call("image", {"prompt": f"barrel {index}"}, run_id="job-1")

    with pytest.raises(client.LudoUnavailable):
        client.call("image", {"prompt": "barrel 4"}, run_id="job-1")

    assert len(posts) == 3, "the fourth was sent anyway"


def test_a_refusal_reaches_the_user_as_nothing_spent(posts, monkeypatch):
    """LudoUnavailable, not LudoError. The actions layer turns the first
    into ran=False and the second into "credits MAY have gone", and a
    ceiling is the one case where we know for certain they did not --
    the request was never sent."""
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "1")
    actions.generate_image("the one that fits")

    refused = actions.generate_image("the one over the line")

    assert refused["success"] is False
    assert refused["ran"] is False, "it implied credits may have gone"
    assert refused["cost"] == {"calls": 0, "reused": 0}
    assert len(posts) == 1


def test_one_run_s_ceiling_is_not_another_s(posts, monkeypatch):
    monkeypatch.setenv(spend.ENV_RUN_CALLS, "1")

    client.call("image", {"prompt": "a"}, run_id="job-1")
    client.call("image", {"prompt": "b"}, run_id="job-2")

    assert len(posts) == 2


def test_the_day_has_a_ceiling_too(posts, monkeypatch):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "2")

    client.call("image", {"prompt": "a"})
    client.call("image", {"prompt": "b"})
    with pytest.raises(client.LudoUnavailable):
        client.call("image", {"prompt": "c"})

    assert len(posts) == 2


def test_yesterday_does_not_count_against_today(posts, monkeypatch):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "1")
    spend.record("image", key="old", outcome="spent", calls=5)

    ledger = spend.ledger_path()
    stale = [json.loads(line) for line in
             ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    stale[0]["ts"] = time.time() - 90000
    ledger.write_text("\n".join(json.dumps(e) for e in stale) + "\n",
                      encoding="utf-8")

    client.call("image", {"prompt": "today"})
    assert len(posts) == 1


def test_a_ceiling_of_zero_spends_nothing_at_all(posts, monkeypatch):
    """The off switch. Somebody setting this to 0 means "do not spend
    anything" -- reading that as "no limit", which is what a plain
    truthiness check does, would be the worst possible way to be wrong
    on a guard whose whole job is money.
    """
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "0")

    refused = actions.generate_image("a barrel")

    assert refused["success"] is False
    assert refused["ran"] is False
    assert len(posts) == 0


def test_the_refusal_counts_in_english(monkeypatch):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "0")

    with pytest.raises(spend.BudgetExceeded) as raised:
        spend.check("image")

    assert "1 paid Ludo call " in str(raised.value)
    assert "calls" not in str(raised.value)


def test_a_negative_ceiling_is_no_ceiling(posts, monkeypatch):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "-1")

    for index in range(4):
        client.call("image", {"prompt": f"barrel {index}"})

    assert len(posts) == 4


def test_a_nonsense_ceiling_does_not_become_a_ceiling_of_zero(monkeypatch):
    """A typo in a shell variable must not be why nothing works."""
    monkeypatch.setenv(spend.ENV_RUN_CALLS, "lots")

    assert spend.run_cap() == spend.RUN_CALLS


# ======================================================
# What is free stays free
# ======================================================

def test_polling_a_job_is_never_billed():
    assert spend.is_billable("job") is False
    assert spend.is_billable("validate") is False
    assert spend.estimate(["job", "validate"])["calls"] == 0


def test_every_endpoint_is_classified():
    """An endpoint in neither set is one this module forgot to count,
    which means a ceiling it does not apply to."""
    unclassified = [name for name in client.ENDPOINTS
                    if name not in spend.BILLABLE and name not in spend.FREE]

    assert unclassified == []


def test_a_free_call_is_not_stopped_by_a_full_ceiling(monkeypatch):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "1")
    spend.record("image", key="k", outcome="spent", calls=9)

    spend.check("job")  # must not raise


# ======================================================
# Honesty when it is not certain
# ======================================================

def test_a_broken_connection_is_recorded_as_maybe_spent(monkeypatch):
    """The POST may have arrived and been charged before the socket
    died. Assuming it was free is the assumption that empties an
    account, so it counts against the ceiling."""
    def die(*args, **kwargs):
        raise client.requests.RequestException("connection reset")

    monkeypatch.setattr(client.requests, "post", die)

    with pytest.raises(client.LudoError):
        client.call("image", {"prompt": "a barrel"})

    assert spend.spent() == 1


def test_a_refused_request_is_recorded_as_maybe_spent(monkeypatch):
    def refuse(*args, **kwargs):
        return FakeResponse(500, {"message": "upstream on fire"})

    monkeypatch.setattr(client.requests, "post", refuse)

    with pytest.raises(client.LudoError):
        client.call("image", {"prompt": "a barrel"})

    assert spend.spent() == 1


def test_a_failure_is_not_cached_as_a_result(monkeypatch, posts):
    """Reuse must never hand back a failure as though it were an
    asset."""
    def refuse(*args, **kwargs):
        return FakeResponse(500, {"message": "upstream on fire"})

    monkeypatch.setattr(client.requests, "post", refuse)
    with pytest.raises(client.LudoError):
        client.call("image", {"prompt": "a barrel"})

    assert spend.lookup(spend.request_key("image", {"prompt": "a barrel"})) is None


def test_a_ceiling_refusal_costs_nothing(monkeypatch, posts):
    monkeypatch.setenv(spend.ENV_DAILY_CALLS, "1")
    client.call("image", {"prompt": "a"})

    with pytest.raises(client.LudoUnavailable):
        client.call("image", {"prompt": "b"})

    assert spend.spent() == 1, "a refusal was counted as a purchase"


# ======================================================
# The concept you already paid for
# ======================================================

def test_a_model_costs_two_calls(posts):
    answer = actions.generate_model("a barrel")

    assert len(posts) == 2
    assert answer["cost"]["calls"] == 2
    assert answer["steps"] == ["image", "model_3d"]


def test_a_supplied_concept_halves_the_price(posts):
    """The whole point of showing concept art before building: without
    this, approving a picture would generate a SECOND, different one to
    convert -- paying twice and converting the one nobody approved."""
    answer = actions.generate_model(
        "a barrel", image="https://cdn.ludo.ai/a/approved.png")

    assert len(posts) == 1
    assert answer["cost"]["calls"] == 1
    assert answer["steps"] == ["model_3d"]


def test_the_supplied_concept_is_the_one_converted(posts):
    actions.generate_model("a barrel",
                           image="https://cdn.ludo.ai/a/approved.png")

    assert posts[0]["body"]["image"] == "https://cdn.ludo.ai/a/approved.png"


def test_the_concept_it_bought_comes_back_to_be_reused(posts):
    """generate_model returns concept_url precisely so the next call
    does not have to buy one."""
    first = actions.generate_model("a barrel")

    assert first["concept_url"] == "https://cdn.ludo.ai/a/img.png"
    assert first["steps"] == ["image", "model_3d"]


def test_converting_the_same_concept_twice_is_one_purchase(posts):
    """The prompt is not an input to the conversion -- /assets/3d-model
    is given an image, a face count and texture settings, and nothing
    else. So the same picture converted the same way is the same
    purchase however differently the request was worded, and this
    catches a repeat no human would notice they were paying for.
    """
    first = actions.generate_model("a barrel")
    again = actions.generate_model("something else entirely",
                                   image=first["concept_url"])

    assert len(posts) == 2, "it bought the same conversion twice"
    assert again["cost"] == {"calls": 0, "reused": 1}


def test_changing_the_conversion_does_buy_again(posts):
    """Face count IS an input, so a different one is a different model
    and has to be paid for."""
    first = actions.generate_model("a barrel")
    lower = actions.generate_model("a barrel", image=first["concept_url"],
                                   polycount=4000)

    assert lower["cost"]["calls"] == 1
    assert posts[-1]["body"]["target_num_faces"] == 4000


def test_a_model_with_a_concept_still_needs_something_to_make(posts):
    answer = actions.generate_model("")

    assert answer["success"] is False
    assert answer["ran"] is False
    assert len(posts) == 0


# ======================================================
# Estimating, which is the only refund on offer
# ======================================================

def test_a_plan_can_be_priced_before_it_runs():
    assert spend.estimate(["image", "model_3d"])["calls"] == 2
    assert spend.estimate(["image"] * 3)["calls"] == 3


def test_an_estimate_separates_what_is_free():
    estimate = spend.estimate(["image", "job", "model_3d", "validate"])

    assert estimate["calls"] == 2
    assert estimate["free"] == ["job", "validate"]


# ======================================================
# The ledger
# ======================================================

def test_the_ledger_records_what_was_bought(posts):
    actions.generate_image("a mossy barrel")

    entries = spend.history()
    assert len(entries) == 1
    assert entries[0]["endpoint"] == "image"
    assert entries[0]["outcome"] == "spent"
    assert entries[0]["url"] == "https://cdn.ludo.ai/a/img.png"


def test_the_ledger_records_a_reuse_as_costing_nothing(posts):
    actions.generate_image("a mossy barrel")
    actions.generate_image("a mossy barrel")

    outcomes = [entry["outcome"] for entry in spend.history()]
    assert outcomes == ["spent", "reused"]
    assert spend.spent() == 1


def test_a_torn_line_does_not_make_the_history_unreadable(posts):
    """One interrupted write must not take the reuse cache and every
    estimate with it."""
    spend.record("image", key="a", outcome="spent", calls=1)
    with open(spend.ledger_path(), "a", encoding="utf-8") as handle:
        handle.write('{"ts": 2, "endpoint": "im')
    spend.record("image", key="b", outcome="spent", calls=1)

    assert [entry["key"] for entry in spend.history()] == ["a", "b"]


def test_a_ledger_that_cannot_be_written_never_breaks_a_turn(monkeypatch):
    """The money is already gone by the time this is called. Losing the
    record of it must not also lose the answer."""
    monkeypatch.setenv(spend.ENV_LEDGER, str(spend.ledger_path().parent))

    spend.record("image", key="a", outcome="spent", calls=1)  # must not raise


def test_spending_is_grouped_by_run(posts):
    client.call("image", {"prompt": "a"}, run_id="job-1")
    client.call("image", {"prompt": "b"}, run_id="job-2")

    assert spend.spent(run_id="job-1") == 1
    assert spend.spent(run_id="job-2") == 1
    assert spend.spent() == 2
