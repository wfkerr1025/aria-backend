"""Stock quotes, from Yahoo Finance's public quote endpoint.

No key required. That is the whole reason this endpoint is used rather
than a paid quote API, and it comes with a matching caveat: it is not a
documented, supported interface, and it can start refusing requests
without notice. Every failure path here returns [] rather than raising,
so the day it does, a stock question degrades to "no results" instead of
taking the turn down with it.

Applicability is decided before the network call, not after. "What is the
latest news about AI regulation" is not a quote question, and asking a
finance API is a wasted round trip whose empty answer would then have to
be distinguished from a real miss.
"""

from __future__ import annotations

import re
from typing import Any

from ..http_fetch import http_fetch

from logger import get_logger

logger = get_logger(__name__)

PROVENANCE = "yahoo-finance"
# v8/chart, not v7/quote. v7 now answers 401 "User is unable to access
# this feature" without an authenticated crumb, whatever headers are sent;
# v8/chart still serves the same numbers to an ordinary client and carries
# everything a quote needs in its `meta` block.
_QUOTE_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?range=1d&interval=1d"
)
_PAGE_URL = "https://finance.yahoo.com/quote/{symbol}"

# Words that make a question a quote question. "price" alone is not
# enough -- "what is the price of a Tesla" is shopping, not a ticker.
_FINANCE_MARKERS = (
    "stock", "stocks", "share", "shares", "ticker", "nasdaq", "nyse",
    "market cap", "trading at", "stock price", "share price",
)

# The names people actually type. Deliberately small and explicit: a
# fuzzy company-name-to-ticker guess that lands on the wrong symbol
# returns a real, confident, wrong price, which is worse than no answer.
_KNOWN_TICKERS = {
    "microsoft": "MSFT", "msft": "MSFT",
    "apple": "AAPL", "aapl": "AAPL",
    "google": "GOOGL", "alphabet": "GOOGL", "googl": "GOOGL",
    "amazon": "AMZN", "amzn": "AMZN",
    "tesla": "TSLA", "tsla": "TSLA",
    "meta": "META", "facebook": "META",
    "nvidia": "NVDA", "nvda": "NVDA",
    "netflix": "NFLX", "nflx": "NFLX",
    "ibm": "IBM", "intel": "INTC", "intc": "INTC",
    "amd": "AMD", "oracle": "ORCL", "orcl": "ORCL",
    "salesforce": "CRM", "adobe": "ADBE", "adbe": "ADBE",
}

_EXPLICIT_TICKER = re.compile(r"\b([A-Z]{1,5})\b")


def _words(query: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", str(query or "").lower()).split()


def extract_symbol(query: str) -> str | None:
    """The ticker this question is about, or None.

    A named company wins over a bare uppercase word: "Is AMD stock up?"
    and "Is Advanced Micro Devices up?" should reach the same symbol, and
    an uppercase word in a sentence is far more often an acronym than a
    ticker.
    """
    words = _words(query)
    for word in words:
        if word in _KNOWN_TICKERS:
            return _KNOWN_TICKERS[word]

    # An explicit all-caps token, but only when the question is clearly
    # about a stock -- otherwise "What does API stand for" looks like a
    # ticker lookup.
    if any(marker in " ".join(words) for marker in _FINANCE_MARKERS):
        for candidate in _EXPLICIT_TICKER.findall(str(query or "")):
            if candidate.lower() not in _STOPWORD_ACRONYMS:
                return candidate
    return None


# Uppercase words that are never tickers in this context.
_STOPWORD_ACRONYMS = {"a", "i", "the", "usd", "eur", "gbp", "api", "ai", "ceo", "etf"}


def applies_to(query: str) -> bool:
    """Whether this is a quote question at all."""
    text = " ".join(_words(query))
    if not any(marker in text for marker in _FINANCE_MARKERS):
        return False
    return extract_symbol(query) is not None


def _first_quote(payload: Any) -> dict | None:
    """The quote block, from either response shape.

    v8/chart nests it at chart.result[0].meta. The v7/quote shape is still
    read because it is what the endpoint returns if it ever becomes
    reachable again, and because reading both costs four lines.
    """
    if not isinstance(payload, dict):
        return None

    chart = payload.get("chart")
    if isinstance(chart, dict):
        rows = chart.get("result")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            meta = rows[0].get("meta")
            if isinstance(meta, dict):
                return meta

    result = payload.get("quoteResponse")
    if isinstance(result, dict):
        rows = result.get("result")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    return None


def _price_line(quote: dict) -> str | None:
    """Price, currency and the day's move, using only what is present.

    A quote missing its price is not a quote. Everything else is optional
    and simply omitted -- a "0.00%" printed because a field was absent
    reads as "flat today", which is a claim the payload did not make.
    """
    price = quote.get("regularMarketPrice")
    if price is None:
        return None

    currency = quote.get("currency") or ""
    line = f"Price: {price}{' ' + currency if currency else ''}"

    change = quote.get("regularMarketChange")
    percent = quote.get("regularMarketChangePercent")

    # v8/chart gives the previous close rather than the move, so the move
    # is derived from it. Derived, not guessed: both numbers come from the
    # same payload, and if either is missing nothing is printed.
    previous = quote.get("chartPreviousClose") or quote.get("previousClose")
    if change is None and isinstance(previous, (int, float)) and previous:
        change = price - previous
        percent = (change / previous) * 100

    if change is not None and percent is not None:
        line += f" ({change:+.2f}, {percent:+.2f}%)"
    elif percent is not None:
        line += f" ({percent:+.2f}%)"

    low = quote.get("regularMarketDayLow")
    high = quote.get("regularMarketDayHigh")
    if low is not None and high is not None:
        line += f". Day range {low}-{high}"

    volume = quote.get("regularMarketVolume")
    if volume is not None:
        line += f". Volume {volume:,}"

    cap = quote.get("marketCap")
    if cap:
        # Absent from v8/chart. Printed when a payload does carry it,
        # never synthesised when one does not.
        line += f". Market cap {cap:,}"
    return line + "."


def _timestamp(quote: dict) -> str | None:
    from datetime import datetime, timezone

    epoch = quote.get("regularMarketTime")
    if not isinstance(epoch, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def lookup(query: str) -> list[dict]:
    """A quote for the symbol this question names, or []."""
    symbol = extract_symbol(query) if applies_to(query) else None
    if not symbol:
        return []

    try:
        response = http_fetch(_QUOTE_URL.format(symbol=symbol))
    except Exception:
        logger.exception("yahoo_finance: request failed for %s", symbol)
        return []

    if not isinstance(response, dict) or response.get("status") != "ok":
        logger.info("yahoo_finance: no usable response for %s", symbol)
        return []

    quote = _first_quote(response.get("data"))
    if not quote:
        return []

    snippet = _price_line(quote)
    if not snippet:
        return []

    name = quote.get("shortName") or quote.get("longName") or symbol
    return [{
        "title": f"{name} ({quote.get('symbol') or symbol}) — Stock Quote",
        "snippet": snippet,
        "url": _PAGE_URL.format(symbol=quote.get("symbol") or symbol),
        "timestamp": _timestamp(quote),
        "provenance": PROVENANCE,
        "rank": 1,
    }]
