// components/chat/chat.js
// ARIA Lite Chat Module — Path B (WebSocket Bridge)

import { bridge } from "../../core/bridge.js";

const Chat = {
  init() {
    console.log("[Chat] INIT — chat-history exists:", !!document.getElementById("chat-history"));
    console.log("[Chat] Initializing...");

    this.cache();
    this.bindUI();
    this.bindBridge();

    console.log("[Chat] Ready");
  },

  // Cache DOM elements
  cache() {
    this.history = document.getElementById("chat-history");
    this.input = document.getElementById("chat-input");
    this.sendBtn = document.getElementById("chat-send-btn");
    this.typingIndicator = document.getElementById("typing-indicator");

    console.log("[Chat] CACHE RESULT:", {
      history: !!this.history,
      input: !!this.input,
      sendBtn: !!this.sendBtn,
      typingIndicator: !!this.typingIndicator
    });

    if (!this.history || !this.input || !this.sendBtn) {
      console.error("[Chat] Missing required DOM elements");
    }
  },

  // Bind UI events
  bindUI() {
    if (!this.sendBtn || !this.input) {
      console.error("[Chat] bindUI() aborted — missing elements");
      return;
    }

    // Send button
    this.sendBtn.addEventListener("click", () => this.sendMessage());

    // Enter to send, Shift+Enter for newline
    this.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        this.sendMessage();
      }
    });

    // Auto-expanding input
    this.input.addEventListener("input", () => {
      this.input.style.height = "auto";
      this.input.style.height = this.input.scrollHeight + "px";
    });

    console.log("[Chat] UI bound successfully");
  },

  // Bind backend events (Path B Bridge)
  bindBridge() {
    if (!bridge) {
      console.warn("[Chat] Bridge not found");
      return;
    }

    // ARIA typing indicator
    bridge.on("chat_typing", () => {
      this.showTyping();
    });

    // Normal assistant response
    bridge.on("chat_response", (payload) => {
      this.hideTyping();

      let msg = payload.text;

      // Unwrap structured assistant messages
      if (msg && typeof msg === "object") {
        if (msg.type === "assistant" && msg.mode === "text") {
          msg = msg.content;
        }
      }

      this.addMessage(msg || "", "aria");
    });

    // Tool result packets
    bridge.on("tool_result", (payload) => {
      this.hideTyping();
      this.addToolMessage(payload);
    });

    console.log("[Chat] Bridge bound successfully");
  },

  // Send user message
  sendMessage() {
    const text = this.input.value.trim();
    if (!text) return;

    this.addMessage(text, "user");

    // Path B packet format — backend handles agent loop + tools
    bridge.send("chat_request", { text });

    this.input.value = "";
    this.input.style.height = "auto";
  },

  // Add transcript-style message
  addMessage(text, sender) {
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
        ? (window.ariaName || "Aria")
        : (window.userName || "You");

    const line = document.createElement("div");
    line.className = `message-line ${sender}`;

    // Safe markdown rendering
    line.innerHTML = DOMPurify.sanitize(marked.parse(text));

    group.appendChild(header);
    group.appendChild(senderLabel);
    group.appendChild(line);

    this.history.appendChild(group);
    this.history.scrollTop = this.history.scrollHeight;
  },

  // Add tool-result style message
  addToolMessage(payload) {
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

    line.innerHTML = `
      <div class="tool-header">Tool executed: ${toolName}</div>
      <pre class="tool-body">${DOMPurify.sanitize(JSON.stringify(result, null, 2))}</pre>
    `;

    group.appendChild(header);
    group.appendChild(senderLabel);
    group.appendChild(line);

    this.history.appendChild(group);
    this.history.scrollTop = this.history.scrollHeight;
  },

  showTyping() {
    if (this.typingIndicator) {
      this.typingIndicator.textContent = "ARIA is typing...";
    }
  },

  hideTyping() {
    if (this.typingIndicator) {
      this.typingIndicator.textContent = "";
    }
  }
};

export default Chat;
