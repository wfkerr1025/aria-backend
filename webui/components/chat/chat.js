// components/chat/chat.js
// ARIA Lite Chat Module — Unified Dispatcher Version

function chatLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("Chat", msg);
    }
  } catch (err) {
    console.error("[Chat LOG ERROR]", err);
  }
}

chatLog("=== CHAT MODULE LOADED ===");

const Chat = {
  init() {
    chatLog("Chat.init() called.");

    console.log(
      "[Chat] INIT — chat-history exists:",
      !!document.getElementById("chat-history")
    );

    this.cache();
    this.bindUI();

    chatLog("Chat subsystem ready.");
    console.log("[Chat] Ready");
  },

  // Cache DOM elements
  cache() {
    chatLog("Caching DOM elements.");

    this.history = document.getElementById("chat-history");
    this.input = document.getElementById("chat-input");
    this.sendBtn = document.getElementById("chat-send-btn");
    this.typingIndicator = document.getElementById("typing-indicator");

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
    }
  },

  // Bind UI events
  bindUI() {
    chatLog("Binding UI events.");

    if (!this.sendBtn || !this.input) {
      console.error("[Chat] bindUI() aborted — missing elements");
      chatLog("ERROR: bindUI aborted — missing elements.");
      return;
    }

    this.sendBtn.addEventListener("click", () => {
      chatLog("Send button clicked.");
      this.sendMessage();
    });

    this.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        chatLog("Enter pressed → sending message.");
        this.sendMessage();
      }
    });

    this.input.addEventListener("input", () => {
      chatLog("User typing…");
      this.input.style.height = "auto";
      this.input.style.height = this.input.scrollHeight + "px";
    });

    console.log("[Chat] UI bound successfully");
    chatLog("UI bound successfully.");
  },

  /* -----------------------------------------------------------
     Unified Dispatcher Entry Point
     -----------------------------------------------------------
     All backend packets arrive here via:
     window.ARIA_DISPATCH(packet)
  ----------------------------------------------------------- */
  handlePacket(packet) {
    const type = packet?.type;
    const payload = packet?.payload;

    chatLog("handlePacket() received: " + JSON.stringify(packet));

    // Copilot-style chat_response
    if (type === "chat_response") {
      const text = payload?.text || "";
      chatLog("chat_response received. Text length: " + text.length);

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

    this.addMessage(text, "user");

    chatLog("Sending message to backend.");
    window.aria.sendToBackend({
      type: "chat_request",
      messages: [{ role: "user", content: text }],
    });

    this.showTyping();

    this.input.value = "";
    this.input.style.height = "auto";
  },

  // Add transcript-style message
  addMessage(text, sender) {
    chatLog(`addMessage() sender=${sender}, length=${text.length}`);

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
    line.innerHTML = DOMPurify.sanitize(marked.parse(text));

    group.appendChild(header);
    group.appendChild(senderLabel);
    group.appendChild(line);

    this.history.appendChild(group);
    this.history.scrollTop = this.history.scrollHeight;

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
    this.history.scrollTop = this.history.scrollHeight;

    chatLog("Tool message appended to history.");
  },

  showTyping() {
    chatLog("showTyping()");
    if (this.typingIndicator) {
      this.typingIndicator.textContent = "ARIA is typing...";
    }
  },

  hideTyping() {
    chatLog("hideTyping()");
    if (this.typingIndicator) {
      this.typingIndicator.textContent = "";
    }
  },
};

export default Chat;
chatLog("Chat exported.");
