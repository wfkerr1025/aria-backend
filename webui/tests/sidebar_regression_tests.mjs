// webui/tests/sidebar_regression_tests.mjs
//
// Regression tests for:
//   - sidebar order: CORE (Models, Diagnostics, Tools, Plugins, File
//     Ops, Debug, Settings) above CHATS (New Chat, existing chats) — a
//     plain "Chats" section label like "Core", not a special nav button
//     — no collapsible/expandable wrapper.
//   - "New Chat" is wired to Chat.startNewChat().
//   - chats are directly clickable from any page — see
//     webui/tests/frontend_regression_tests.mjs for the chat.js-side
//     coverage of "clicking a chat from another page navigates there
//     immediately instead of silently touching stale DOM"; this file
//     covers the sidebar's own markup/wiring.
//   - each chat row's "⋮" menu (Rename Chat / Delete Chat) delegates to
//     Chat.renameSession()/deleteSession(), only one menu open at a
//     time, closed by an outside click.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/sidebar_regression_tests.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webuiRoot = path.resolve(__dirname, "..");

function makeElement(tag) {
  const el = {
    tagName: tag,
    dataset: {},
    _classes: new Set(),
    _listeners: {},
    _children: [],
    _innerHTML: "",
    textContent: "",
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() {
      return el._innerHTML;
    },
    set innerHTML(v) {
      el._innerHTML = v;
      el._children = [];
    },
    classList: {
      add: (...c) => c.forEach((x) => el._classes.add(x)),
      remove: (...c) => c.forEach((x) => el._classes.delete(x)),
      contains: (c) => el._classes.has(c),
    },
    appendChild(child) {
      el._children.push(child);
      return child;
    },
    addEventListener(evt, cb) {
      (el._listeners[evt] ||= []).push(cb);
    },
    setAttribute(name, value) {
      el[name] = value;
    },
    dispatch(evt, arg) {
      const fakeEvent = arg !== undefined ? arg : { stopPropagation() {} };
      (el._listeners[evt] || []).forEach((cb) => cb(fakeEvent));
    },
  };
  return el;
}

// ============================================================
// PART 1 — static markup checks (index.html)
// ============================================================
const indexHtml = fs.readFileSync(path.join(webuiRoot, "index.html"), "utf-8");
const sidebarMatch = indexHtml.match(/<aside id="sidebar">[\s\S]*?<\/aside>/);

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("the sidebar has no collapsible/expandable wrapper anywhere", () => {
  assert.ok(sidebarMatch, "expected to find the <aside id=\"sidebar\"> block");
  // Strip HTML comments first — this file documents the "no collapsible
  // wrapper" design decision in prose inside a comment, which would
  // otherwise trip a naive keyword search.
  const withoutComments = sidebarMatch[0].replace(/<!--[\s\S]*?-->/g, "");
  assert.doesNotMatch(withoutComments, /<details/i, "no <details> collapsible element allowed");
  assert.doesNotMatch(withoutComments, /aria-expanded/, "no aria-expanded toggle allowed");
  assert.doesNotMatch(withoutComments, /class="[^"]*collaps(ed|ible)[^"]*"/i, "no collapsible/expandable class names allowed");
  assert.doesNotMatch(withoutComments, /class="[^"]*expandable[^"]*"/i, "no collapsible/expandable class names allowed");
});

