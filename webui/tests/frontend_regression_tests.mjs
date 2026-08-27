// webui/tests/frontend_regression_tests.mjs
//
// Regression tests for the chat panel navigation/session/resend bugs
// fixed in this pass:
//   - Router.navigate() (webui/core/router.js) replaces #panel-container's
//     entire innerHTML every time the user navigates to the chat panel —
//     including navigating BACK to it — which used to leave Chat's cached
//     DOM references (this.history/this.input/this.sendBtn/...) pointing
//     at detached nodes, because init() only ever ran cache()+bindUI()
//     once. That's what looked like "chat breaks after navigating" and
//     "New Chat does nothing": conversationId/history were fine in
//     memory the whole time, but nothing was wired to the new DOM.
//   - startNewChat() / the Chat Menu now track multiple sessions so
//     switching between chats actually works.
//   - _resendLastMessage() re-sends the message that triggered a
//     safety_warning without duplicating the bubble/history entry.
//
// Self-contained, plain-assert, no test framework or npm dependency —
// same convention as backend/tests/skr_and_ipc_tests.py. Run directly:
//
//   node webui/tests/frontend_regression_tests.mjs
//
// The Electron-level pieces (the separate warning BrowserWindow, IPC
// wiring in main.js/preload.js, and webui/core/app.js's dispatcher —
// which needs Toast/Topbar/Sidebar/Router/Theme and a real DOM to
// import cleanly) were verified separately via a live browser session
// (see the session's summary), not here — a minimal fake DOM good
// enough for chat.js's own logic is not enough to import app.js.

import assert from "node:assert/strict";

