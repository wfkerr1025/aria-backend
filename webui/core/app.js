// core/app.js
// ARIA Lite — Application Bootstrap & Global Packet Dispatcher (v1.5)

import { bridge } from "./bridge.js";
import { Router } from "./router.js";
import Sidebar from "../components/sidebar/sidebar.js";
import { Theme } from "./theme-loader.js";

// UI subsystems
import Toast from "../components/toast/toast.js";
import Topbar from "../components/topbar/topbar.js";
import Chat from "../components/chat/chat.js";
import Statusbar from "../components/statusbar/statusbar.js";
import WarningBanner from "../components/warning_banner/warning_banner.js";
import ConnectionGuard from "../components/connection_guard/connection_guard.js";

// ======================================================
// Unified Logging Helper
// ======================================================
function appLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("App", msg);
    }
  } catch (err) {
    console.error("[App LOG ERROR]", err);
  }
}

// Unified cross-runtime logging (backend/logging_server.py) — distinct
// from appLog() above, which goes through Electron IPC. See
// webui/core/bridge.js / webui/components/chat/chat.js for the same
// helper, duplicated per-file by established convention in this repo.
const LOG_SERVER_URL = "http://127.0.0.1:5001/log";

function unifiedLog(subsystem, level, message, context = {}) {
  try {
    fetch(LOG_SERVER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subsystem, level: level || "INFO", message, context }),
    }).catch(() => {});
  } catch (_err) {
    // Swallow — logging must never break runtime.
  }
}

appLog("=== APP MODULE LOADED ===");

// -----------------------------------------------------------
// GLOBAL STATE (defensive init)
//
// bridge.js's Bridge constructor (imported above) already creates
// window.ARIA_STATE = { modelPref: "automatic" } as a module side effect
// before this file's own top-level code runs (ES module imports
// evaluate before the importing module's body) — so a plain
// `if (!window.ARIA_STATE)` guard here would never fire and
// suppressSafetyWarningsThisSession would never get added. Check the
// specific field instead of the whole object.
// -----------------------------------------------------------
window.ARIA_STATE = window.ARIA_STATE || {};
if (window.ARIA_STATE.suppressSafetyWarningsThisSession === undefined) {
  // Session-only, never persisted (per spec) — reset to false on every
  // fresh launch/reload. Read by chat.js when building an outgoing
  // chat_request (skipSafetyCheck) and by showSafetyWarning() below
  // (defensive: the backend should stop sending safety_warning at all
  // once skipSafetyCheck is honored, but a stray one must still not
  // pop the dialog back up).
  window.ARIA_STATE.suppressSafetyWarningsThisSession = false;
  appLog("ARIA_STATE initialized.");
}

// -----------------------------------------------------------
// HELPER: Safe panel dispatch
// -----------------------------------------------------------
const dispatchToPanel = (panel, packet) => {
  appLog("Dispatching packet to panel: " + JSON.stringify(packet));
  if (panel?.handlePacket) {
    panel.handlePacket(packet);
  }
};

//// -----------------------------------------------------------
// SAFETY WARNING → separate modal BrowserWindow
//
// The warning UI itself no longer lives in this window's DOM (see
// ARIA-Lite Desktop/warning.html) — it's a real, separate Electron
// window opened via window.aria.showSafetyWarning(), which keeps it
// from tangling with chat's own layout and lets this window's JS /
// WebSocket connection keep running underneath it while it's open.
// Its decision comes back asynchronously through onWarningChoice()
// below, wired once at bootstrap.
// -----------------------------------------------------------
function showSafetyWarning(packet) {
  // Defensive net: the backend should stop sending safety_warning at all
  // once a chat_request carries skipSafetyCheck (see chat.js), so this
  // shouldn't normally fire once suppression is on — but if one arrives
  // anyway (e.g. a request sent right before the flag was set), honor
  // the user's choice rather than popping the modal back up.
  if (window.ARIA_STATE?.suppressSafetyWarningsThisSession) {
    appLog("Safety warning suppressed for this session — not opening modal: " + JSON.stringify(packet));
    return;
  }

  appLog("Safety warning triggered: " + JSON.stringify(packet));

  if (!window.aria?.showSafetyWarning) {
    console.error("[App] window.aria.showSafetyWarning unavailable — preload.js not loaded, cannot show warning modal.");
    appLog("ERROR: window.aria.showSafetyWarning unavailable.");
    return;
  }

  window.aria.showSafetyWarning(packet);
  appLog("Safety warning modal requested.");
}

// The warning modal (warning.js) reports back exactly one decision per
// warning it showed: { action: "proceed" | "switch" | "ignore", modelId,
// suppress }. "proceed"/"switch" both need the ORIGINAL message —
// the one that triggered the warning — actually answered: the backend
// never generated a reply for it (it sent safety_warning instead of
// running inference), so this resends it via Chat._resendLastMessage()
// rather than just re-arming the model server-side and leaving the user
// hanging. See chat.js's sendMessage()/window.lastUserMessage and
// backend/websocket/handlers.py's load_model_override /
// switch_to_lighter_model (both arm a one-shot safety bypass for
// exactly this next request).
function bindWarningChoiceHandler() {
  if (!window.aria?.onWarningChoice) {
    console.error("[App] window.aria.onWarningChoice unavailable — preload.js not loaded.");
    appLog("ERROR: window.aria.onWarningChoice unavailable.");
    return;
  }

  window.aria.onWarningChoice((choice) => {
    appLog("Warning modal choice received: " + JSON.stringify(choice));
    unifiedLog("app", "INFO", "Safety warning modal choice", choice);

    if (choice.suppress) {
      window.ARIA_STATE.suppressSafetyWarningsThisSession = true;
      appLog("User enabled: suppress safety warnings for this session.");
      unifiedLog("app", "INFO", "Safety warnings suppressed for session");
    }

    if (choice.action === "proceed" && choice.modelId) {
      appLog("User selected Proceed Anyway.");
      bridge.send("load_model_override", { model_id: choice.modelId });
      window.chatPanel?._resendLastMessage?.();
      return;
    }

    if (choice.action === "switch" && choice.modelId) {
      appLog("User selected lighter model: " + choice.modelId);
      bridge.send("switch_to_lighter_model", { model_id: choice.modelId });
      window.chatPanel?._resendLastMessage?.();
      return;
    }

    appLog("User dismissed the safety warning without proceeding.");
  });
}

