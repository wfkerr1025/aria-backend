// webui/tests/commit_verification_report_tests.mjs
//
// What the Control Center says after a commit.
//
// Commit is where a staged EDIT reaches the project, so it is where the
// verification runs -- and the page has to say what happened. The
// failure this guards against is quiet: an undone commit fell through
// to the generic counter and rendered as "undone: 1 file", which reads
// exactly like a commit that worked. The whole point is that it did not.
//
// These RUN the function rather than searching its source, so a
// rendering that stopped being called would fail here.

import assert from "node:assert/strict";

// workspaces.js imports bridge.js, which touches window at module load.
// The other suites in here read source text to avoid that; this one
// wants to RUN the function, so it supplies the little the module needs
// and imports for real. A shim is cheaper than a rendering that is only
// ever checked by grep.
globalThis.window = {
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
  ARIA_STATE: {},
};
globalThis.document = {
  getElementById: () => null,
  addEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
};

const { describeReport } = await import("../pages/workspaces/workspaces.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("an undone commit never reads as a successful one", () => {
  const said = describeReport({
    status: "undone",
    files: ["greet.py"],
    verification: {
      message: "I ran your full test suite and this change broke "
             + "`tests/test_greet.py::test_hello`. I put your project back "
             + "the way it was.",
    },
  });

  assert.ok(said.startsWith("Undone."), said);
  assert.ok(said.includes("test_hello"), said);
  assert.ok(!said.includes("1 file"), said);
});

test("an undone commit says something even with no message", () => {
  const said = describeReport({ status: "undone", files: ["greet.py"] });

  assert.ok(said.includes("put back"), said);
  assert.ok(!said.includes("1 file"), said);
});

test("a successful commit reports that the tests ran", () => {
  const said = describeReport({
    status: "committed",
    files: ["greet.py"],
    verification: { message: "I ran your full test suite (4s) and they pass." },
  });

  assert.ok(said.includes("committed: 1 file."), said);
  assert.ok(said.includes("they pass"), said);
});

test("a commit with no verification does not imply one happened", () => {
  const said = describeReport({ status: "committed", files: ["a.py", "b.py"] });

  assert.equal(said, "committed: 2 files.");
});

test("refused and empty are unchanged", () => {
  assert.ok(describeReport({ status: "refused" }).startsWith("Refused:"));
  assert.equal(describeReport({ status: "empty" }), "Nothing to do.");
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
