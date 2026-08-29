// webui/tests/test_refresh_indicator.mjs
//
// The Control Center's Refresh button, and the reason it needed fixing.
//
// Refresh always sent workspace_list_request. It always did. What it
// never did was look like it: the normal outcome of a refresh is state
// identical to the state already on screen, so nothing moved, and a
// button that worked was indistinguishable from a button that was never
// wired up. "Refresh does nothing" was a true report of the feedback,
// not of the packet.
//
// Three things fix that and all three are asserted here: a spinner while
// the request is out, a timestamp when the answer lands, and a flash on
// the cards it rendered. The timestamp is the one that survives
// prefers-reduced-motion, which is why it is not decoration.
//
// Self-contained, plain-assert, no test framework -- same convention as
// the other files here.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webui = path.resolve(here, "..");

const read = (...parts) => fs.readFileSync(path.join(...parts), "utf8");

const pageJs = read(webui, "pages/workspaces/workspaces.js");
const pageHtml = read(webui, "pages/workspaces/workspaces.html");
const pageCss = read(webui, "pages/workspaces/workspaces.css");

// Comments removed properly -- block, line and HTML. A line filter is
// not enough: the comment explaining what the code must not do says the
// forbidden thing out loud, and this codebase has had four source
// assertions match their own explanation that way.
const codeOnly = (source) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/<!--[\s\S]*?-->/g, "")
        .replace(/\/\/.*$/gm, "");

const code = codeOnly(pageJs);

const results = [];
function test(name, fn) {
  try {
    fn();
    results.push(["PASS", name]);
  } catch (error) {
    results.push(["FAIL", `${name}\n    ${error.message}`]);
  }
}

// ------------------------------------------------------
// The spinner
// ------------------------------------------------------
test("the button has something to spin", () => {
  assert.ok(pageHtml.includes('class="ws-spinner"'));
  assert.ok(/\.ws-spinner\s*\{/.test(pageCss));
  assert.ok(/@keyframes ws-spin/.test(pageCss));
});

test("it is hidden until the button is busy", () => {
  // Otherwise it spins forever and stops meaning anything.
  assert.ok(/\.ws-spinner\s*\{[^}]*display:\s*none/.test(pageCss));
  assert.ok(pageCss.includes("#ws-refresh.is-busy .ws-spinner"));
});

test("every request sets it, not just Refresh", () => {
  // Add, remove, set-primary, commit, discard and rollback all go to the
  // backend and all take time. A spinner that appears for one of seven
  // teaches the user it means nothing.
  const sends = code.match(/bridge\.send\(/g) || [];
  const busies = code.match(/this\.setBusy\(true\)/g) || [];
  assert.ok(busies.length >= sends.length - 1,
            `${sends.length} sends but only ${busies.length} setBusy(true)`);
});

test("the button is never disabled", () => {
  // Found by clicking it. A list result clears the busy flag and then
  // immediately re-arms it for the details request behind it, so any
  // request that never gets an answer left Refresh permanently dead --
  // and Refresh is the control that would have recovered the page.
  //
  // A refresh is an idempotent read: nothing is protected by blocking a
  // second one, and the escape hatch must not be the thing that jams.
  assert.ok(!/disabled\s*=\s*(busy|true)/.test(code),
            "the Refresh button can be disabled, which can strand the page");
});

// ------------------------------------------------------
// The timestamp
// ------------------------------------------------------
test("there is somewhere to put it", () => {
  assert.ok(pageHtml.includes('id="ws-refreshed"'));
});

test("it is announced to a screen reader", () => {
  // The whole point of this element is to say "that click did
  // something", which is exactly the case aria-live exists for.
  assert.ok(/id="ws-refreshed"[^>]*aria-live="polite"/.test(pageHtml));
});

test("it is written when a result arrives", () => {
  assert.ok(code.includes("markRefreshed"));
  assert.ok(code.includes("Refreshed at ${new Date().toLocaleTimeString()}"));
});

test("the clock does not jitter", () => {
  // Proportional digits make a ticking timestamp shuffle sideways, which
  // draws the eye to the one part of the page that is not news.
  assert.ok(/\.ws-refreshed\s*\{[^}]*tabular-nums/.test(pageCss));
});

// ------------------------------------------------------
// The flash
// ------------------------------------------------------
test("the cards flash", () => {
  assert.ok(code.includes("flashCards"));
  assert.ok(pageCss.includes(".ws-flash"));
  assert.ok(/@keyframes ws-flash/.test(pageCss));
});

test("only when the user asked for it", () => {
  // Every arriving packet renders the list, including the one that draws
  // the page. A highlight that fires constantly stops meaning "this just
  // changed".
  assert.ok(code.includes("flashPending"));
  assert.ok(/flashPending\s*=\s*true/.test(code), "nothing ever arms the flash");
  assert.ok(/flashPending\s*=\s*false/.test(code), "nothing ever disarms it");
});

test("the class is always removed, event or no event", () => {
  // animationend is the accurate signal; the timer is the one that
  // always arrives. Under prefers-reduced-motion the animation is
  // `none` and animationend never fires, and in a hidden tab animations
  // do not advance -- which is how this was found. Without the backstop
  // the class sticks, and every card then flashes at once the moment
  // animations resume.
  assert.ok(code.includes('addEventListener("animationend"'));
  assert.ok(code.includes("{ once: true }"));
  assert.ok(/setTimeout\(clear/.test(code), "nothing clears the flash when no event fires");
});

// ------------------------------------------------------
// Failure still clears it
// ------------------------------------------------------
test("a refused operation does not leave the button spinning", () => {
  // A spinner that never stops reads as "still working" and is worse
  // than no spinner: it hides the error rather than reporting it.
  const errorBranch = code.slice(code.indexOf('packet.type === "error"'));
  assert.ok(errorBranch.includes("this.setBusy(false)"));
});

// ------------------------------------------------------
// Motion is a preference
// ------------------------------------------------------
test("reduced motion turns the animations off", () => {
  const query = pageCss.slice(pageCss.indexOf("prefers-reduced-motion"));
  assert.ok(query.includes(".ws-spinner"));
  assert.ok(query.includes(".ws-flash"));
});

test("and the timestamp still says it happened", () => {
  // The reason it is safe to drop both animations: the one piece of
  // feedback that is text, not motion, is untouched by that media query.
  const query = pageCss.slice(pageCss.indexOf("prefers-reduced-motion"));
  assert.ok(!query.includes("ws-refreshed"));
});

// ------------------------------------------------------
for (const [status, name] of results) console.log(`${status}  ${name}`);
const failed = results.filter(([status]) => status === "FAIL");
if (failed.length) {
  console.log(`\n${failed.length} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll tests passed.");
