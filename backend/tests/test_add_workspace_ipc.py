# backend/tests/test_add_workspace_ipc.py
#
# Add Workspace: the button, the packet, and the handler behind it.
#
# The reported symptom was "the button renders, but clicking it sends no
# IPC packet", and the proposed cause was a Router.load() call. That was
# not it -- Router.load survives only in a comment explaining that it was
# already fixed, and the page has always called bridge.send.
#
# The actual cause is that ARIA runs inside Electron, whose renderer does
# not implement window.prompt(). It throws. The click handler asked for a
# directory through it, threw before reaching bridge.send, and the packet
# was never built. From the outside that is indistinguishable from a
# button nobody wired up, which is exactly how it was reported.
#
# So this file asserts both ends: the page does not call the function
# that throws, and the backend does the right thing with the packet it
# now actually sends.

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import ipc_schema
from backend.core import workspace_manager as wm

REPO = Path(__file__).resolve().parents[2]
PAGE_JS = (REPO / "webui/pages/workspaces/workspaces.js").read_text(encoding="utf-8")
PAGE_HTML = (REPO / "webui/pages/workspaces/workspaces.html").read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Source with comments removed -- block, line and HTML.

    Not a line filter. Every source assertion in this codebase that used
    one ended up matching its own explanation of the bug instead of the
    code, because the sentence describing what the file must not do says
    the forbidden thing out loud. That has happened four times now,
    including once in this very file: the comment above the dialog says
    "window.prompt() throws in an Electron renderer", and a line filter
    keeping every line that does not START with a comment marker kept it.
    """
    without_blocks = re.sub(r"/\*[\s\S]*?\*/", "", source)
    without_html = re.sub(r"<!--[\s\S]*?-->", "", without_blocks)
    return re.sub(r"(?m)//.*$", "", without_html)


# ======================================================
# The click can reach bridge.send
# ======================================================
def test_the_page_does_not_call_the_function_that_throws_in_electron():
    code = _code_only(PAGE_JS)

    # window.prompt() is not implemented in an Electron renderer and will
    # not be. Any call to it inside a click handler ends the handler.
    assert "window.prompt(" not in code
    assert "window.confirm(" not in code


def test_the_page_carries_its_own_dialog():
    # Because it cannot borrow the platform's. Both halves have to be
    # present: a dialog object with nothing to render into resolves as
    # cancelled and the button is silent again.
    assert "const dialog" in PAGE_JS
    assert 'id="ws-dialog"' in PAGE_HTML
    assert 'id="ws-dialog-input"' in PAGE_HTML


def test_add_workspace_sends_the_add_packet():
    code = _code_only(PAGE_JS)

    assert "addWorkspace" in code
    assert "IPC.WORKSPACE_ADD_REQUEST" in code
    # Awaited, so the packet is sent after the answer rather than with an
    # unresolved promise as the path.
    assert "await dialog.text(" in code


def test_the_add_button_is_bound_to_it():
    code = _code_only(PAGE_JS)
    assert 'getElementById("ws-add")' in code
    assert "addWorkspace()" in code


def test_the_path_is_sent_as_typed():
    code = _code_only(PAGE_JS)
    # Validation belongs to the backend, which knows what the file tools
    # will accept. A check in the page could pass while the real one
    # fails, which is a worse failure than no check at all.
    assert "bridge.send(IPC.WORKSPACE_ADD_REQUEST, { path })" in code


def test_cancelling_is_distinguished_from_clearing():
    code = _code_only(PAGE_JS)
    # null is cancelled and sends nothing; "" is a cleared field and is
    # the backend's to refuse. Collapsing them would make Cancel add a
    # workspace at the empty path.
    assert "if (path === null) return;" in code


# ======================================================
# Both ends name the same packet
# ======================================================
def test_the_packet_exists_on_both_sides():
    ui_schema = (REPO / "webui/core/ipc_schema.js").read_text(encoding="utf-8")

    assert ipc_schema.WORKSPACE_ADD_REQUEST == "workspace_add_request"
    assert '"workspace_add_request"' in ui_schema
    assert "WORKSPACE_ADD_REQUEST" in ui_schema


# ======================================================
# The backend does something real with it
# ======================================================
@pytest.fixture
def registry(tmp_path, monkeypatch):
    from backend.core import file_tools

    monkeypatch.setenv(file_tools.ENV_WORKSPACE, str(tmp_path))
    wm.reset_registry()
    wm.ensure_default_workspace()
    yield tmp_path
    wm.reset_registry()


def test_adding_a_directory_registers_it(registry, tmp_path):
    other = tmp_path / "second_project"
    other.mkdir()

    before = len(wm.get_workspace_list())
    wm.add_workspace(str(other))

    listed = wm.get_workspace_list()
    assert len(listed) == before + 1
    assert any(Path(w["root_path"]) == other for w in listed)


def test_the_answer_is_the_list_it_produced(registry, tmp_path):
    # A mutation answers with the state it caused, so the page never has
    # to ask twice for the result of its own click -- and never renders a
    # list from before the change it just made.
    other = tmp_path / "third"
    other.mkdir()
    wm.add_workspace(str(other))

    assert any(Path(w["root_path"]) == other for w in wm.get_workspace_list())


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_path_is_refused(registry, bad):
    with pytest.raises(wm.WorkspaceError):
        wm.add_workspace(bad)


def test_a_directory_that_does_not_exist_is_refused(registry, tmp_path):
    with pytest.raises(wm.WorkspaceError):
        wm.add_workspace(str(tmp_path / "no_such_place"))


def test_a_file_is_refused(registry, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(wm.WorkspaceError):
        wm.add_workspace(str(target))


def test_a_refused_add_leaves_the_registry_alone(registry, tmp_path):
    before = wm.get_workspace_list()

    with pytest.raises(wm.WorkspaceError):
        wm.add_workspace(str(tmp_path / "nope"))

    assert wm.get_workspace_list() == before


def test_each_workspace_stages_inside_itself(registry, tmp_path):
    # The rule that makes several projects safe, restated here because
    # adding one is where it would be broken: staging lives inside the
    # project it stages for, so one project's staged files are not
    # reachable from another's root.
    other = tmp_path / "isolated"
    other.mkdir()
    wm.add_workspace(str(other))

    entry = next(w for w in wm.get_workspace_list() if Path(w["root_path"]) == other)
    assert Path(entry["ghost_directory_path"]).is_relative_to(other)
