"""Search providers for the web_search tool.

DuckDuckGo's Instant Answer API was the only backend, and it cannot answer
the questions this assistant is most often asked. Measured against it:

    stock price of Microsoft        -> {"Abstract":"","AbstractSource":"",...}
    latest news about AI regulation -> {"Abstract":"","AbstractSource":"",...}
    current EUR/USD exchange rate   -> {"Abstract":"","AbstractSource":"",...}

Every one an empty envelope. The tool call fired, the plan was right, the
evidence pipeline worked -- and there was nothing in the response, because
that API carries encyclopaedia abstracts, not quotes, headlines or rates.

Each module here answers one kind of question and returns [] for every
other kind, so a query reaches only the provider that can serve it. A
provider that has no key, cannot parse the query, or fails outright also
returns [] -- never a guess, and never a partial result dressed up as a
whole one. When all of them return [], that is a hard miss and the tool
says so rather than inventing a summary.

The contract is one function:

    lookup(query: str) -> list[dict]

with each dict carrying title / snippet / url / timestamp / provenance /
rank. Nothing here formats prose for a model; that stays with the
normalization layer, which already knows how.
"""

from __future__ import annotations

from . import (
    exchange_rate_api,
    news_mediastack,
    news_nytimes,
    web_langsearch,
    yahoo_finance,
)

__all__ = [
    "NEWS_PROVIDERS",
    "PRIMARY_PROVIDERS",
    "FALLBACK_PROVIDERS",
    "PROVIDERS",
    "exchange_rate_api",
    "news_mediastack",
    "news_nytimes",
    "web_langsearch",
    "yahoo_finance",
]

# The news providers, in the order their results are kept.
#
# Ordering is not cosmetic here: deduplication keeps the first occurrence
# of a story, so this decides which version of a shared story survives.
# The Times leads because a named publisher's own abstract is better
# evidence than a headline with no body text.
#
# Both need a key, so with neither configured this tier is empty and
# every news question falls through to LangSearch. That is the intended
# shape, not a gap: the fallback exists precisely so a news question
# does not fail for want of a key.
#
# GDELT is gone. It was the keyless floor and the argument for it was
# good -- always on, no account, no ceiling -- but api.gdeltproject.org
# would not answer, and at the shared 10s request timeout it consumed
# web_search's entire tool budget and the turn gave up 2.1 seconds
# before LangSearch returned ten good results. Shortening its timeout
# fixed the collision; it still bought nothing but three seconds of
# silence on every news query. A provider that cannot be reached is not
# a floor.
#
# NewsAPI is gone too. Its free tier is development-only: localhost
# origins, 100 requests a day, and no production use permitted. A
# provider that cannot legally run in the product is not a provider.
NEWS_PROVIDERS = (news_nytimes, news_mediastack)

# The specialists. Order is the order results arrive in, and therefore
# their rank: a quote is a more direct answer to "what is X trading at"
# than an article about it, so finance leads; news is context; a rate is
# its own question.
PRIMARY_PROVIDERS = (yahoo_finance, *NEWS_PROVIDERS, exchange_rate_api)

# The general-web tier, reached only when the specialists found nothing.
#
# Brave and Tavily are gone: two paid free-tiers, two keys to hold, and
# two response shapes to parse for one job that LangSearch does free
# across the whole web. SerpAPI was considered and never built.
#
# Being a second tier rather than another entry in PRIMARY_PROVIDERS is
# what makes the news fallback work. A news question the Times answered
# does not also get a page of search results -- a named publisher's
# abstract with a date is better evidence than a link about the same
# story, and carrying both spends the answer's budget twice on one fact.
# A news question nobody answered, because no key is configured, now
# reaches the web instead of failing.
FALLBACK_PROVIDERS = (web_langsearch,)

# Every provider, in preference order. The tiering lives in web_search's
# dispatcher; this stays the flat registry that callers enumerating "what
# backends exist" expect.
PROVIDERS = (*PRIMARY_PROVIDERS, *FALLBACK_PROVIDERS)