test("sidebar order is CORE above CHATS: Models...Settings, then New Chat + chat list", () => {
  const html = sidebarMatch[0];
  const coreLabelIdx = html.indexOf('sidebar-section-label">Core');
  const modelsIdx = html.indexOf('data-panel="models"');
  const settingsIdx = html.indexOf('data-panel="settings"');
  const dividerIdx = html.indexOf("sidebar-divider");
  const chatsLabelIdx = html.indexOf('sidebar-section-label">Chats');
  const newChatIdx = html.indexOf("sidebar-new-chat-btn");
  const chatListIdx = html.indexOf('id="sidebar-chat-list"');

  for (const [name, idx] of [
    ["Core label", coreLabelIdx], ["Models button", modelsIdx], ["Settings button", settingsIdx],
    ["divider", dividerIdx], ["Chats label", chatsLabelIdx], ["New Chat button", newChatIdx],
    ["chat list", chatListIdx],
  ]) {
    assert.notEqual(idx, -1, `expected to find ${name} in the sidebar block`);
  }

  assert.ok(coreLabelIdx < modelsIdx, "Core label must come before the Models button");
  assert.ok(modelsIdx < settingsIdx, "Models must come before Settings (Core section order)");
  assert.ok(settingsIdx < dividerIdx, "the whole Core section must come before the divider");
  assert.ok(dividerIdx < chatsLabelIdx, "the divider must separate Core from the Chats section");
  assert.ok(chatsLabelIdx < newChatIdx, "Chats label must come before New Chat");
  assert.ok(newChatIdx < chatListIdx, "New Chat must come before the chat list");
});

test("there is no separate 'Chat' nav button — only a plain 'Chats' section label", () => {
  // The Chats section header is a plain label, matching "Core" — not a
  // clickable/highlightable nav button (individual chat entries and New
  // Chat are the only actionable items in this section now).
  assert.doesNotMatch(sidebarMatch[0], /sidebar-chat-header/, "no dedicated chat-header nav button should remain");
});

test("the model indicator is not present in the sidebar", () => {
  assert.doesNotMatch(sidebarMatch[0], /active-model-status/, "the model indicator must not appear inside the sidebar");
});

// ============================================================
// PART 2 — sidebar.js wiring
// ============================================================
const registry = {};
function resetSidebarDom() {
  registry["sidebar-new-chat-btn"] = makeElement("button");
  registry["sidebar-chat-list"] = makeElement("div");
}
resetSidebarDom();

const documentClickListeners = [];
global.document = {
  getElementById: (id) => registry[id] || null,
  querySelectorAll: (sel) => {
    if (sel === ".sidebar-btn:not(.sidebar-new-chat-btn)") return [];
    if (sel === ".sidebar-chat-item-menu.open") {
      return (registry["sidebar-chat-list"]?._children || [])
        .map((row) => row._children[2])
        .filter((menu) => menu && menu._classes.has("open"));
    }
    return [];
  },
  createElement: (tag) => makeElement(tag),
  addEventListener: (evt, cb) => {
    if (evt === "click") documentClickListeners.push(cb);
  },
};
global.window = global;
global.window.aria = { log: () => {} };

const { default: Sidebar } = await import("../components/sidebar/sidebar.js");

function freshSidebar() {
  resetSidebarDom();
  documentClickListeners.length = 0;
  Sidebar.init();
  return Sidebar;
}

function documentClick() {
  documentClickListeners.forEach((cb) => cb());
}

test("New Chat button click calls Chat.startNewChat()", () => {
  freshSidebar();
  let called = false;
  global.window.chatPanel = { startNewChat: () => (called = true) };

  registry["sidebar-new-chat-btn"].dispatch("click");

  assert.equal(called, true);
  delete global.window.chatPanel;
});

test("renderChatList()'s click handler delegates to Chat.switchToSession(), not a local navigation of its own", () => {
  freshSidebar();
  let switchedTo = null;
  global.window.chatPanel = { switchToSession: (id) => (switchedTo = id) };

  Sidebar.renderChatList(
    [{ id: "session-1", history: [{ role: "user", content: "hi" }], label: null, createdAt: Date.now() }],
    "session-1"
  );

  const row = registry["sidebar-chat-list"]._children[0];
  const item = row._children[0];
  item.dispatch("click");
  assert.equal(switchedTo, "session-1");

  delete global.window.chatPanel;
});

test("each chat row renders a '⋮' menu button with Rename Chat and Delete Chat options", () => {
  freshSidebar();
  Sidebar.renderChatList(
    [{ id: "session-1", history: [], label: "My Chat", createdAt: Date.now() }],
    "session-1"
  );

  const row = registry["sidebar-chat-list"]._children[0];
  const [item, menuBtn, menu] = row._children;

  assert.equal(item.className.includes("sidebar-chat-item"), true);
  assert.equal(menuBtn.textContent, "⋮");
  assert.ok(menuBtn._classes.has("sidebar-chat-item-menu-btn"));

  const optionLabels = menu._children.map((c) => c.textContent);
  assert.deepEqual(optionLabels, ["Rename Chat", "Delete Chat"]);
});

