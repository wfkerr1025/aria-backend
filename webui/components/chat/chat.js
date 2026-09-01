// components/chat/chat.js
// ARIA Lite Chat Module — Unified Dispatcher Version

import { bridge } from "../../core/bridge.js";
import { IPC } from "../../core/ipc_schema.js";
import ConnectionGuard from "../connection_guard/connection_guard.js";

function chatLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Chat", msg);
    }
  } catch (err) {
    console.error("[Chat LOG ERROR]", err);
  }
}

// Unified cross-runtime logging (backend/logging_server.py,
// http://127.0.0.1:5001/log) — see webui/core/bridge.js for the same
// helper. Never throws — a log call must never be able to break the UI.
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

chatLog("=== CHAT MODULE LOADED ===");

const CHAT_DOM_POLL_INTERVAL_MS = 50;
const CHAT_DOM_TIMEOUT_MS = 5000;

// Mirrors backend/core/conversation_manager.py's DEFAULT_MAX_HISTORY_MESSAGES —
// keep in sync so the client's "what will actually get sent" reasoning
// matches what the server will keep after its own trim.
const MAX_HISTORY_MESSAGES = 12;
// Hard cap on locally-retained history (independent of MAX_HISTORY_MESSAGES,
// which only bounds what's *sent*) so a very long session's array can't
// grow unbounded in memory.
const MAX_RETAINED_HISTORY = 200;

