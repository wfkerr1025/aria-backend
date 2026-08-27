"""The tool implementations ARIA actually calls out to.

This file has to exist, and the reason is not obvious enough to leave
unwritten.

There are two packages named `tools` in this repository:

    tools/           web_search, get_weather, http_fetch, providers/
    backend/tools/   the Phase 9 planning layer

Without this file, the top-level one is a *namespace* package (PEP 420)
and `backend/tools/` is a regular one. Python's finder does not simply
take the first match on sys.path: a directory with no `__init__.py` is
recorded as a namespace portion and the scan CONTINUES, and any regular
package found later wins outright. So `import tools` resolved to
`backend/tools/` even with the repository root at sys.path[0] -- which is
exactly what backend/ws_server.py puts there.

The effect was that `from tools.web_search import web_search` raised
ModuleNotFoundError inside the running server, every time. The tool call
failed in under a millisecond, the turn reported no evidence, and the user
saw "I could not retrieve current data for this query" -- with no request
ever reaching the network. It looked like a search backend problem for a
long time because, from outside, that is precisely what it looked like.

It never reproduced under pytest, because the test process runs from the
repository root with `backend/` not on sys.path, so `tools` resolved the
way everyone assumed it did.

Making this a regular package settles it: found at sys.path[0], returned
immediately, no scan continues. The `backend.tools.*` imports are absolute
and unaffected.
"""
