# backend/tests/test_remove_overlay.py
#
# The working-directory overlay: what is left of it, and what must not
# come back.
#
# It began as an expanding panel carrying every workspace field, because
# it was the only place any of them lived. The Control Center
# (pages/workspaces) owns all of that now -- the roots, the staged files,
# the diffs, commit, discard, rollback -- so what survives here is one
# line and a way into that page.
#
# The file is deliberately not deleted. The line has to show live counts,
# which means subscribing to a packet, which means a module; inlining the
# markup in index.html would give a status bar that shows whatever was
# true when the page loaded and never updates. What was removed is the
# overlay BEHAVIOUR: the expansion, the duplicated fields, the buttons.
#
# Two places showing the same staged count is one place too many, and the
# one that goes stale is the one nobody is looking at.

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPONENT_JS = (REPO / "webui/components/working_directory/working_directory.js").read_text(encoding="utf-8")
COMPONENT_CSS = (REPO / "webui/components/working_directory/working_directory.css").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "webui/index.html").read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Source with block, line and HTML comments removed."""
    without_blocks = re.sub(r"/\*[\s\S]*?\*/", "", source)
    without_html = re.sub(r"<!--[\s\S]*?-->", "", without_blocks)
    return re.sub(r"(?m)//.*$", "", without_html)


CODE = _code_only(COMPONENT_JS)


# ======================================================
# The overlay behaviour is gone
# ======================================================
def test_it_never_expands():
    # It used to expand in place, over the composer's Send button at
    # 1280x720. A persistent panel that hides working controls is worse
    # than no panel.
    assert "expanded" not in CODE
    assert ":not(.wd-collapsed)" not in COMPONENT_CSS


def test_it_has_no_buttons_of_its_own():
    # Commit, discard and rollback live on the Control Center, where the
    # diff they act on is visible. A button that applies a change you
    # cannot see is the wrong affordance in the wrong place.
    for gone in ("wd-commit", "wd-discard", "wd-rollback", "wd-set-root"):
        assert gone not in CODE


def test_it_renders_exactly_one_row():
    assert 'this.el.className = "wd-collapsed"' in CODE
    # The row classes still have layout rules attached, but nothing
    # renders into them any more.
    assert "wd-row" not in CODE


# ======================================================
# What replaced it
# ======================================================
def test_the_line_says_all_three_counts():
    # "Workspaces: N · Staged: M · Tool-capable: Yes/No", always all
    # three. The earlier version dropped "0 staged" and dropped the
    # capability field when it was fine, which read as tidier and was
    # worse: a line whose fields come and go has to be re-read to be
    # understood, and the reason to glance at it is to confirm that
    # nothing changed.
    assert "Workspaces: ${workspaces}" in CODE
    assert "Staged: ${staged}" in CODE
    assert "Tool-capable: ${capable}" in CODE


def test_the_counts_come_from_the_packet():
    assert "s.workspace_count" in CODE
    assert "s.staged_count" in CODE
    assert "s.tool_capable" in CODE


def test_the_backend_supplies_the_workspace_count():
    from backend.core import workspace_manager

    # Counted where the registry is, not in the UI. The Control Center
    # holds the list and is usually closed, so a status line that waits
    # for another packet shows a wrong number until one arrives.
    assert "workspace_count" in workspace_manager.describe_workspace()


def test_it_computes_no_path_of_its_own():
    # Every value comes from the packet. A path derived here would be a
    # second opinion able to disagree with the boundary the file tools
    # actually enforce.
    assert "aria_staging" not in CODE


def test_clicking_it_opens_the_control_center():
    assert 'Router.navigate("settings/workspaces")' in CODE
    # Checked against the router's real API rather than a remembered
    # name: an earlier version asserted Router.load, which does not
    # exist, so the test agreed with the bug instead of finding it.
    router_js = (REPO / "webui/core/router.js").read_text(encoding="utf-8")
    assert "async navigate(" in router_js


def test_it_is_reachable_without_a_mouse():
    assert 'tabindex="0"' in CODE
    assert "keydown" in CODE


def test_the_project_name_is_escaped_before_it_reaches_innerhtml():
    # It comes off a filesystem and this builds its DOM with innerHTML.
    assert "function escapeHtml" in COMPONENT_JS
    assert "escapeHtml(this.projectName())" in CODE
    assert "escapeHtml(this.summary())" in CODE


# ======================================================
# It is still actually mounted
# ======================================================
def test_index_html_still_loads_it():
    # A component that exists in a file, is never linked and is never
    # mounted looks exactly like a finished feature from the outside, and
    # its tests pass.
    assert 'id="working-directory-panel"' in INDEX_HTML
    assert "components/working_directory/working_directory.css" in INDEX_HTML
    assert "components/working_directory/working_directory.js" in INDEX_HTML


def test_it_sits_clear_of_the_sidebar():
    assert re.search(r"#working-directory-panel\s*\{[^}]*right:", COMPONENT_CSS)
    assert not re.search(r"#working-directory-panel\s*\{[^}]*[^-]left:", COMPONENT_CSS)