function newConversationId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const Chat = {
  _ready: false,
  _pollTimer: null,
  // { requestId, buffer, line } for the in-progress streamed aria bubble,
  // or null when nothing is currently streaming.
  _activeStream: null,

  // Full accumulated turn history for the CURRENT session (both roles).
  // Always the same array object as this._sessions[current].history —
  // see switchToSession()/startNewChat(). Bounded by MAX_RETAINED_HISTORY;
  // what actually gets *sent* per turn is governed separately by
  // _multiTurn / MAX_HISTORY_MESSAGES.
  _conversationHistory: [],
  _conversationId: null,
  // false = topic-drift guard: only the latest user message is sent.
  // true = send the last MAX_HISTORY_MESSAGES turns for real context.
  _multiTurn: true,

  // All chat sessions created this app run — [{ id, history, label,
  // createdAt }] — powers the Chat Menu dropdown. Session-only, like
  // window.ARIA_STATE; not persisted across an app restart.
  _sessions: [],
  _menuOpen: false,

  // Router.navigate() (webui/core/router.js) replaces #panel-container's
  // entire innerHTML with a fresh fetch of chat.html every time the user
  // navigates to the chat panel — including navigating BACK to it after
  // visiting another page. That destroys every DOM node this.cache()
  // captured (this.history, this.input, this.sendBtn, ...), so init()
  // must re-run cache()+bindUI() on every call, not just the first —
  // the previous `if (this._ready) return` guard here left those
  // references pointing at detached nodes after any panel round-trip:
  // clicks on the *new* New Chat/Send buttons hit no listener at all,
  // and any message render call appended to the *old*, no-longer-visible
  // this.history. That's what actually looked like "chat breaks after
  // navigating" and "New Chat does nothing" — not lost state (the
  // conversationId/history/sessions below survive fine in this same
  // singleton object across navigations; only the DOM wiring didn't).
  init() {
    chatLog("Chat.init() called.");

    if (this._pollTimer !== null) {
      chatLog("Chat.init() called again — already waiting for DOM, ignoring.");
      return;
    }

    this._waitForDom(0);
  },

  _waitForDom(elapsedMs) {
    const history = document.getElementById("chat-history");
    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send-btn");

    if (history && input && sendBtn) {
      this._pollTimer = null;
      chatLog(`Chat DOM elements found after ${elapsedMs}ms — proceeding with init.`);
      this._completeInit();
      return;
    }

    if (elapsedMs >= CHAT_DOM_TIMEOUT_MS) {
      this._pollTimer = null;
      console.error("[Chat] DOM elements still missing after " + CHAT_DOM_TIMEOUT_MS + "ms — giving up.");
      chatLog(`ERROR: DOM elements still missing after ${CHAT_DOM_TIMEOUT_MS}ms timeout.`);
      unifiedLog("chat", "ERROR", "UI error: chat DOM elements not found within timeout", {
        history: !!history, input: !!input, sendBtn: !!sendBtn, elapsedMs,
      });
      return;
    }

    this._pollTimer = setTimeout(
      () => this._waitForDom(elapsedMs + CHAT_DOM_POLL_INTERVAL_MS),
      CHAT_DOM_POLL_INTERVAL_MS
    );
  },

  _completeInit() {
    this.cache();
    this.bindUI();

    // Batch 2.5 — a mid-disconnect navigation TO the Chat panel must
    // start with the input already disabled, not wait for the next
    // connection_status event (chat.html is a router-loaded panel;
    // #chat-input didn't exist until this.cache() just above ran).
    ConnectionGuard.applyCurrentState();

    const isFirstInit = !this._ready;

    if (isFirstInit) {
      this._conversationId = newConversationId();
      this._sessions = [{
        id: this._conversationId,
        history: this._conversationHistory,
        label: null,
        createdAt: Date.now(),
      }];
      this._ready = true;
      chatLog("Chat subsystem ready (first init). conversationId=" + this._conversationId);
      console.log("[Chat] Ready");
    } else {
      // Returning to the chat panel after navigating away — state
      // (sessions/history/conversationId) is untouched, but the DOM was
      // just replaced, so the transcript needs repainting into it.
      chatLog("Chat subsystem re-bound after panel navigation.");
      console.log("[Chat] Re-bound after navigation");
      this._repaintHistory();
    }

    this._renderSidebarChatList();

    unifiedLog("chat", "INFO", "Chat ready", {
      conversationId: this._conversationId, firstInit: isFirstInit,
    });
  },

  // Copilot/ChatGPT-style sidebar chat list (webui/components/sidebar/
  // sidebar.js) — the sidebar renders whatever it's given here and
  // delegates clicks back to switchToSession() below; this is the only
  // place that pushes state to it.
  _renderSidebarChatList() {
    window.Sidebar?.renderChatList?.(this._sessions, this._conversationId);
  },

  // Replay the current session's history into a freshly-loaded (empty)
  // #chat-history element — used after router.js re-injects chat.html so
  // the transcript reappears instead of staying stuck in the detached
  // old DOM. Rebuilds from data (_conversationHistory), not from saved
  // HTML, so it stays correct even if rendering logic changes later.
  _repaintHistory() {
    if (!this.history) return;

    this.history.innerHTML = "";
    for (const turn of this._conversationHistory) {
      const { line } = this._createMessageShell(turn.role === "user" ? "user" : "aria");
      this._renderMarkdownInto(line, turn.content, { repaint: true });
    }
    this.history.scrollTop = this.history.scrollHeight;

    chatLog("History repainted. turns=" + this._conversationHistory.length);
  },

  // Public toggle for Selective History Inclusion — no dedicated UI
  // control exists yet, but the policy itself (and its logging) is fully
  // wired, so a future toolbar switch just needs to call this.
  setMultiTurn(enabled) {
    this._multiTurn = !!enabled;
    chatLog("setMultiTurn(" + this._multiTurn + ")");
    unifiedLog("chat", "INFO", "multi_turn setting changed", {
      multiTurn: this._multiTurn, conversationId: this._conversationId,
    });
  },

  // Direct chat navigation (sidebar chat list is always visible, on
  // every page — see webui/components/sidebar/sidebar.js and
  // webui/index.html): true whenever the Chat panel isn't the one
  // currently mounted in #panel-container. When that's the case, this.history/
  // this.input/etc. are stale references to a DETACHED chat.html from
  // whenever Chat was last visible (or never cached at all) — writing
  // through them (this.history.innerHTML = ..., _repaintHistory()) would
  // silently mutate an invisible node instead of doing nothing useful,
  // which is exactly what used to make clicking a chat from another page
  // look broken ("you have to click Chat first"). Both startNewChat()
  // and switchToSession() below check this and, when true, navigate to
  // Chat instead of touching the DOM directly — Chat.init()'s existing
  // re-bind branch (see _completeInit() above) repaints from the
  // session state they just updated, once chat.html is actually mounted.
  _isChatPanelVisible() {
    return window.Router?.currentPanel === "chat";
  },

  // Context reset — new chat bubble: start a brand-new session (its own
  // history + conversationId), added to the sidebar chat list (see
  // webui/components/sidebar/sidebar.js), and tell the backend so its
  // per-connection conversation_id rotates too. The suppression flag
  // (window.ARIA_STATE) is untouched — it's app-wide and session-scoped
  // to the whole run, not per-chat.
  startNewChat() {
    chatLog("startNewChat() called.");

    const previousId = this._conversationId;
    const wasVisible = this._isChatPanelVisible();

    this._conversationHistory = [];
    this._activeStream = null;
    this._conversationId = newConversationId();

    this._sessions.push({
      id: this._conversationId,
      history: this._conversationHistory,
      label: null,
      createdAt: Date.now(),
    });

    if (wasVisible) {
      if (this.history) {
        this.history.innerHTML = "";
      }
      this.hideTyping();
    } else {
      chatLog("startNewChat() — not on the Chat panel, navigating there.");
      window.dispatchEvent(new CustomEvent("navigatePanel", { detail: "chat" }));
    }
    this._renderSidebarChatList();

    unifiedLog("chat", "INFO", "context_reset", {
      previousConversationId: previousId, newConversationId: this._conversationId,
    });

    bridge.send(IPC.CONTEXT_RESET, { conversationId: this._conversationId });
  },

  // Sidebar chat list — switch the visible transcript to a different
  // session created earlier this run (via startNewChat()). Re-points
  // _conversationHistory at that session's own array (so _pushHistory()
  // keeps mutating the right one), repaints the transcript from it, and
  // tells the backend to rotate its conversation_id to match.
  switchToSession(sessionId) {
    chatLog("switchToSession() called. id=" + sessionId);

    const session = this._sessions.find(s => s.id === sessionId);
    if (!session) {
      chatLog("switchToSession() — unknown session id: " + sessionId);
      return;
    }

    const alreadyActive = sessionId === this._conversationId;
    const wasVisible = this._isChatPanelVisible();

    if (alreadyActive && wasVisible) {
      // Genuinely nothing to do — already viewing exactly this session.
      return;
    }

    this._conversationHistory = session.history;
    this._conversationId = session.id;
    this._activeStream = null;

    if (wasVisible) {
      this.hideTyping();
      this._repaintHistory();
    } else {
      chatLog("switchToSession() — not on the Chat panel, navigating there.");
      window.dispatchEvent(new CustomEvent("navigatePanel", { detail: "chat" }));
    }
    this._renderSidebarChatList();

    unifiedLog("chat", "INFO", "switched_session", { conversationId: this._conversationId });
    bridge.send(IPC.CONTEXT_RESET, { conversationId: this._conversationId });
  },

  // Sidebar chat list's "⋮" menu → Delete Chat (webui/components/
  // sidebar/sidebar.js). There is no backend chat registry to clean up
  // — conversation_id is just a per-connection label
  // (backend/websocket/handlers.py never persists conversation history
  // keyed by it), so deleting a chat is purely this in-memory _sessions
  // array. If the deleted chat is the one currently open, falls through
  // to a fresh New Chat (same as clicking New Chat directly) so the user
  // is never left looking at a transcript with no session behind it —
  // that call also re-renders the sidebar and navigates to Chat if
  // needed, so nothing further is required in that branch.
  deleteSession(sessionId) {
    chatLog("deleteSession() called. id=" + sessionId);

    const index = this._sessions.findIndex(s => s.id === sessionId);
    if (index === -1) {
      chatLog("deleteSession() — unknown session id: " + sessionId);
      return;
    }

    const wasActive = sessionId === this._conversationId;
    this._sessions.splice(index, 1);

    unifiedLog("chat", "INFO", "session_deleted", { conversationId: sessionId, wasActive });

    if (wasActive) {
      this.startNewChat();
      return;
    }

    this._renderSidebarChatList();
  },

  // Sidebar chat list's "⋮" menu → Rename Chat. Pins a label that
  // overrides renderChatList()'s auto-derived "first user message" title
  // (webui/components/sidebar/sidebar.js).
  renameSession(sessionId, newLabel) {
    chatLog("renameSession() called. id=" + sessionId);

    const session = this._sessions.find(s => s.id === sessionId);
    if (!session) {
      chatLog("renameSession() — unknown session id: " + sessionId);
      return;
    }

    const trimmed = (newLabel || "").trim();
    if (!trimmed) {
      chatLog("renameSession() — empty label, ignoring.");
      return;
    }

    session.label = trimmed;
    unifiedLog("chat", "INFO", "session_renamed", { conversationId: sessionId });
    this._renderSidebarChatList();
  },

  // Chat Menu — repurposed from the session list (now the sidebar's
  // job, see _renderSidebarChatList()) into per-chat utilities.
  _toggleMenu() {
    this._menuOpen = !this._menuOpen;
    chatLog("Chat menu toggled → " + this._menuOpen);
    this._renderMenu();
  },

  _closeMenu() {
    if (!this._menuOpen) return;
    this._menuOpen = false;
    this._renderMenu();
  },

  _renderMenu() {
    if (!this.menuDropdown) return;

    this.menuDropdown.classList.toggle("open", this._menuOpen);
    if (!this._menuOpen) return;

    this.menuDropdown.innerHTML = "";

    const utilities = [
      { label: "Export Transcript", action: () => this.exportTranscript() },
      { label: "Print", action: () => this.printTranscript() },
      { label: "Summarize This Chat", action: () => this.summarizeChat() },
    ];

    utilities.forEach(({ label, action }) => {
      const item = document.createElement("button");
      item.className = "chat-menu-item";
      item.textContent = label;
      item.addEventListener("click", () => {
        this._closeMenu();
        action();
      });
      this.menuDropdown.appendChild(item);
    });
  },

  // Chat Menu utility — download the current session's transcript as
  // plain text. Electron's renderer can't write files directly without
  // extra IPC plumbing, so this uses the standard browser download-via-
  // anchor trick, which Electron's BrowserWindow supports natively.
  exportTranscript() {
    chatLog("exportTranscript() called.");

    const lines = this._conversationHistory.map(turn =>
      `[${turn.role === "user" ? (window.userName || "You") : (window.ariaName || "Aria")}]\n${turn.content}\n`
    );
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = `aria-chat-${this._conversationId}.txt`;
    a.click();
    URL.revokeObjectURL(url);

    unifiedLog("chat", "INFO", "Transcript exported", { conversationId: this._conversationId });
  },

  // Chat Menu utility — print the current transcript via the browser's
  // native print dialog, applied only to #chat-history via a scoped
  // print stylesheet (see chat.css's @media print rules) so the rest of
  // the app chrome doesn't show up on the page.
  printTranscript() {
    chatLog("printTranscript() called.");
    unifiedLog("chat", "INFO", "Transcript print requested", { conversationId: this._conversationId });
    window.print();
  },

  // Chat Menu utility — ask ARIA to summarize the conversation so far.
  // A real chat_request, not a client-side trick — the model actually
  // sees the transcript and writes the summary like any other reply.
  summarizeChat() {
    chatLog("summarizeChat() called.");

    if (!this._conversationHistory.length) {
      chatLog("summarizeChat() — nothing to summarize yet.");
      return;
    }

    const text = "Summarize this conversation so far in a few sentences.";
    window.lastUserMessage = text;
    this.addMessage(text, "user");
    this._pushHistory("user", text);

    unifiedLog("chat", "INFO", "Summarize requested", { conversationId: this._conversationId });

    this._setTurnRunning(true);
    bridge.send(IPC.CHAT_REQUEST, {
      messages: this._buildOutgoingMessages(),
      conversationId: this._conversationId,
      multiTurn: true,
      skipSafetyCheck: !!window.ARIA_STATE?.suppressSafetyWarningsThisSession,
    });

  },

  // Cache DOM elements
  cache() {
    chatLog("Caching DOM elements.");

    this.history = document.getElementById("chat-history");
    this.input = document.getElementById("chat-input");
    this.sendBtn = document.getElementById("chat-send-btn");
    this.typingIndicator = document.getElementById("typing-indicator");
    // "New Chat" used to also live here (chat.html's toolbar) — removed,
    // the sidebar's New Chat button (webui/index.html) is now the only
    // one, wired to this.startNewChat() via webui/components/sidebar/
    // sidebar.js instead.
    this.menuBtn = document.getElementById("chat-menu-btn");
    this.menuDropdown = document.getElementById("chat-menu-dropdown");

    chatLog(
      "CACHE RESULT: " +
        JSON.stringify({
          history: !!this.history,
          input: !!this.input,
          sendBtn: !!this.sendBtn,
          typingIndicator: !!this.typingIndicator
        })
    );

    if (!this.history || !this.input || !this.sendBtn) {
      console.error("[Chat] Missing required DOM elements");
      chatLog("ERROR: Missing required DOM elements.");
      unifiedLog("chat", "ERROR", "UI error: missing required DOM elements", {
        history: !!this.history, input: !!this.input, sendBtn: !!this.sendBtn,
      });
    }
  },

  // Bind UI events
  bindUI() {
    chatLog("Binding UI events.");

    if (!this.sendBtn || !this.input) {
      console.error("[Chat] bindUI() aborted — missing elements");
      chatLog("ERROR: bindUI aborted — missing elements.");
      unifiedLog("chat", "ERROR", "UI error: bindUI() aborted — missing elements");
      return;
    }

    this.sendBtn.addEventListener("click", () => {
      // One button, two jobs: Send while idle, Stop while a turn is
      // running. A separate Stop control would sit dead for almost
      // all of a session, and the moment you want it is exactly the
      // moment Send is useless.
      if (this.isTurnRunning()) {
        chatLog("Stop button clicked.");
        this.stopCurrentTurn();
        return;
      }
      chatLog("Send button clicked.");
      this.sendMessage();
    });

    if (this.menuBtn) {
      this.menuBtn.addEventListener("click", () => {
        chatLog("Chat Menu button clicked.");
        this._toggleMenu();
      });
    } else {
      chatLog("Chat Menu button not present in DOM — skipping wiring.");
    }

    this.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        // Enter always sends and never stops. Muscle memory would
        // otherwise cancel the turn it had just started.
        chatLog("Enter pressed → sending message.");
        this.sendMessage();
      }
      if (ev.key === "Escape" && this.isTurnRunning()) {
        ev.preventDefault();
        chatLog("Escape pressed → stopping the running turn.");
        this.stopCurrentTurn();
      }
    });

    this.input.addEventListener("input", () => {
      chatLog("User typing…");
      this.input.style.height = "auto";
      this.input.style.height = this.input.scrollHeight + "px";
    });

    console.log("[Chat] UI bound successfully");
    chatLog("UI bound successfully.");
    unifiedLog("chat", "INFO", "Chat UI bound successfully");
  },

  /* -----------------------------------------------------------
     Unified Dispatcher Entry Point
     -----------------------------------------------------------
     All backend packets arrive here via:
     window.ARIA_DISPATCH(packet)
  ----------------------------------------------------------- */
  handlePacket(packet) {
    try {
      this._handlePacketInner(packet);
    } catch (err) {
      console.error("[Chat] handlePacket() exception:", err);
      chatLog("ERROR: handlePacket() exception: " + err);
      unifiedLog("chat", "ERROR", "handlePacket() exception: " + err, { packetType: packet?.type });
    }
  },

  _handlePacketInner(packet) {
    const type = packet?.type;
    const payload = packet?.payload;

    chatLog("handlePacket() received: " + JSON.stringify(packet));

    // Live streaming packets from backend/core/streaming_engine.py.
    // These are flat ({type, modelId, requestId, token}), NOT wrapped in
    // a `payload` envelope like the ipc_router-style packets below — that
    // mismatch (this function only ever read `payload?.x`) is why
    // stream_token never did anything here even though bridge.js was
    // receiving and logging every one of them correctly.
    if (type === "status") {
      this._handleStatus(packet);
      return;
    }

    if (type === "progress") {
      this._handleProgress(packet);
      return;
    }

    if (type === "message_revised") {
      this._handleMessageRevised(packet);
      return;
    }

    if (type === IPC.STREAM_START) {
      this._handleStreamStart(packet);
      return;
    }

    if (type === IPC.STREAM_TOKEN) {
      this._handleStreamToken(packet);
      return;
    }

    if (type === IPC.STREAM_END) {
      this._handleStreamEnd(packet);
      return;
    }

    if (type === IPC.STREAM_ERROR) {
      this._handleStreamError(packet);
      return;
    }

    if (type === IPC.STREAM_CANCELLED) {
      chatLog("stream_cancelled received. requestId=" + packet.requestId);
      // The bubble itself is closed by the stream_end that follows.
      return;
    }

    if (type === IPC.CHAT_STOP_RESULT) {
      chatLog("chat_stop_result: stopped=" + packet.stopped);
      this._setTurnRunning(false);
      return;
    }

    // Backend-level IPC error (malformed packet, unknown model, a handler
    // exception, ...) — previously reached this function via the same
    // ARIA_DISPATCH fallback fan-out as everything else here, but nothing
    // rendered it: the user just saw the "typing…" indicator hang with no
    // explanation. See backend/ipc_errors.py for the "code" field.
    if (type === IPC.ERROR) {
      chatLog("error packet received: " + packet.message);
      unifiedLog("chat", "ERROR", "IPC error packet received", {
        message: packet.message, code: packet.code, requestType: packet.request_type,
      });
      this.hideTyping();
      if (typeof window.showToast === "function") {
        window.showToast(packet.message || "An error occurred.", "error");
      }
      return;
    }

    // Backend confirming its per-connection conversation_id rotated to
    // match the one startNewChat() already generated client-side.
    if (type === IPC.CONTEXT_RESET_ACK) {
      chatLog("context_reset_ack received. conversationId=" + packet.conversationId);
      unifiedLog("chat", "DEBUG", "context_reset_ack received", {
        conversationId: packet.conversationId,
      });
      return;
    }

    // Copilot-style chat_response
    if (type === "chat_response") {
      const text = payload?.text || "";
      chatLog("chat_response received. Text length: " + text.length);
      unifiedLog("chat", "INFO", "chat_response received", { textLength: text.length });

      if (text) {
        this.hideTyping();
        this.addMessage(text, "aria");
      }
      return;
    }

    // Copilot-style system_result
    if (type === "system_result") {
      const summary = payload?.summary || "System updated";
      chatLog("system_result received: " + summary);

      this.hideTyping();
      this.addMessage(summary, "aria");
      return;
    }

    // Copilot-style tool_result
    if (type === "tool_result") {
      chatLog("tool_result received.");
      this.hideTyping();
      this.addToolMessage(payload || packet);
      return;
    }

    // Legacy ipc_command_response
    if (type === "ipc_command_response") {
      chatLog("ipc_command_response received.");

      const op = payload?.operation;

      // Direct chat_reply
      if (op === "chat_reply") {
        const reply = payload.reply;
        chatLog("chat_reply received. Length: " + (reply?.length || 0));

        if (reply) {
          this.hideTyping();
          this.addMessage(reply, "aria");
        }
        return;
      }

      // LLM wrapper → chat_reply
      if (op === "llm") {
        const content = payload?.result?.content;
        if (content?.operation === "chat_reply") {
          const reply = content.reply;
          chatLog("LLM chat_reply received. Length: " + (reply?.length || 0));

          if (reply) {
            this.hideTyping();
            this.addMessage(reply, "aria");
          }
          return;
        }
      }

      return;
    }
  },

  // Send user message through IPC → Electron → Backend → LLM
  sendMessage() {
    const text = this.input.value.trim();
    chatLog("sendMessage() called. Length: " + text.length);

    if (!text) {
      chatLog("sendMessage() aborted — empty message.");
      return;
    }

    // The triggering message for a possible safety_warning — kept
    // globally (not just in _conversationHistory) so warning.js's
    // "Proceed Anyway" / suggested-model flow can resend it without
    // reaching into Chat's internals. See _resendLastMessage().
    window.lastUserMessage = text;

    this.addMessage(text, "user");
    this._pushHistory("user", text);
    // The sidebar's title for this session (see sidebar.js's
    // renderChatList()) is derived from the first user turn — update it
    // as soon as one exists, rather than only on the next new-chat/
    // switch-session event, so a fresh "New Chat" session gets its real
    // title the moment its first message is sent.
    this._renderSidebarChatList();

    const messagesToSend = this._buildOutgoingMessages();

    chatLog("Sending message to backend.");
    unifiedLog("chat", "INFO", "Chat send", {
      textLength: text.length, conversationId: this._conversationId,
    });

    // FIXED: Use unified dispatcher instead of window.aria.sendToBackend
    this._setTurnRunning(true);
    bridge.send(IPC.CHAT_REQUEST, {
      messages: messagesToSend,
      conversationId: this._conversationId,
      multiTurn: this._multiTurn,
      // "Do Not Show This Message Anymore This Session" (webui/core/app.js) —
      // tells the backend to skip the safety gate entirely rather than
      // relying on the frontend to just not display a warning it still
      // receives (see backend/websocket/handlers.py::_handle_chat_request
      // and backend/server.py's /chat for where this is honored).
      skipSafetyCheck: !!window.ARIA_STATE?.suppressSafetyWarningsThisSession,
    });


    this.input.value = "";
    this.input.style.height = "auto";
  },

  // Re-send the message that triggered a safety_warning, after the user
  // resolves it via the warning modal (Proceed Anyway / switch to a
  // suggested model). The user's message and its history entry were
  // already added in sendMessage() when the original request was made —
  // the backend never generated a reply for it (it sent safety_warning
  // instead), so this must NOT call addMessage()/_pushHistory() again
  // (that would duplicate the bubble); it just re-issues the same
  // outgoing messages array so inference actually runs this time.
  // `overrides` lets the caller merge in flags like skipSafetyCheck;
  // the one-shot bypass armed server-side by load_model_override /
  // switch_to_lighter_model (see backend/websocket/handlers.py) covers
  // the rest without needing anything special here.
  _resendLastMessage(overrides = {}) {
    const last = this._conversationHistory[this._conversationHistory.length - 1];
    if (!last || last.role !== "user") {
      chatLog("_resendLastMessage() — no pending user turn to resend, skipping.");
      return;
    }

    chatLog("_resendLastMessage() called.");
    unifiedLog("chat", "INFO", "Resending message after safety warning", {
      conversationId: this._conversationId,
    });

    const messagesToSend = this._buildOutgoingMessages();

    this._setTurnRunning(true);
    bridge.send(IPC.CHAT_REQUEST, {
      messages: messagesToSend,
      conversationId: this._conversationId,
      multiTurn: this._multiTurn,
      skipSafetyCheck: !!window.ARIA_STATE?.suppressSafetyWarningsThisSession,
      ...overrides,
    });

  },

  // Append a turn to the locally-retained conversation log (both roles),
  // trimmed to MAX_RETAINED_HISTORY so a long session can't grow it
  // unbounded. This is independent of what gets *sent* per turn — see
  // _buildOutgoingMessages().
  _pushHistory(role, content) {
    this._conversationHistory.push({ role, content });
    if (this._conversationHistory.length > MAX_RETAINED_HISTORY) {
      this._conversationHistory.splice(0, this._conversationHistory.length - MAX_RETAINED_HISTORY);
    }
  },

  // Selective History Inclusion: multi-turn OFF sends only the latest
  // user message (topic-drift guard); multi-turn ON sends the last
  // MAX_HISTORY_MESSAGES turns. The server re-applies the same policy
  // defensively (backend/core/conversation_manager.py) — this client-side
  // pass is what actually shrinks the payload, not just a formality.
  _buildOutgoingMessages() {
    const full = this._conversationHistory;
    const kept = this._multiTurn ? full.slice(-MAX_HISTORY_MESSAGES) : full.slice(-1);

    const policyInfo = {
      mode: this._multiTurn ? "multi_turn" : "single_turn",
      incomingCount: full.length,
      keptCount: kept.length,
      droppedCount: Math.max(0, full.length - kept.length),
      maxMessages: MAX_HISTORY_MESSAGES,
      conversationId: this._conversationId,
    };

    chatLog("history_policy: " + JSON.stringify(policyInfo));
    unifiedLog("chat", "INFO", "history_policy: " + policyInfo.mode, policyInfo);

    return kept.map(({ role, content }) => ({ role, content }));
  },

  /* -----------------------------------------------------------
     Scroll position
     -----------------------------------------------------------

     The transcript follows new output only while the reader is at the
     bottom. Scrolling up during a long answer is the reader taking over,
     and it holds until they come back down -- see
     components/chat/scroll-policy.js for the rule and why it is a pure
     function.
  */

  // Whether the reader is currently following along. Recomputed from the
  // live position on every token rather than latched at stream_start, so
  // scrolling up mid-answer takes effect on the very next token.
  _scrollPinned: true,

  _scrollPolicy() {
    // The policy loads as a plain script (see index.html). If it is
    // missing, fall back to "follow only when already at the bottom",
    // which is the safe half of the rule -- never the old unconditional
    // scroll, because that is the behaviour being fixed.
    return (typeof window !== "undefined" && window.AriaScrollPolicy) || {
      shouldFollow: (metrics) => {
        const gap = metrics.scrollHeight - (metrics.scrollTop + metrics.clientHeight);
        const atBottom = gap <= 48;
        return { follow: atBottom, pinned: atBottom };
      },
    };
  },

  /* Put the top of a message at the top of the view.
   *
   * The replacement for `scrollTop = scrollHeight`. Scrolling to the
   * bottom of a long answer leaves the reader looking at its last line
   * with the whole thing above them, which is what "I have to scroll
   * every message" meant: not that nothing moved, but that it moved to
   * the wrong end.
   *
   * Offset arithmetic rather than scrollIntoView(): the transcript is a
   * scroll container inside a page that also scrolls, and
   * scrollIntoView moves whichever ancestor it likes -- which is how a
   * transcript scroll turns into the whole app jumping.
   */
  scrollToMessageTop(element) {
    if (!this.history || !element) return;

    // Measured against the container's CONTENT box, not its border box.
    // getBoundingClientRect().top is the outer edge; the first line of
    // text sits below the border and the padding, so aligning to the
    // outer edge leaves the message that far down the view -- measured
    // at 17px with a 1px border and 10px of padding.
    const box = this.history.getBoundingClientRect();
    const paddingTop = parseFloat(getComputedStyle(this.history).paddingTop) || 0;
    const contentTop = box.top + this.history.clientTop + paddingTop;

    const messageTop = element.getBoundingClientRect().top;
    const target = this.history.scrollTop + (messageTop - contentTop);

    // Clamped: a short final message cannot be brought to the top of the
    // view, and asking for it would otherwise scroll past the end.
    const maxScroll = this.history.scrollHeight - this.history.clientHeight;
    this.history.scrollTop = Math.max(0, Math.min(target, maxScroll));
  },

  // Scroll only if the reader has not moved away, and only to the top of
  // the message that prompted it.
  _maybeScroll(reason, element) {
    if (!this.history) return;

    const { follow, pinned } = this._scrollPolicy().shouldFollow(
      {
        scrollTop: this.history.scrollTop,
        scrollHeight: this.history.scrollHeight,
        clientHeight: this.history.clientHeight,
      },
      { pinned: this._scrollPinned },
      reason,
    );

    this._scrollPinned = pinned;
    if (!follow) return;

    if (element) {
      this.scrollToMessageTop(element);
    } else {
      // No element to aim at -- the old behaviour, kept for any caller
      // that has nothing better to offer.
      this.history.scrollTop = this.history.scrollHeight;
    }
  },

  /* -----------------------------------------------------------
     Turn status (backend/core/turn_status.py)

     Replaces the single undifferentiated "ARIA is typing..." that
     covered everything from intent detection through to the last token.
     A turn can spend seconds in retrieval and a web lookup before the
     first token exists, and showing "typing" for that reads as a hang.
     ----------------------------------------------------------- */

  _statusLabels: {
    listening: "",
    thinking: "Thinking\u2026",
    planning: "Planning\u2026",
    searching: "Searching the web\u2026",
    executing: "Running tools\u2026",
    synthesizing: "Putting it together\u2026",
    writing: "Writing\u2026",
    idle: "",
  },

  _handleStatus(packet) {
    const value = packet && packet.value;
    const label = this._statusLabels[value];
    if (label === undefined) {
      chatLog("status packet with unknown value: " + value);
      return;
    }

    // What the turn actually ran, straight off the backend's own record
    // -- so this says "searched the web" only when a search really
    // happened.
    let text = label;
    if (value === "writing" && packet.tool_runs && packet.tool_runs.length) {
      const searched = packet.tool_runs.indexOf("web_search") !== -1;
      if (searched) text = "Writing\u2026 (searched the web)";
    }

    this._lastStatus = value;
    if (this.typingIndicator) {
      this.typingIndicator.textContent = text;
    }
    chatLog("status: " + value);
  },

  /* -----------------------------------------------------------
     Live token streaming (stream_start / stream_token / stream_end /
     stream_error from backend/core/streaming_engine.py)
     ----------------------------------------------------------- */

  /* -----------------------------------------------------------
     Progress, and the revision at the end of a turn
     ----------------------------------------------------------- */

  // A live line saying what ARIA is doing: "loading nemo-12b",
  // "applying changes".
  //
  // Its own element, replaced each time, cleared when the stream ends --
  // NOT part of the message. Progress in the token stream would end up
  // in the transcript, in the history the next turn reads, and in the
  // text actions are parsed from.
  _handleProgress(packet) {
    const label = String(packet.label || "").trim();
    if (!label) return;

    if (!this._progressLine) {
      const line = document.createElement("div");
      line.className = "aria-progress-line";
      this.history?.appendChild(line);
      this._progressLine = line;
    }
    this._progressLine.textContent = label + "…";
    this._maybeScroll("progress");
  },

  _clearProgress() {
    this._progressLine?.remove();
    this._progressLine = null;
  },

  // Replace the streamed text with the checked text.
  //
  // Streaming shows tokens as they arrive and cannot take them back;
  // this is how the cleanup still applies. It arrives before stream_end,
  // so the corrected text is what _handleStreamEnd records in the
  // history the next turn reads.
  _handleMessageRevised(packet) {
    const text = String(packet.text || "");
    if (!text) return;

    if (!this._activeStream) {
      chatLog("message_revised with no active stream — ignored.");
      return;
    }

    chatLog("message_revised: replacing " + this._activeStream.buffer.length +
            " chars with " + text.length);
    this._activeStream.buffer = text;
    this._renderActiveStream();
  },

  /* ----------------------------------------------------------
     Send / Stop

     One button, two jobs. A separate Stop control would sit dead for
     almost all of a session, and the moment you want it is exactly
     the moment Send is useless -- so Send becomes Stop while a turn is
     running.

     This is only possible because chat turns stopped blocking the
     backend's message loop: a stop packet sent during a turn could
     not previously even be READ, because the loop was parked awaiting
     that same turn.
     ---------------------------------------------------------- */

  _setTurnRunning(running) {
    const wanted = !!running;
    if (this._turnRunning === wanted) return;
    this._turnRunning = wanted;

    const button = this.sendBtn || document.getElementById("chat-send-btn");
    if (!button) return;

    button.textContent = wanted ? "Stop" : "Send";
    button.classList.toggle("is-stopping", wanted);
    // Deliberately NOT disabled while running -- that is the whole
    // point. A disabled button is what made a slow turn look like a
    // frozen application.
    button.disabled = false;
  },

  // Called by the Send button. Which job it does depends on whether a
  // turn is in flight.
  stopCurrentTurn() {
    chatLog("chat_stop requested.");
    bridge.send(IPC.CHAT_STOP, {});
    // Not flipped back here: the button returns to Send when
    // chat_stop_result (or the stream_end that precedes it) arrives,
    // so it never claims to have stopped something it has not.
  },

  isTurnRunning() {
    return !!this._turnRunning;
  },

  // Begin a new incrementally-rendered aria bubble for this requestId.
  _handleStreamStart(packet) {
    chatLog("stream_start received. requestId=" + packet.requestId);
    unifiedLog("chat", "INFO", "stream_start received", {
      requestId: packet.requestId, modelId: packet.modelId,
    });

    // The indicator is driven by status packets now; the "writing" one
    // arrives with this stream_start. Left cleared here as a fallback for
    // a backend that sends no status packets at all.
    if (!this._lastStatus) this.hideTyping();
    const { line } = this._createMessageShell("aria");
    this._activeStream = { requestId: packet.requestId, buffer: "", line };
  },

  // Append one token to the active bubble's buffer and re-render it.
  // Called once per stream_token packet — this is what makes tokens
  // appear incrementally instead of all at once.
  _handleStreamToken(packet) {
    const token = packet.token || "";
    chatLog("stream_token received. len=" + token.length);
    unifiedLog("chat", "DEBUG", "stream_token received", {
      requestId: packet.requestId, tokenLength: token.length,
    });

    if (!this._activeStream || this._activeStream.requestId !== packet.requestId) {
      // A continuation reopens the bubble it is continuing.
      //
      // The backend routes an attached turn's tokens under the
      // SESSION's requestId -- the one the previous reply used -- so
      // a token arriving for the last closed stream is "carry on
      // where you left off", not a new answer. Without this the reply
      // would arrive as a second bubble with no relationship to the
      // first, which is the thing multi-turn streaming exists to fix.
      if (this._lastStream && this._lastStream.requestId === packet.requestId
          && this._lastStream.line && this._lastStream.line.isConnected) {
        chatLog("stream_token continues the previous bubble. requestId="
                + packet.requestId);
        this._activeStream = {
          requestId: packet.requestId,
          buffer: this._lastStream.buffer || "",
          line: this._lastStream.line,
        };
      } else {
        // No matching stream_start (missed, out of order, or a stale
        // requestId from a previous turn) — start a shell now so the
        // token still renders instead of being silently dropped.
        chatLog("stream_token with no matching active stream — starting bubble now.");
        const { line } = this._createMessageShell("aria");
        this._activeStream = { requestId: packet.requestId, buffer: "", line };
      }
    }

    this._activeStream.buffer += token;
    chatLog("Token appended. Buffer length=" + this._activeStream.buffer.length);
    unifiedLog("chat", "DEBUG", "Token appended to bubble", {
      requestId: packet.requestId, bufferLength: this._activeStream.buffer.length,
    });

    this._renderActiveStream();
  },

  // Finalize the bubble when the backend signals the stream is done.
  _handleStreamEnd(packet) {
    chatLog("stream_end received. requestId=" + packet.requestId);
    unifiedLog("chat", "INFO", "stream_end received", { requestId: packet.requestId });

    if (this._activeStream && this._activeStream.requestId === packet.requestId) {
      this._renderActiveStream();
      // Record ARIA's own reply so the next turn's multi-turn context
      // includes it — without this, multi-turn mode would only ever see
      // the user's side of the conversation.
      if (this._activeStream.buffer) {
        this._pushHistory("assistant", this._activeStream.buffer);
      }
      // Kept addressable, so a continuation can append to it rather
      // than opening a second reply. Only the LAST one: an older
      // bubble is not something "continue" could ever mean.
      this._lastStream = {
        requestId: this._activeStream.requestId,
        buffer: this._activeStream.buffer,
        line: this._activeStream.line,
      };
      this._activeStream = null;
      this._maybeScroll("stream_end");
    }
    this._clearProgress();
    this.hideTyping();
    this._setTurnRunning(false);
  },

  // Surface a mid-stream failure inline instead of leaving a dangling
  // "typing…" indicator with no explanation.
  _handleStreamError(packet) {
    chatLog("stream_error received: " + packet.message);
    unifiedLog("chat", "ERROR", "stream_error received: " + packet.message, {
      requestId: packet.requestId,
    });

    if (this._activeStream && this._activeStream.requestId === packet.requestId) {
      const errorText = (this._activeStream.buffer ? this._activeStream.buffer + "\n\n" : "") +
        `⚠ ${packet.message || "Streaming error"}`;
      this._renderMarkdownInto(this._activeStream.line, errorText, { requestId: packet.requestId });
      this._activeStream = null;
    } else {
      this.addMessage(`⚠ ${packet.message || "Streaming error"}`, "aria");
    }
    this.hideTyping();
  },

  // Re-render the active stream's accumulated buffer. Re-parsing the
  // whole buffer as markdown each token (rather than appending raw HTML)
  // keeps output correct even when a token lands mid-tag/mid-fence.
  _renderActiveStream() {
    const stream = this._activeStream;
    if (!stream) return;

    this._renderMarkdownInto(stream.line, stream.buffer, { requestId: stream.requestId });
    this._maybeScroll("token");

    chatLog("Bubble updated. requestId=" + stream.requestId);
    unifiedLog("chat", "DEBUG", "Bubble updated", {
      requestId: stream.requestId, bufferLength: stream.buffer.length,
    });
  },

  // Build an empty message bubble (header/sender/content line), append
  // it to history, and return the content line for the caller to fill
  // in — shared by addMessage() (fills it once) and the streaming
  // handlers above (fill it incrementally).
  _createMessageShell(sender) {
    const group = document.createElement("div");
    group.className = `message-group ${sender}`;

    const timestamp = new Date().toLocaleString();
    const header = document.createElement("div");
    header.className = `message-header ${sender}`;
    header.textContent = timestamp;

    const senderLabel = document.createElement("div");
    senderLabel.className = `message-sender ${sender}`;
    senderLabel.textContent =
      sender === "aria"
        ? window.ariaName || "Aria"
        : window.userName || "You";

    const line = document.createElement("div");
    line.className = `message-line ${sender}`;

    group.appendChild(header);
    group.appendChild(senderLabel);
    group.appendChild(line);

    this.history.appendChild(group);
    // A message the reader sent is the one case that always scrolls --
    // they just acted, and their own words are at the bottom. Anything
    // else respects where they are reading.
    // Aimed at the message that just arrived, from either side. The
    // user's own message always scrolls; an answer scrolls only for a
    // reader who is still at the bottom.
    this._maybeScroll(sender === "user" ? "new_message" : "assistant_message", group);

    return { group, line };
  },

  // Shared markdown-render-with-fallback used by both addMessage() and
  // the streaming bubble updater.
  _renderMarkdownInto(line, text, context = {}) {
    try {
      line.innerHTML = DOMPurify.sanitize(marked.parse(text));
    } catch (err) {
      console.error("[Chat] Bubble rendering failed:", err);
      chatLog("ERROR: Bubble rendering failed: " + err);
      unifiedLog("chat", "ERROR", "Bubble rendering failure: " + err, {
        ...context, textLength: text.length,
      });
      line.textContent = text;
    }
  },

  // Add transcript-style message
  addMessage(text, sender) {
    chatLog(`addMessage() sender=${sender}, length=${text.length}`);

    const { line } = this._createMessageShell(sender);
    this._renderMarkdownInto(line, text, { sender });

    // _createMessageShell already applied the policy for this sender.
    chatLog("Message appended to history.");
  },

  // Add tool-result style message
  addToolMessage(payload) {
    chatLog("addToolMessage() called.");

    const group = document.createElement("div");
    group.className = "message-group tool";

    const timestamp = new Date().toLocaleString();
    const header = document.createElement("div");
    header.className = "message-header tool";
    header.textContent = timestamp;

    const senderLabel = document.createElement("div");
    senderLabel.className = "message-sender tool";
    senderLabel.textContent = "Tool";

    const line = document.createElement("div");
    line.className = "message-line tool";

    const toolName = payload.tool || payload.task || "unknown";
    const result = payload.result ?? payload.data ?? payload;

    chatLog("Tool executed: " + toolName);

    line.innerHTML = `
      <div class="tool-header">Tool executed: ${toolName}</div>
      <pre class="tool-body">${DOMPurify.sanitize(
        JSON.stringify(result, null, 2)
      )}</pre>
    `;

    group.appendChild(header);
    group.appendChild(senderLabel);
    group.appendChild(line);

    this.history.appendChild(group);
    this._maybeScroll("token");

    chatLog("Tool message appended to history.");
  },

  // showTyping() is gone. It set a fixed "ARIA is typing..." the instant
  // the user pressed send, which was the only indicator available before
  // the backend described what it was doing -- and which flashed for a
  // moment before the first status packet replaced it with something
  // true. _handleStatus is the only writer of the indicator now, so what
  // it says always matches what the turn is actually doing.
  //
  // hideTyping() stays: clearing the indicator is still needed on the
  // paths that end a turn without a status packet (an IPC error, a
  // dropped connection).

  hideTyping() {
    chatLog("hideTyping()");
    if (this.typingIndicator) {
      this.typingIndicator.textContent = "";
    }
  },
};

export default Chat;
chatLog("Chat exported.");
