// components/chat/chat-panel.js
import Chat from "./chat.js";

function chatPanelLog(msg) {
  try {
    if (window.aria && typeof window.aria.log === "function") {
      window.aria.log("ChatPanel", msg);
    }
  } catch (err) {
    console.error("[ChatPanel LOG ERROR]", err);
  }
}

chatPanelLog("=== CHAT PANEL MODULE LOADED ===");

const ChatPanel = {
  render() {
    chatPanelLog("Rendering chat panel…");

    const container = document.getElementById("panel-container");
    if (!container) {
      console.error("[ChatPanel] panel-container not found");
      chatPanelLog("ERROR: panel-container not found.");
      return;
    }

    chatPanelLog("Injecting chat panel HTML.");

    container.innerHTML = `
      <div id="chat-panel">

        <!-- Chat history -->
        <div id="chat-history" class="chat-history"></div>

        <!-- Typing indicator -->
        <div id="typing-indicator" class="typing-indicator"></div>

        <!-- Input area -->
        <div id="chat-input-area">
          <textarea id="chat-input" placeholder="Type a message..."></textarea>
          <button id="chat-send-btn" class="btn btn-primary">Send</button>
        </div>
      </div>
    `;

    chatPanelLog("Initializing Chat subsystem.");
    Chat.init();

    chatPanelLog("Chat panel fully rendered.");
  }
};

export default ChatPanel;
chatPanelLog("ChatPanel exported.");
