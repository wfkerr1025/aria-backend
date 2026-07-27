// components/chat/chat.js
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

  bindUI() {
    if (!this.sendBtn || !this.input) {
      console.error("[Chat] bindUI() aborted — missing elements");
      return;
    }

    this.sendBtn.addEventListener("click", () => this.sendMessage());

    this.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        this.sendMessage();
      }
    });

    this.input.addEventListener("input", () => {
      this.input.style.height = "auto";
      this.input.style.height = this.input.scrollHeight + "px";
    });

    console.log("[Chat] UI bound successfully");
  },

  bindBridge() {
    if (!bridge) {
      console.warn("[Chat] Bridge not found");
      return;
    }

    bridge.on("chat_typing", () => {
      this.showTyping();
    });

    bridge.on("chat_response", (payload) => {
      this.hideTyping();
      this.addMessage(payload.text, "aria");
    });

    console.log("[Chat] Bridge bound successfully");
  },

  sendMessage() {
    const text = this.input.value.trim();
    if (!text) return;

    this.addMessage(text, "user");

    bridge.send("chat_request", { text });

    this.input.value = "";
    this.input.style.height = "auto";
  },

  addMessage(text, sender) {
    const group = document.createElement("div");
    group.className = `message-group ${sender}`;

    const timestamp = new Date().toLocaleString();
    const header = document.createElement("div");
    header.className = "message-header";
    header.textContent = timestamp;

    const senderLabel = document.createElement("div");
    senderLabel.className = `message-sender ${sender}`;
    senderLabel.textContent = sender === "aria" ? "Aria (AI):" : "You:";

    const line = document.createElement("div");
    line.className = `message-line ${sender}`;
    line.innerHTML = DOMPurify.sanitize(marked.parse(text));

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
