// webui/tests/warning_banner_regression_tests.mjs
//
// Regression tests for the unified warning banner
// (webui/components/warning_banner/warning_banner.js) — the in-chat
// counterpart to backend.core.warning_manager's warning_event packets:
//   - a "normal" warning renders once and, once dismissed, never
//     reappears (the backend itself never re-sends it this session,
//     but this component must not force it back up on a stray repeat
//     either)
//   - a "critical" warning ALWAYS re-appears when a new event for the
//     same id arrives, even if the user just dismissed it
//   - only visible while the Chat panel is the active one
//   - always names the ACTIVE model, never a fallback/system model
//
// Self-contained, plain-assert, no test framework — same convention as
// webui/tests/statusbar_regression_tests.mjs. Run directly:
//
//   node webui/tests/warning_banner_regression_tests.mjs

import assert from "node:assert/strict";

function makeElement(tag) {
  const el = {
    tagName: tag,
    _children: [],
    _innerHTML: "",
    get innerHTML() {
      return el._innerHTML;
    },
    set innerHTML(v) {
      el._innerHTML = v;
      el._children = [];
    },
    appendChild(child) {
      el._children.push(child);
    },
    querySelectorAll(sel) {
      // Only ever used here to count rendered banner rows.
      if (sel === ".aria-warning-banner") return el._children.filter((c) => c._className?.includes("aria-warning-banner"));
      return [];
    },
  };
  return el;
}

function makeButton() {
  const el = makeElement("button");
  el._listeners = {};
  el.addEventListener = (evt, cb) => {
    (el._listeners[evt] ||= []).push(cb);
  };
  el.click = () => (el._listeners.click || []).forEach((cb) => cb());
  return el;
}

// document.createElement needs to distinguish div vs button so the
// component's dismiss button is actually clickable in this fake DOM.
global.document = {
  createElement: (tag) => (tag === "button" ? makeButton() : makeElement(tag)),
  getElementById: (id) => registry[id] || null,
};

const registry = {};
function resetDom() {
  registry["aria-warning-banner-container"] = makeElement("div");
  // className setter used by the component to tag level-normal/level-critical.
  registry["aria-warning-banner-container"]._children = [];
}
resetDom();

global.window = global;
const backendPacketListeners = [];
const navigatePanelListeners = [];
global.window.addEventListener = (evt, cb) => {
  if (evt === "backend-packet") backendPacketListeners.push(cb);
  if (evt === "navigatePanel") navigatePanelListeners.push(cb);
};
global.window.Router = { currentPanel: "chat" };
global.window.Statusbar = { getActiveModelDisplayName: () => null };
global.window.aria = { log: () => {} };

function dispatchBackendPacket(packet) {
  backendPacketListeners.forEach((cb) => cb({ detail: packet }));
}

const { default: WarningBanner } = await import("../components/warning_banner/warning_banner.js");

function freshBanner() {
  backendPacketListeners.length = 0;
  navigatePanelListeners.length = 0;
  resetDom();
  WarningBanner._banners.clear();
  WarningBanner.init();
  return WarningBanner;
}

// Real elements created via document.createElement track their own
// className for querying below (component sets el.className = "...").
function patchClassNameTracking() {
  const origCreate = global.document.createElement;
  global.document.createElement = (tag) => {
    const el = origCreate(tag);
    let cls = "";
    Object.defineProperty(el, "className", {
      get: () => cls,
      set: (v) => {
        cls = v;
        el._className = v;
      },
    });
    return el;
  };
}
patchClassNameTracking();

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

function visibleBanners(banner) {
  return registry["aria-warning-banner-container"]._children.filter((c) => c._className);
}

test("a normal warning renders and includes the Active Model, not a fallback", () => {
  const banner = freshBanner();
  global.window.Statusbar.getActiveModelDisplayName = () => "Mistral 7B";

  dispatchBackendPacket({
    type: "warning_event", id: "heavy_model", level: "normal",
    message: "Model may be heavy for your PC.", model_id: "mistral-7b-q4km",
  });

  const rendered = visibleBanners(banner);
  assert.equal(rendered.length, 1);
  const msgSpan = rendered[0]._children.find((c) => c.tagName === "span");
  assert.ok(msgSpan.textContent.includes("Mistral 7B"), `expected the Active Model name in: ${msgSpan.textContent}`);
  assert.ok(!msgSpan.textContent.includes("fallback"), "must never reference a fallback/system model");
});

test("dismissing a normal warning hides it and it does not come back on a stray repeat", () => {
  const banner = freshBanner();
  dispatchBackendPacket({
    type: "warning_event", id: "performance_reduced", level: "normal",
    message: "Performance may be reduced.", model_id: "m1",
  });
  assert.equal(visibleBanners(banner).length, 1);

  const dismissBtn = registry["aria-warning-banner-container"]._children[0]._children.find((c) => c.tagName === "button");
  dismissBtn.click();
  assert.equal(visibleBanners(banner).length, 0);

  // A stray repeat of the SAME normal event must not force it back.
  dispatchBackendPacket({
    type: "warning_event", id: "performance_reduced", level: "normal",
    message: "Performance may be reduced.", model_id: "m1",
  });
  assert.equal(visibleBanners(banner).length, 0);
});

test("a critical warning re-appears on a new event even after being dismissed", () => {
  const banner = freshBanner();
  dispatchBackendPacket({
    type: "warning_event", id: "ram_critical", level: "critical",
    message: "RAM usage is critically high.", model_id: "m1",
  });
  assert.equal(visibleBanners(banner).length, 1);

  const dismissBtn = registry["aria-warning-banner-container"]._children[0]._children.find((c) => c.tagName === "button");
  dismissBtn.click();
  assert.equal(visibleBanners(banner).length, 0);

  dispatchBackendPacket({
    type: "warning_event", id: "ram_critical", level: "critical",
    message: "RAM usage is critically high.", model_id: "m1",
  });
  assert.equal(visibleBanners(banner).length, 1, "critical warnings must override the dismiss state");
});

test("the banner is hidden entirely outside the Chat panel", () => {
  const banner = freshBanner();
  dispatchBackendPacket({
    type: "warning_event", id: "auto_balance_active", level: "normal",
    message: "Auto-balance is active.", model_id: "m1",
  });
  assert.equal(visibleBanners(banner).length, 1);

  global.window.Router.currentPanel = "models";
  navigatePanelListeners.forEach((cb) => cb());
  assert.equal(visibleBanners(banner).length, 0, "banner must not render outside the Chat panel");

  global.window.Router.currentPanel = "chat";
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