// -----------------------------------------------------------
// GLOBAL DISPATCHER (Copilot-style routing)
// -----------------------------------------------------------
window.ARIA_DISPATCH = (packet) => {
  appLog("ARIA_DISPATCH received packet: " + JSON.stringify(packet));

  const { type, payload } = packet;

  // SAFETY WARNING
  if (type === "safety_warning") {
    appLog("Dispatch: safety_warning");
    showSafetyWarning(packet);
    return;
  }

  // SYSTEM RESULT → mode change + toast + chat
  if (type === "system_result") {
    const mode = payload?.result?.mode || payload?.result?.value || "automatic";
    appLog("Dispatch: system_result → mode=" + mode);

    window.ARIA_STATE.modelPref = mode;

    dispatchToPanel(window.topbarPanel, {
      type: "mode_changed",
      mode
    });

    Toast.show(`Switched to ${mode.toUpperCase()} mode`, mode);
    appLog("Toast shown for mode change.");

    dispatchToPanel(window.chatPanel, {
      type: "chat_response",
      payload: { text: `Switched to ${mode.toUpperCase()} mode.` }
    });

    return;
  }

  // LEGACY / WRAPPED PACKETS
  if (type === "ipc_command_response") {
    appLog("Dispatch: ipc_command_response");

    const result = payload?.result;
    const content = result?.content;

    // Nested system_result
    if (content?.system_result) {
      const mode =
        content.system_result.mode ||
        content.system_result.value ||
        "automatic";

      appLog("Nested system_result → mode=" + mode);

      window.ARIA_STATE.modelPref = mode;

      dispatchToPanel(window.topbarPanel, {
        type: "mode_changed",
        mode
      });

      Toast.show(`Switched to ${mode.toUpperCase()} mode`, mode);

      dispatchToPanel(window.chatPanel, {
        type: "chat_response",
        payload: { text: `Switched to ${mode.toUpperCase()} mode.` }
      });

      return;
    }

    // Nested chat_reply
    if (content?.operation === "chat_reply" && content.reply) {
      appLog("Nested chat_reply received.");

      dispatchToPanel(window.chatPanel, {
        type: "chat_response",
        payload: { text: content.reply }
      });

      dispatchToPanel(window.chatPanel, {
        type: "chat_response",
        payload: { typing: false }
      });
    }
  }

  // TOOL RESULT
  if (type === "tool_result") {
    appLog("Dispatch: tool_result");
    dispatchToPanel(window.toolPanel, packet);
    Toast.show(payload?.summary || "Tool completed", "info");
  }

  // CHAT RESPONSE
  if (type === "chat_response") {
    appLog("Dispatch: chat_response");
    dispatchToPanel(window.chatPanel, packet);
  }

  // FALLBACK FAN-OUT
  appLog("Fallback fan-out dispatch.");
  dispatchToPanel(window.chatPanel, packet);
  dispatchToPanel(window.topbarPanel, packet);
  dispatchToPanel(window.toolPanel, packet);
};

// -----------------------------------------------------------
// BACKEND-PACKET → DISPATCHER
// -----------------------------------------------------------
window.addEventListener("backend-packet", (ev) => {
  appLog("backend-packet event received.");
  window.ARIA_DISPATCH(ev.detail);
});

// -----------------------------------------------------------
// APP INITIALIZATION (Bootstrap Only)
// -----------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  console.log("[App] Initializing ARIA Lite...");
  appLog("Initializing ARIA Lite…");

  Toast.init();
  appLog("Toast subsystem initialized.");

  Topbar.init();
  window.topbarPanel = Topbar;
  appLog("Topbar initialized.");

  Statusbar.init();
  window.Statusbar = Statusbar;
  appLog("Statusbar initialized.");

  ConnectionGuard.init();
  window.ConnectionGuard = ConnectionGuard;
  appLog("ConnectionGuard initialized.");

  WarningBanner.init();
  appLog("WarningBanner initialized.");

  Chat.init();
  window.chatPanel = Chat;
  appLog("Chat subsystem initialized.");

  Sidebar.init();
  window.Sidebar = Sidebar;
  appLog("Sidebar initialized.");

  window.Router = Router;
  Router.init(bridge);
  appLog("Router initialized.");

  bindWarningChoiceHandler();
  appLog("Warning modal choice handler bound.");

  console.log("[App] Connecting WebSocket...");
  appLog("Connecting IPC bridge…");
  bridge.connect();

  console.log("[App] Theme system ready:", Theme);
  appLog("Theme system ready.");
});
