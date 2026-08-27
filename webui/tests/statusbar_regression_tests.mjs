// webui/tests/statusbar_regression_tests.mjs
//
// Regression tests for the bottom-left model indicator
// (webui/components/statusbar/statusbar.js): Active Model ONLY. Covers
// three generations of bug fixes:
//
//   - (earliest) it used to render the literal string "Model: cloud
//     (Cloud)" (a stale/incomplete active_model_changed provider name
//     falling back to the word "cloud" itself).
//
//   - (later) Active Model used to ALSO be driven by per-turn
//     active_model_changed packets, which could legitimately be a
//     lighter/fallback-tier model under Automatic Mode routing for a
//     simple prompt — so the status bar could show a different model
//     than the Models/Diagnostics page and Routing Log (both keyed off
//     the stable mode_status_result.active_model_id registry role),
//     which is exactly what looked like "Active Model shows the
//     fallback model". Active Model is bound strictly to
//     mode_status_result; active_model_changed is only ever consulted
//     to decide which local model to query the auto-balancer for (the
//     "Balanced" chip), never to render the label.
//
//   - (this pass) the status bar had grown System Model and Emergency
//     Model labels alongside Active Model, making it read as a
//     routing-context dashboard rather than a mode indicator. Those
//     two are system CONFIGURATION values (which model plays which
//     registry role) and now live only in Diagnostics -> Models
//     (webui/pages/models/models.js) and Settings -> Local Models
//     (per-card role badges) — see statusbar.js's module docstring.
//     The bottom bar shows the Active Model and nothing else.
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/settings_regression_tests.mjs. Run directly:
//
//   node webui/tests/statusbar_regression_tests.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webuiRoot = path.resolve(__dirname, "..");

function makeElement(tag) {
  const el = {
    tagName: tag,
    _classes: new Set(),
    textContent: "",
    get className() {
      return [...el._classes].join(" ");
    },
    set className(v) {
      el._classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
  };
  return el;
}

const registry = {};
function resetStatusbarDom() {
  registry["active-model-status"] = makeElement("div");
  registry["auto-balance-chip"] = makeElement("button");
  registry["auto-balance-chip"].addEventListener = () => {};
  registry["auto-balance-chip"].style = {};
  // Deliberately NOT registered: "system-model-status"/"emergency-
  // model-status" no longer exist in index.html at all (see the tests
  // below asserting exactly that) — getElementById() for either must
  // return null, which is what an absent registry entry already gives.
}
resetStatusbarDom();

global.document = {
  getElementById: (id) => registry[id] || null,
};
global.window = global;

let sentPackets = [];
global.window.aria = { sendToBackend: (p) => sentPackets.push(p), log: () => {} };

const backendPacketListeners = [];
global.window.addEventListener = function (evt, cb) {
  if (evt === "backend-packet") backendPacketListeners.push(cb);
};

function dispatchBackendPacket(packet) {
  backendPacketListeners.forEach((cb) => cb({ detail: packet }));
}

const { default: Statusbar } = await import("../components/statusbar/statusbar.js");

function freshStatusbar() {
  sentPackets = [];
  backendPacketListeners.length = 0;
  resetStatusbarDom();
  Statusbar.init();
  sentPackets = []; // drop init()'s own mode_status_request from later assertions
  return Statusbar;
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("init() requests mode_status_request so the label isn't stuck empty", () => {
  resetStatusbarDom();
  backendPacketListeners.length = 0;
  sentPackets = [];
  Statusbar.init();
  const types = sentPackets.map((p) => p.type);
  assert.ok(types.includes("mode_status_request"), `expected mode_status_request, got ${JSON.stringify(types)}`);
});

test("mode_status_result in local mode shows the active model's display name", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", cloud_provider: null, cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: "mistral-7b-q4km",
      active_model_display_name: "Mistral 7B Instruct (Q4_K_M)",
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: Mistral 7B Instruct (Q4_K_M) (Local)");
  assert.ok(el._classes.has("model-local"));
});

test("mode_status_result in automatic mode labels it (Automatic), not (Local)", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "automatic", cloud_provider: null, cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: "nemo-12b-q5",
      active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)",
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: Mistral Nemo 12B Instruct (Q5_K_M) (Automatic)");
  assert.ok(el._classes.has("model-automatic"));
});