// ============================================================
// Minimal fake DOM — just enough surface for chat.js's cache()/
// bindUI()/render methods. Not a general-purpose DOM shim.
// ============================================================
function makeElement(tag) {
  const el = {
    tagName: tag,
    _children: [],
    _classes: new Set(),
    _listeners: {},
    _innerHTML: "",
    textContent: "",
    value: "",
    style: {},
    scrollTop: 0,
    scrollHeight: 0,
    get innerHTML() {
      return el._innerHTML;
    },
    set innerHTML(v) {
      el._innerHTML = v;
      el._children = [];
    },
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    classList: {
      add: (...cls) => cls.forEach((c) => el._classes.add(c)),
      remove: (...cls) => cls.forEach((c) => el._classes.delete(c)),
      toggle: (c, force) => {
        const has = el._classes.has(c);
        const want = force === undefined ? !has : force;
        want ? el._classes.add(c) : el._classes.delete(c);
        return want;
      },
      contains: (c) => el._classes.has(c),
    },
    appendChild(child) {
      el._children.push(child);
      child.parentEl = el;
      return child;
    },
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    dispatch(evt, arg) {
      (el._listeners[evt] || []).forEach((cb) => cb(arg));
    },
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  return el;
}

const registry = {};

function resetChatDom() {
  // Simulates router.js's `container.innerHTML = html` — brand new
  // element objects for every id chat.js's cache() looks up, same as a
  // fresh fetch of chat.html actually produces.
  for (const id of [
    "chat-history", "chat-input", "chat-send-btn", "typing-indicator",
    "chat-menu-btn", "chat-menu-dropdown",
  ]) {
    registry[id] = makeElement("div");
  }
}
resetChatDom();

global.document = {
  getElementById: (id) => registry[id] || null,
  createElement: (tag) => makeElement(tag),
};
global.window = global;
global.DOMPurify = { sanitize: (s) => s };
global.marked = { parse: (s) => s };
global.fetch = () => Promise.resolve({ catch: () => {} });

let sentPackets = [];
global.window.aria = {
  sendToBackend: (packet) => sentPackets.push(packet),
  log: () => {},
};

// Direct chat navigation (webui/components/chat/chat.js's
// startNewChat()/switchToSession()) checks window.Router.currentPanel to
// decide whether it's safe to touch the DOM directly or whether it needs
// to navigate to the Chat panel first — see _isChatPanelVisible(). Every
// test in this file exercises chat.js as if the user were already
// looking at Chat (matching what these tests were originally written to
// assume), so that's the default here; the dedicated
// "clicking a chat from another page" tests below override it.
global.window.Router = { currentPanel: "chat" };
const dispatchedEvents = [];
global.window.dispatchEvent = (evt) => dispatchedEvents.push(evt);

// chat.js now binds its own "backend-packet" listener directly (dual-
// context UI's "Using: <model> (<Local/Cloud>)" header — see
// _bindUsingHeaderListener()), the same established pattern
// webui/components/statusbar/statusbar.js already uses. A real window
// always has addEventListener; this minimal fake DOM didn't need one
// until now — a plain recorder is enough for these tests, which never
// dispatch a real "backend-packet" CustomEvent themselves.
const windowListeners = {};
global.window.addEventListener = (evt, cb) => {
  (windowListeners[evt] ||= []).push(cb);
};
global.window.removeEventListener = () => {};

const { default: Chat } = await import("../components/chat/chat.js");

function freshChat() {
  // Chat is a singleton module export — reset its mutable state between
  // tests the same way a real app run only ever does once (there's no
  // re-import), so each test starts from a clean, un-initialized Chat.
  Chat._ready = false;
  Chat._pollTimer = null;
  Chat._activeStream = null;
  Chat._conversationHistory = [];
  Chat._conversationId = null;
  Chat._multiTurn = true;
  Chat._sessions = [];
  Chat._menuOpen = false;
  sentPackets = [];
  dispatchedEvents.length = 0;
  global.window.Router = { currentPanel: "chat" };
  resetChatDom();
  return Chat;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

// ============================================================
test("first init creates exactly one session and binds the DOM", () => {
  const chat = freshChat();
  chat.init();

  assert.equal(chat._ready, true);
  assert.equal(chat._sessions.length, 1);
  assert.equal(chat._sessions[0].id, chat._conversationId);
  assert.equal(chat.history, registry["chat-history"]);
});

test("navigating away and back re-binds to the NEW DOM and repaints history", () => {
  const chat = freshChat();
  chat.init();

  chat.input.value = "hello before navigation";
  chat.sendMessage();
  assert.equal(chat._conversationHistory.length, 1);
  assert.equal(registry["chat-history"]._children.length, 1);

  const oldHistoryEl = chat.history;
  const oldInputEl = chat.input;

  // Simulate router.js's innerHTML replacement on navigating back to chat.
  resetChatDom();
  assert.notEqual(registry["chat-history"], oldHistoryEl);

  // Router re-invokes module.default.init() every time — this must NOT
  // be a no-op just because _ready is already true (that was the bug).
  chat.init();

  assert.notEqual(chat.history, oldHistoryEl, "history ref must repoint to the new DOM node");
  assert.notEqual(chat.input, oldInputEl, "input ref must repoint to the new DOM node");
  assert.equal(chat.history._children.length, 1, "the prior message must be repainted into the new DOM");
  assert.equal(chat._conversationHistory.length, 1, "in-memory history must survive navigation");
  assert.equal(chat._conversationId, chat._sessions[0].id, "conversationId must survive navigation");
});

test("startNewChat() works after navigation and starts a fresh, empty session", () => {
  // The chat toolbar's own "New Chat" button was removed (the sidebar's
  // is now the only entry point — see webui/components/sidebar/sidebar.js
  // and webui/components/chat/chat.html), so this exercises
  // startNewChat() directly, the same call the sidebar button makes via
  // window.chatPanel.startNewChat(). What's actually under test — that
  // starting a new chat correctly re-binds to fresh DOM after a
  // navigation away and back — is unaffected by which button triggers it.
  const chat = freshChat();
  chat.init();

  chat.input.value = "first session message";
  chat.sendMessage();

  resetChatDom();
  chat.init(); // re-bind, as router.js does on every navigation back to chat

  chat.startNewChat();

  assert.equal(chat._sessions.length, 2);
  assert.equal(chat._conversationHistory.length, 0);
  assert.equal(chat.history._children.length, 0);
  assert.notEqual(chat._conversationId, chat._sessions[0].id);
});

test("sidebar chat list receives session updates and switching repaints without duplicating", () => {
  // The session list moved from the Chat Menu dropdown into the actual
  // left sidebar (webui/components/sidebar/sidebar.js's renderChatList),
  // pushed from chat.js via window.Sidebar. Stub it to observe what
  // chat.js sends without needing sidebar.js's own DOM.
  const renderCalls = [];
  global.window.Sidebar = {
    renderChatList: (sessions, activeId) => renderCalls.push({ count: sessions.length, activeId }),
  };

  const chat = freshChat();
  chat.init();
  assert.equal(renderCalls.length, 1, "first init must push the initial session to the sidebar");
  assert.equal(renderCalls[0].count, 1);

  chat.input.value = "session one message";
  chat.sendMessage();
  const firstSessionId = chat._conversationId;

  chat.startNewChat();
  assert.equal(renderCalls.at(-1).count, 2, "startNewChat must push the updated session list");
  chat.input.value = "session two message";
  chat.sendMessage();

  chat.switchToSession(firstSessionId);
  assert.equal(chat._conversationId, firstSessionId);
  assert.equal(chat._conversationHistory.length, 1);
  assert.equal(chat._conversationHistory[0].content, "session one message");
  assert.equal(chat.history._children.length, 1, "switching sessions must repaint, not accumulate");
  assert.equal(renderCalls.at(-1).activeId, firstSessionId, "switching must tell the sidebar which session is now active");

  delete global.window.Sidebar;
});

test("Chat Menu (repurposed) exposes utility actions, not the session list", () => {
  const chat = freshChat();
  chat.init();

  chat._toggleMenu();
  assert.equal(chat._menuOpen, true);

  const labels = chat.menuDropdown._children.map((c) => c.textContent);
  assert.deepEqual(labels, ["Export Transcript", "Print", "Summarize This Chat"]);
});

test("_resendLastMessage() re-sends without duplicating the bubble or history entry", () => {
  const chat = freshChat();
  chat.init();

  chat.input.value = "trigger a warning";
  chat.sendMessage(); // simulates the original request that got a safety_warning back instead of a reply

  assert.equal(sentPackets.length, 1);
  assert.equal(chat._conversationHistory.length, 1);
  assert.equal(chat.history._children.length, 1);

  chat._resendLastMessage({ skipSafetyCheck: true });

  assert.equal(sentPackets.length, 2, "resend must send a new chat_request");
  assert.equal(chat._conversationHistory.length, 1, "resend must not push a duplicate history entry");
  assert.equal(chat.history._children.length, 1, "resend must not add a duplicate bubble");

  const resent = sentPackets[1];
  assert.equal(resent.type, "chat_request");
  assert.equal(resent.payload.skipSafetyCheck, true, "override must reach the resent packet");
  assert.deepEqual(
    resent.payload.messages.map((m) => m.content),
    ["trigger a warning"],
    "resend must carry the same message that triggered the warning"
  );
});

test("_resendLastMessage() is a no-op when there is no pending user turn", () => {
  const chat = freshChat();
  chat.init();

  chat._resendLastMessage();
  assert.equal(sentPackets.length, 0);
});

// ============================================================
// Direct chat navigation — clicking a chat from another page must
// navigate to the Chat panel immediately, not silently repaint a
// detached, invisible transcript (see chat.js's _isChatPanelVisible()).
// ============================================================
test("startNewChat() from another page navigates to Chat instead of touching stale DOM", () => {
  const chat = freshChat();
  chat.init();
  chat.input.value = "message before leaving";
  chat.sendMessage();

  const staleHistoryEl = chat.history;

  // Simulate the user having navigated to, say, the Models page —
  // #panel-container no longer holds chat.html, so chat.history/etc. are
  // stale references (same as resetChatDom() simulates elsewhere in this
  // file), and Router.currentPanel reflects the new page.
  global.window.Router.currentPanel = "models";

  chat.startNewChat();

  // Must navigate rather than write into the stale (now-invisible) node.
  assert.equal(dispatchedEvents.length, 1);
  assert.equal(dispatchedEvents[0].type, "navigatePanel");
  assert.equal(dispatchedEvents[0].detail, "chat");
  assert.equal(staleHistoryEl._children.length, 1, "the stale, detached node must be left untouched");

  // Session state itself must still update correctly — Chat.init()'s
  // re-bind branch (triggered by the navigation) repaints from this once
  // chat.html is actually remounted.
  assert.equal(chat._sessions.length, 2);
  assert.equal(chat._conversationHistory.length, 0);
});

test("switchToSession() from another page navigates to Chat instead of repainting stale DOM", () => {
  const chat = freshChat();
  chat.init();
  chat.input.value = "session one message";
  chat.sendMessage();
  const firstSessionId = chat._conversationId;

  chat.startNewChat(); // still on "chat" here — session two is now active
  chat.input.value = "session two message";
  chat.sendMessage();

  const staleHistoryEl = chat.history;
  global.window.Router.currentPanel = "settings";
  dispatchedEvents.length = 0;

  chat.switchToSession(firstSessionId);

  assert.equal(dispatchedEvents.length, 1);
  assert.equal(dispatchedEvents[0].type, "navigatePanel");
  assert.equal(dispatchedEvents[0].detail, "chat");
  assert.equal(staleHistoryEl._children.length, 1, "must not repaint into the stale, detached node");

  // State must still switch correctly even though nothing was repainted.
  assert.equal(chat._conversationId, firstSessionId);
  assert.equal(chat._conversationHistory.length, 1);
  assert.equal(chat._conversationHistory[0].content, "session one message");
});

test("switchToSession() for the already-active session while ALREADY on Chat is a true no-op", () => {
  const chat = freshChat();
  chat.init();
  chat.input.value = "only message";
  chat.sendMessage();

  dispatchedEvents.length = 0;
  chat.switchToSession(chat._conversationId);

  assert.equal(dispatchedEvents.length, 0, "must not navigate or send anything when nothing actually changes");
});

test("switchToSession() for the already-active session from ANOTHER page still navigates to Chat", () => {
  // Even the currently-active session must be reachable by clicking it
  // from elsewhere — "clicking a chat navigates immediately" applies
  // regardless of whether it happens to already be the active session.
  const chat = freshChat();
  chat.init();
  chat.input.value = "only message";
  chat.sendMessage();
  const activeId = chat._conversationId;

  global.window.Router.currentPanel = "diagnostics";
  dispatchedEvents.length = 0;

  chat.switchToSession(activeId);

  assert.equal(dispatchedEvents.length, 1);
  assert.equal(dispatchedEvents[0].type, "navigatePanel");
  assert.equal(dispatchedEvents[0].detail, "chat");
});

// ============================================================
// Delete / Rename chat (webui/components/sidebar/sidebar.js's "⋮" menu
// delegates to these). There is no backend chat registry to clean up —
// conversation_id is just a per-connection label
// (backend/websocket/handlers.py never persists conversation history
// keyed by it) — so deletion/rename are purely operations on chat.js's
// own in-memory _sessions array.
// ============================================================
test("deleteSession() removes the chat from the in-memory registry", () => {
  const chat = freshChat();
  chat.init();
  chat.startNewChat();
  const idToDelete = chat._sessions[0].id; // the first (non-active) session

  chat.deleteSession(idToDelete);

  assert.equal(chat._sessions.length, 1, "the deleted session must be gone from _sessions");
  assert.ok(!chat._sessions.some((s) => s.id === idToDelete), "no trace of the deleted id must remain");
});

test("deleteSession() updates the sidebar immediately", () => {
  const renderCalls = [];
  global.window.Sidebar = {
    renderChatList: (sessions, activeId) => renderCalls.push({ count: sessions.length, activeId }),
  };

  const chat = freshChat();
  chat.init();
  chat.startNewChat();
  const idToDelete = chat._sessions[0].id;
  renderCalls.length = 0;

  chat.deleteSession(idToDelete);

  assert.equal(renderCalls.length, 1, "deleting a chat must immediately push an updated list to the sidebar");
  assert.equal(renderCalls[0].count, 1);

  delete global.window.Sidebar;
});

test("deleting the active chat switches to a fresh New Chat", () => {
  const chat = freshChat();
  chat.init();
  chat.input.value = "message in the chat about to be deleted";
  chat.sendMessage();
  const activeId = chat._conversationId;

  chat.deleteSession(activeId);

  assert.notEqual(chat._conversationId, activeId, "a fresh session must now be active");
  assert.equal(chat._conversationHistory.length, 0, "the fresh session must start empty");
  assert.equal(chat.history._children.length, 0, "the transcript must be repainted empty, not left showing the deleted chat");
  assert.ok(!chat._sessions.some((s) => s.id === activeId), "the deleted session must not remain in _sessions");
});

test("deleting a non-active chat leaves the active chat untouched and no duplicates remain", () => {
  const chat = freshChat();
  chat.init();
  chat.input.value = "session one message";
  chat.sendMessage();
  const firstId = chat._conversationId;

  chat.startNewChat();
  chat.input.value = "session two message";
  chat.sendMessage();
  const secondId = chat._conversationId;

  chat.deleteSession(firstId);

  assert.equal(chat._sessions.length, 1, "exactly one session must remain, no duplicates");
  assert.equal(chat._sessions[0].id, secondId);
  assert.equal(chat._conversationId, secondId, "the still-active chat must be unaffected by deleting a different one");
  assert.equal(chat._conversationHistory.length, 1);
  assert.equal(chat._conversationHistory[0].content, "session two message");
});

test("deleteSession() with an unknown id is a safe no-op", () => {
  const chat = freshChat();
  chat.init();
  const before = chat._sessions.length;

  chat.deleteSession("not-a-real-session-id");

  assert.equal(chat._sessions.length, before, "an unknown id must not change the session count");
});

test("renameSession() sets a pinned label that renderChatList() would receive", () => {
  const chat = freshChat();
  chat.init();
  const id = chat._conversationId;

  chat.renameSession(id, "My Renamed Chat");

  assert.equal(chat._sessions[0].label, "My Renamed Chat");
});

test("renameSession() ignores a blank label", () => {
  const chat = freshChat();
  chat.init();
  const id = chat._conversationId;
  chat._sessions[0].label = "Original Label";

  chat.renameSession(id, "   ");

  assert.equal(chat._sessions[0].label, "Original Label", "a blank/whitespace-only label must be ignored");
});

// ============================================================
let failures = 0;
for (const { name, fn } of tests) {
  try {
    fn();
    console.log(`PASS  ${name}`);
  } catch (err) {
    failures += 1;
    console.log(`FAIL  ${name}: ${err.message}`);
  }
}

console.log("");
if (failures) {
  console.log(`${failures} FAILED`);
  process.exit(1);
}
console.log("All tests passed.");