test("clicking the '⋮' button opens the menu; clicking it again closes it", () => {
  freshSidebar();
  Sidebar.renderChatList([{ id: "s1", history: [], label: "Chat", createdAt: Date.now() }], "s1");
  const row = registry["sidebar-chat-list"]._children[0];
  const [, menuBtn, menu] = row._children;

  assert.equal(menu._classes.has("open"), false);
  menuBtn.dispatch("click");
  assert.equal(menu._classes.has("open"), true);
  menuBtn.dispatch("click");
  assert.equal(menu._classes.has("open"), false);
});

test("opening one chat's menu closes any other chat's already-open menu", () => {
  freshSidebar();
  Sidebar.renderChatList(
    [
      { id: "s1", history: [], label: "Chat One", createdAt: Date.now() },
      { id: "s2", history: [], label: "Chat Two", createdAt: Date.now() },
    ],
    "s1"
  );

  const rows = registry["sidebar-chat-list"]._children;
  const menuA = rows[0]._children[2];
  const menuB = rows[1]._children[2];

  rows[0]._children[1].dispatch("click"); // open menu A
  assert.equal(menuA._classes.has("open"), true);

  rows[1]._children[1].dispatch("click"); // open menu B
  assert.equal(menuB._classes.has("open"), true);
  assert.equal(menuA._classes.has("open"), false, "opening a second menu must close the first");
});

test("an outside click closes any open chat menu", () => {
  freshSidebar();
  Sidebar.renderChatList([{ id: "s1", history: [], label: "Chat", createdAt: Date.now() }], "s1");
  const row = registry["sidebar-chat-list"]._children[0];
  const [, menuBtn, menu] = row._children;

  menuBtn.dispatch("click");
  assert.equal(menu._classes.has("open"), true);

  documentClick();
  assert.equal(menu._classes.has("open"), false);
});

test("Rename Chat calls Chat.renameSession() with the new label", () => {
  freshSidebar();
  const originalPrompt = global.window.prompt;
  global.window.prompt = () => "New Name";

  let renamed = null;
  global.window.chatPanel = { renameSession: (id, label) => (renamed = { id, label }) };

  Sidebar.renderChatList([{ id: "s1", history: [], label: "Old Name", createdAt: Date.now() }], "s1");
  const row = registry["sidebar-chat-list"]._children[0];
  const [, , menu] = row._children;
  const [renameBtn] = menu._children;
  renameBtn.dispatch("click");

  assert.deepEqual(renamed, { id: "s1", label: "New Name" });

  global.window.prompt = originalPrompt;
  delete global.window.chatPanel;
});

test("Rename Chat does nothing if the prompt is cancelled or empty", () => {
  freshSidebar();
  const originalPrompt = global.window.prompt;
  global.window.prompt = () => null;

  let called = false;
  global.window.chatPanel = { renameSession: () => (called = true) };

  Sidebar.renderChatList([{ id: "s1", history: [], label: "Chat", createdAt: Date.now() }], "s1");
  const row = registry["sidebar-chat-list"]._children[0];
  const [renameBtn] = row._children[2]._children;
  renameBtn.dispatch("click");

  assert.equal(called, false);

  global.window.prompt = originalPrompt;
  delete global.window.chatPanel;
});

test("Delete Chat calls Chat.deleteSession() with the chat's id", () => {
  freshSidebar();
  let deletedId = null;
  global.window.chatPanel = { deleteSession: (id) => (deletedId = id) };

  Sidebar.renderChatList([{ id: "s1", history: [], label: "Chat", createdAt: Date.now() }], "s1");
  const row = registry["sidebar-chat-list"]._children[0];
  const [, deleteBtn] = row._children[2]._children;
  deleteBtn.dispatch("click");

  assert.equal(deletedId, "s1");
  delete global.window.chatPanel;
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