test("mode_status_result in cloud mode shows the provider's proper display name", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "openai", cloud_provider_display_name: "OpenAI",
      explicit_model_override: null, active_model_id: null, active_model_display_name: null,
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: OpenAI (Cloud)");
  assert.ok(el._classes.has("model-cloud"));
});

test("mode_status_result in cloud mode with no provider ever chosen never shows the literal word 'cloud'", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: null, cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: null, active_model_display_name: null,
    },
  });
  const el = registry["active-model-status"];
  assert.equal(el.textContent, "Model: —");
  assert.ok(!el.textContent.toLowerCase().includes("cloud"), `must never show the literal word "cloud": ${el.textContent}`);
});

test("active_model_changed never touches the Active Model label — only mode_status_result does", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", cloud_provider: null, cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: "mistral-7b-q4km",
      active_model_display_name: "Mistral 7B Instruct (Q4_K_M)",
    },
  });
  assert.equal(registry["active-model-status"].textContent, "Model: Mistral 7B Instruct (Q4_K_M) (Local)");

  // A per-turn active_model_changed for a COMPLETELY different model
  // (as Automatic Mode routing a simple prompt to a lighter model
  // would send) must NOT change what's on screen — this is exactly the
  // "Active Model shows the fallback model" bug being guarded against.
  dispatchBackendPacket({
    type: "active_model_changed",
    modelId: "qwen2.5-0.5b-instruct-q4_k_m",
    displayName: "Qwen2.5 0.5B Instruct (Q4_K_M)",
    requestId: 1, location: "local", provider: "local", providerDisplayName: "Local",
  });
  assert.equal(
    registry["active-model-status"].textContent, "Model: Mistral 7B Instruct (Q4_K_M) (Local)",
    "active_model_changed must never override the mode_status_result-driven Active Model label"
  );

  dispatchBackendPacket({
    type: "active_model_changed",
    modelId: null, displayName: null, requestId: 2,
    location: "cloud", provider: "anthropic", providerDisplayName: "Anthropic",
  });
  assert.equal(
    registry["active-model-status"].textContent, "Model: Mistral 7B Instruct (Q4_K_M) (Local)",
    "active_model_changed must never override the label even when it reports a cloud location"
  );
});

test("mode_status_result is authoritative even after several active_model_changed packets have arrived", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "active_model_changed",
    modelId: null, displayName: null, requestId: 4,
    location: "cloud", provider: "openai", providerDisplayName: "OpenAI",
  });
  assert.equal(registry["active-model-status"].textContent, "", "no mode_status_result has arrived yet — active_model_changed alone must not render anything");

  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "automatic", cloud_provider: null, cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: "nemo-12b-q5",
      active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)",
    },
  });
  assert.equal(registry["active-model-status"].textContent, "Model: Mistral Nemo 12B Instruct (Q5_K_M) (Automatic)");
});

test("mode_set_result (explicit mode switch) re-requests mode_status so the label refreshes", () => {
  freshStatusbar();
  sentPackets = [];
  dispatchBackendPacket({ type: "mode_set_result", payload: { ok: true, mode: "cloud" } });
  assert.ok(sentPackets.some((p) => p.type === "mode_status_request"), "mode_set_result must trigger a fresh mode_status_request");

  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "anthropic", cloud_provider_display_name: "Anthropic",
      explicit_model_override: null, active_model_id: null, active_model_display_name: null,
    },
  });
  assert.equal(registry["active-model-status"].textContent, "Model: Anthropic (Cloud)");
});

