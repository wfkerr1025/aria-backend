"""Foreign exchange rates, from ExchangeRate-API.

Needs a key for the v6 endpoint, and returns [] without one for the same
reason as the news provider: a missing key is configuration, not a search
miss, and the two should not be indistinguishable in the logs.

The pair has to be extracted before the call, because the endpoint is
per-base-currency: /v6/latest/EUR returns every rate against EUR, and the
target is picked out of that. A question this module cannot resolve to two
currency codes is not an FX question, and it says so by returning [].
"""

from __future__ import annotations

import re
from typing import Any

from ..http_fetch import http_fetch
from .provider_keys import module_key

from logger import get_logger

logger = get_logger(__name__)

PROVENANCE = "exchange-rate-api"
MODULE_NAME = "exchangerate"
ENV_VAR = "EXCHANGERATE_API_KEY"

_ENDPOINT = "https://v6.exchangerate-api.com/v6/{key}/latest/{base}"
_PAGE_URL = "https://www.exchangerate-api.com/"

# ISO 4217 codes common enough to be worth recognising, plus the names
# people use instead. Restricted on purpose: a three-letter token is only
# read as a currency when it is one of these, so "the CEO said" does not
# become a lookup.
_CURRENCIES = {
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "sterling": "GBP",
    "jpy": "JPY", "yen": "JPY",
    "chf": "CHF", "franc": "CHF",
    "cad": "CAD", "aud": "AUD", "nzd": "NZD",
    "cny": "CNY", "yuan": "CNY", "rmb": "CNY",
    "inr": "INR", "rupee": "INR", "rupees": "INR",
    "brl": "BRL", "zar": "ZAR", "sek": "SEK", "nok": "NOK", "mxn": "MXN",
}

_FX_MARKERS = ("exchange rate", "conversion rate", "fx rate", "convert",
               "worth in", "in dollars", "in euros", "in pounds")


def _tokens(query: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", str(query or "").lower()).split()


def api_key() -> str | None:
    """The configured key, or None. Read fresh, never cached."""
    return module_key(MODULE_NAME, ENV_VAR)


def extract_pair(query: str) -> tuple[str, str] | None:
    """The (base, target) this question is about, or None.

    Order is the order they appear: "EUR/USD" and "euros to dollars" both
    mean "how much USD for one EUR". Getting it backwards would return a
    real, precise, inverted number.
    """
    codes = []
    for token in _tokens(query):
        code = _CURRENCIES.get(token)
        if code and code not in codes:
            codes.append(code)
        if len(codes) == 2:
            break
    return (codes[0], codes[1]) if len(codes) == 2 else None


def applies_to(query: str) -> bool:
    text = " ".join(_tokens(query))
    has_marker = any(marker in text for marker in _FX_MARKERS) or "rate" in text
    return has_marker and extract_pair(query) is not None


def lookup(query: str) -> list[dict]:
    """The rate for the pair this question names, or []."""
    if not applies_to(query):
        return []

    pair = extract_pair(query)
    if not pair:
        return []
    base, target = pair

    key = api_key()
    if not key:
        logger.info("exchange_rate_api: no key configured; skipping")
        return []

    try:
        response = http_fetch(_ENDPOINT.format(key=key, base=base))
    except Exception:
        logger.exception("exchange_rate_api: request failed for %s", base)
        return []

    if not isinstance(response, dict) or response.get("status") != "ok":
        return []

    data = response.get("data")
    if not isinstance(data, dict) or data.get("result") == "error":
        logger.info("exchange_rate_api: %s", (data or {}).get("error-type"))
        return []

    rates = data.get("conversion_rates") or data.get("rates")
    if not isinstance(rates, dict):
        return []

    rate = rates.get(target)
    if not isinstance(rate, (int, float)):
        return []

    return [{
        "title": f"{base}/{target} Exchange Rate",
        "snippet": f"1 {base} = {rate} {target}.",
        "url": _PAGE_URL,
        "timestamp": data.get("time_last_update_utc"),
        "provenance": PROVENANCE,
        "rank": 1,
    }]
