// A continuation appends to the bubble it is continuing.
//
// The backend routes an attached turn's tokens under the SESSION's
// requestId -- the one the previous reply used. Without the client
// half, that token finds no active stream (stream_end cleared it) and
// opens a SECOND bubble, which is the thing multi-turn streaming
// exists to fix.
//
// Source-level, like every other test here: there is no DOM.

import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const read = (...parts) => readFileSync(join(root, ...parts), "utf8");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the finished bubble is kept addressable", () => {
  const js = read("components", "chat", "chat.js");
  const end = js.slice(js.indexOf("_handleStreamEnd(packet) {"));
  const body = end.slice(0, end.indexOf("\n  },"));

  assert.ok(body.includes("this._lastStream = {"),
            "without this a continuation has nothing to reopen");
  assert.ok(body.indexOf("this._lastStream") < body.indexOf("this._activeStream = null"),
            "it must be captured before the active stream is cleared");
});

test("a token for the last bubble reopens it instead of making a new one", () => {
  const js = read("components", "chat", "chat.js");
  const token = js.slice(js.indexOf("_handleStreamToken(packet) {"));
  const body = token.slice(0, token.indexOf("\n  },"));

  assert.ok(body.includes("this._lastStream.requestId === packet.requestId"),
            "the match is on requestId -- that is what the session pins");
  assert.ok(body.indexOf("_lastStream.requestId === packet.requestId")
            < body.indexOf("_createMessageShell"),
            "reopening must be tried before a new shell is created");
});

test("a stale bubble that left the DOM is not reopened", () => {
  const js = read("components", "chat", "chat.js");

  assert.ok(js.includes("this._lastStream.line.isConnected"),
            "a cleared chat would otherwise be written into a detached node");
});

test("only the last bubble is continuable", () => {
  const js = read("components", "chat", "chat.js");
  const end = js.slice(js.indexOf("_handleStreamEnd(packet) {"));
  const body = end.slice(0, end.indexOf("\n  },"));

  // A list would let "continue" reopen something from ten replies ago.
  assert.ok(!/_lastStreams|_streamHistory|\.push\(/.test(body),
            "only the most recent reply is what 'continue' could mean");
});

test("the continued bubble keeps what it already had", () => {
  const js = read("components", "chat", "chat.js");
  const token = js.slice(js.indexOf("_handleStreamToken(packet) {"));
  const body = token.slice(0, token.indexOf("\n  },"));

  assert.ok(body.includes("buffer: this._lastStream.buffer"),
            "starting from an empty buffer would erase the first half");
});

let failed = 0;
for (const [name, fn] of tests) {
  try { fn(); console.log(`  ok  ${name}`); }
  catch (error) { failed += 1; console.error(`  FAIL  ${name}\n        ${error.message}`); }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