test("MODEL_SET_ACTIVE_RESULT (Models page 'Set as Main' button) re-requests mode_status", () => {
  freshStatusbar();

  sentPackets = [];
  dispatchBackendPacket({ type: "model_set_active_result", payload: { ok: true, model_id: "mistral-7b-q4km" } });
  assert.ok(
    sentPackets.some((p) => p.type === "mode_status_request"),
    "model_set_active_result must trigger a fresh mode_status_request — this is what used to leave the status bar stale after clicking 'Set as Main' on the Models page"
  );
});

test("MODEL_SET_FALLBACK_RESULT and MODEL_SET_EMERGENCY_RESULT do NOT trigger a status-bar refresh — neither role is shown here anymore", () => {
  freshStatusbar();

  sentPackets = [];
  dispatchBackendPacket({ type: "model_set_fallback_result", payload: { ok: true, model_id: "phi-3-mini-4k-instruct-q4" } });
  assert.equal(sentPackets.length, 0, "the status bar shows only the Active Model, so a fallback-role change has nothing here to refresh");

  dispatchBackendPacket({ type: "model_set_emergency_result", payload: { ok: true, model_id: "qwen2.5-0.5b-instruct-q4_k_m" } });
  assert.equal(sentPackets.length, 0, "the status bar shows only the Active Model, so an emergency-role change has nothing here to refresh");
});

test("fallback_model_id/emergency_model_id fields on mode_status_result are ignored — only Active Model renders here", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "local", cloud_provider: null, cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: "nemo-12b-q5",
      active_model_display_name: "Mistral Nemo 12B Instruct (Q5_K_M)",
      fallback_model_id: "phi-3-mini-4k-instruct-q4",
      fallback_model_display_name: "Phi-3-mini-4k-instruct-q4.gguf (auto-discovered)",
      emergency_model_id: "qwen2.5-0.5b-instruct-q4_k_m",
      emergency_model_display_name: "qwen2.5-0.5b-instruct-q4_k_m.gguf (auto-discovered)",
    },
  });
  assert.equal(registry["active-model-status"].textContent, "Model: Mistral Nemo 12B Instruct (Q5_K_M) (Local)");
  assert.equal(document.getElementById("system-model-status"), null, "no #system-model-status element exists to render into");
  assert.equal(document.getElementById("emergency-model-status"), null, "no #emergency-model-status element exists to render into");
});

test("unrelated packet types are ignored without throwing", () => {
  freshStatusbar();
  const el = registry["active-model-status"];
  el.textContent = "Model: —";
  assert.doesNotThrow(() => dispatchBackendPacket({ type: "stream_token", modelId: "x", token: "hi" }));
  assert.equal(el.textContent, "Model: —", "an unrelated packet type must not touch the label");
});

// ============================================================
// Explicit guards for the exact forbidden strings named in the
// absolute-mode-separation task (Section 4). Active Model rendering
// now happens exclusively in _renderFromModeStatus() (active_model_changed
// no longer renders anything at all — see the two tests above), so
// these guards target mode_status_result's cloud branch, which never
// reads active_model_id/active_model_display_name when routing_mode is
// "cloud".
// ============================================================
test("never renders 'Model: cloud (Cloud)' even if provider info is completely missing", () => {
  freshStatusbar();
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "cloud", cloud_provider_display_name: null,
      explicit_model_override: null, active_model_id: null, active_model_display_name: null,
    },
  });
  const text = registry["active-model-status"].textContent;
  assert.notEqual(text, "Model: cloud (Cloud)", text);
});

test("Batch 2.5: cloud mode with a real resolved cloud model shows the model name AND the provider", () => {
  freshStatusbar();
  // As of Batch 2 (backend.core.model_selector), routing_mode="cloud"
  // now carries a REAL cloud model_id/display_name (e.g. "gpt-4" / "GPT-4")
  // — never a local one; backend.core.routing_invariants makes the old
  // "local id leaking into cloud state" shape structurally impossible.
  // The status bar now trusts and shows it, alongside the provider.
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "openai", cloud_provider_display_name: "OpenAI",
      explicit_model_override: null, active_model_id: "gpt-4",
      active_model_display_name: "GPT-4", location: "cloud",
    },
  });
  const text = registry["active-model-status"].textContent;
  assert.equal(text, "Model: GPT-4 (OpenAI · Cloud)", text);
});

