// One button, two jobs: Send while idle, Stop while a turn is running.
//
// The backend half of this is backend/tests/test_chat_turn_dispatch.py.
// This side checks the wiring that decides WHICH job the button does,
// and that the pieces it depends on actually exist -- a Stop that sends
// a packet name the schema does not carry is a button that does
// nothing, which is worse than no button.
//
// Source-level, like every other test in this directory: there is no
// DOM here.

import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

const read = (...parts) => readFileSync(join(root, ...parts), "utf8");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ======================================================
// The wire names
// ======================================================

test("the stop packet names exist in the schema", () => {
  const schema = read("core", "ipc_schema.js");

  assert.ok(schema.includes('CHAT_STOP: "chat_stop"'),
            "chat.js sends IPC.CHAT_STOP; undefined would send `undefined`");
  assert.ok(schema.includes('CHAT_STOP_RESULT: "chat_stop_result"'));
  assert.ok(schema.includes('STREAM_CANCELLED: "stream_cancelled"'));
});

test("the frontend and backend agree on the packet name", () => {
  const schema = read("core", "ipc_schema.js");
  const handlers = readFileSync(
    join(root, "..", "backend", "websocket", "handlers.py"), "utf8");

  const backendName = handlers.match(/STOP_TYPE\s*=\s*"([^"]+)"/);
  assert.ok(backendName, "backend does not define STOP_TYPE");
  assert.ok(schema.includes(`CHAT_STOP: "${backendName[1]}"`),
            `backend listens for "${backendName[1]}"`);
});

// ======================================================
// Which job the button does
// ======================================================

test("the send button stops when a turn is running", () => {
  const js = read("components", "chat", "chat.js");
  const handler = js.slice(js.indexOf('this.sendBtn.addEventListener("click"'));
  const body = handler.slice(0, handler.indexOf("});"));

  assert.ok(body.includes("isTurnRunning()"),
            "the button must ask which job it is doing");
  assert.ok(body.indexOf("stopCurrentTurn") < body.indexOf("this.sendMessage()"),
            "the stop branch must return before sending a second message");
});

test("Enter always sends and never stops", () => {
  const js = read("components", "chat", "chat.js");
  const keydown = js.slice(js.indexOf('this.input.addEventListener("keydown"'));
  const body = keydown.slice(0, keydown.indexOf("});"));
  const enter = body.slice(body.indexOf('ev.key === "Enter"'),
                           body.indexOf('ev.key === "Escape"'));

  assert.ok(enter.includes("this.sendMessage()"));
  assert.ok(!enter.includes("stopCurrentTurn"),
            "muscle memory would cancel the turn it had just started");
});

test("Escape stops, but only while something is running", () => {
  const js = read("components", "chat", "chat.js");

  assert.ok(/ev\.key === "Escape" && this\.isTurnRunning\(\)/.test(js),
            "Escape on an idle chat must do nothing");
});

// ======================================================
// When the button flips
// ======================================================

test("every chat_request marks the turn as running", () => {
  const js = read("components", "chat", "chat.js");
  const sends = js.split("bridge.send(IPC.CHAT_REQUEST").length - 1;
  const marks = js.split("this._setTurnRunning(true)").length - 1;

  assert.strictEqual(marks, sends,
    `${sends} chat_request sends but ${marks} marked as running -- one that ` +
    "is missed leaves the button saying Send during a turn");
});

test("the turn is marked finished when the stream ends", () => {
  const js = read("components", "chat", "chat.js");
  const end = js.slice(js.indexOf("_handleStreamEnd(packet) {"));
  const body = end.slice(0, end.indexOf("\n  },"));

  assert.ok(body.includes("this._setTurnRunning(false)"),
            "otherwise the button stays on Stop after the reply arrives");
});

test("the stop result flips the button back", () => {
  const js = read("components", "chat", "chat.js");

  assert.ok(js.includes("IPC.CHAT_STOP_RESULT"),
            "an unhandled result leaves the button on Stop forever");
});

test("the button never claims to have stopped before the backend says so", () => {
  const js = read("components", "chat", "chat.js");
  const stop = js.slice(js.indexOf("stopCurrentTurn() {"));
  const body = stop.slice(0, stop.indexOf("\n  },"));

  assert.ok(!body.includes("_setTurnRunning(false)"),
            "flipping here would say 'stopped' for a stop that failed");
});

// ======================================================
// It has to be visible
// ======================================================

test("stopping looks different from sending", () => {
  const css = read("components", "chat", "chat.css");

  assert.ok(css.includes("#chat-send-btn.is-stopping"),
            "a button whose label changes and nothing else is easy to miss");
});

test("the stop style uses tokens the theme actually defines", () => {
  const css = read("components", "chat", "chat.css");
  const rule = css.slice(css.indexOf("#chat-send-btn.is-stopping"));
  const used = [...rule.slice(0, 400).matchAll(/var\((--[\w-]+)/g)].map(m => m[1]);
  const theme = read("themes", "base", "charcoal.css");

  for (const token of used) {
    assert.ok(theme.includes(`${token}:`),
              `${token} is not defined by the theme, so the rule silently ` +
              "falls back to nothing");
  }
});

test("the button is never disabled while running", () => {
  const js = read("components", "chat", "chat.js");
  const setter = js.slice(js.indexOf("_setTurnRunning(running) {"));
  const body = setter.slice(0, setter.indexOf("\n  },"));

  assert.ok(body.includes("button.disabled = false"),
            "a disabled button is what made a slow turn look like a freeze");
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (error) {
    failed += 1;
    console.error(`  FAIL  ${name}\n        ${error.message}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
