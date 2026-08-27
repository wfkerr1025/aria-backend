// webui/tests/routing_log_regression_tests.mjs
//
// Regression tests for the collapsible Routing Log panel
// (webui/components/routing_log/routing_log.js) — dual-context UI spec
// item 3D. Covers:
//   - init() requests diagnostics_routing_request so the panel isn't
//     stuck on its static placeholder
//   - a diagnostics_routing_result renders each entry's model/tier/
//     context length/tool use
//   - a stream_end packet (turn just finished) triggers a re-request so
//     the log stays current without polling on a timer
//   - an empty recent_decisions list shows the empty-state message
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/routing_log_regression_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = { tagName: tag, _innerHTML: "", get innerHTML() { return el._innerHTML; }, set innerHTML(v) { el._innerHTML = v; } };
  return el;
}

const registry = {};
function resetDom() {
  registry["routing-log-content"] = makeElement("div");
}
resetDom();

global.document = { getElementById: (id) => registry[id] || null };
global.window = global;

const backendPacketListeners = [];
global.window.addEventListener = (evt, cb) => {
  if (evt === "backend-packet") backendPacketListeners.push(cb);
};

let sentPackets = [];
global.window.aria = { sendToBackend: (p) => sentPackets.push(p), log: () => {} };

function dispatchBackendPacket(packet) {
  backendPacketListeners.forEach((cb) => cb({ detail: packet }));
}

const { default: RoutingLog } = await import("../components/routing_log/routing_log.js");

function freshRoutingLog() {
  sentPackets = [];
  backendPacketListeners.length = 0;
  resetDom();
  RoutingLog._bound = false;
  RoutingLog.init();
  return RoutingLog;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("init() requests diagnostics_routing_request", () => {
  freshRoutingLog();
  const types = sentPackets.map((p) => p.type);
  assert.ok(types.includes("diagnostics_routing_request"), `expected diagnostics_routing_request, got ${JSON.stringify(types)}`);
});

test("a diagnostics_routing_result renders model/tier/context/tool-use", () => {
  freshRoutingLog();
  dispatchBackendPacket({
    type: "diagnostics_routing_result",
    payload: {
      recent_decisions: [
        { selected_model: "nemo-12b-q5", reasoning_tier: "difficult", context_length: 500, tool_use: true },
      ],
    },
  });

  const html = registry["routing-log-content"].innerHTML;
  assert.ok(html.includes("nemo-12b-q5"), html);
  assert.ok(html.includes("difficult"), html);
  assert.ok(html.includes("500"), html);
  assert.ok(html.includes("yes"), html);
});

test("an empty recent_decisions list shows the empty-state message", () => {
  freshRoutingLog();
  dispatchBackendPacket({ type: "diagnostics_routing_result", payload: { recent_decisions: [] } });
  assert.ok(registry["routing-log-content"].innerHTML.includes("No routing decisions yet"));
});

test("stream_end triggers a re-request so the log stays current", () => {
  freshRoutingLog();
  sentPackets = [];
  dispatchBackendPacket({ type: "stream_end", requestId: "r1" });
  const types = sentPackets.map((p) => p.type);
  assert.ok(types.includes("diagnostics_routing_request"), `expected a re-request after stream_end, got ${JSON.stringify(types)}`);
});

test("most recent decisions render first (reverse chronological)", () => {
  freshRoutingLog();
  dispatchBackendPacket({
    type: "diagnostics_routing_result",
    payload: {
      recent_decisions: [
        { selected_model: "older-model", reasoning_tier: "trivial", context_length: 10, tool_use: false },
        { selected_model: "newer-model", reasoning_tier: "medium", context_length: 20, tool_use: false },
      ],
    },
  });
  const html = registry["routing-log-content"].innerHTML;
  assert.ok(html.indexOf("newer-model") < html.indexOf("older-model"), "expected newest decision to render first");
});

let failures = 0;
for (const { name, fn } of tests) {
  try {
    fn();
    console.log(`PASS  ${name}`);
  } catch (err) {
    console.log(`FAIL  ${name}: ${err.message}`);
    failures++;
  }
}

console.log();
if (failures) {
  console.log(`${failures} FAILED`);
  process.exit(1);
} else {
  console.log("All tests passed.");
}