test("never renders a local model_id as the Cloud label — a null active_model_display_name falls back to the provider alone", () => {
  freshStatusbar();
  // No specific cloud model resolved yet (e.g. the provider has no
  // default/override mapping) — must show the provider alone, never a
  // local-registry name/id and never the bare word "cloud".
  dispatchBackendPacket({
    type: "mode_status_result",
    payload: {
      routing_mode: "cloud", cloud_provider: "openai", cloud_provider_display_name: "OpenAI",
      explicit_model_override: null, active_model_id: null,
      active_model_display_name: null, location: "cloud",
    },
  });
  const text = registry["active-model-status"].textContent;
  assert.equal(text, "Model: OpenAI (Cloud)", text);
  assert.notEqual(text, "Model: nemo-12b-q5 (Cloud)", text);
});

// ============================================================
// Model indicator lives at the bottom-left of the main window, a
// fixed, page-global overlay aligned with the bottom-right System OK
// indicator — not inside the sidebar, and not inside any router-loaded
// panel's own HTML (so it survives navigation unchanged, with no
// rebind-on-navigation machinery needed).
// ============================================================
test("index.html defines the bottom-left fixed model-status overlay, outside #panel-container", () => {
  const indexHtml = fs.readFileSync(path.join(webuiRoot, "index.html"), "utf-8");
  assert.match(indexHtml, /id="active-model-container"/, "index.html must define the fixed overlay container");
  assert.match(indexHtml, /id="active-model-status"/, "index.html must define the #active-model-status badge itself");

  // It must live OUTSIDE #panel-container (router.js only ever replaces
  // that element's contents), so navigation never destroys/recreates it.
  const panelContainerIdx = indexHtml.indexOf('id="panel-container"');
  const overlayIdx = indexHtml.indexOf('id="active-model-container"');
  assert.ok(panelContainerIdx !== -1 && overlayIdx !== -1);
  assert.ok(overlayIdx > panelContainerIdx, "the overlay must be declared as a sibling of #panel-container, not inside it");
});

test("the model indicator does not appear inside the sidebar", () => {
  const indexHtml = fs.readFileSync(path.join(webuiRoot, "index.html"), "utf-8");
  const sidebarMatch = indexHtml.match(/<aside id="sidebar">[\s\S]*?<\/aside>/);
  assert.ok(sidebarMatch, "expected to find the <aside id=\"sidebar\"> block in index.html");
  assert.doesNotMatch(sidebarMatch[0], /active-model-status/, "the model indicator must not appear inside the sidebar");
});

test("the model indicator does not appear inside chat.html (not between chat history and the input bar)", () => {
  const chatHtml = fs.readFileSync(path.join(webuiRoot, "components/chat/chat.html"), "utf-8");
  assert.doesNotMatch(chatHtml, /active-model-status/, "chat.html must not define the model-status badge itself");
  assert.doesNotMatch(chatHtml, /chat-model-status-bar/, "chat.html must not define the old in-window status bar container");
});

test("System Model and Emergency Model no longer have any DOM presence anywhere in index.html", () => {
  const indexHtml = fs.readFileSync(path.join(webuiRoot, "index.html"), "utf-8");
  assert.doesNotMatch(indexHtml, /system-model-status/, "System Model must not appear in the bottom bar — it belongs in Diagnostics -> Models / Settings -> Local Models only");
  assert.doesNotMatch(indexHtml, /emergency-model-status/, "Emergency Model must not appear in the bottom bar — it belongs in Diagnostics -> Models / Settings -> Local Models only");
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
