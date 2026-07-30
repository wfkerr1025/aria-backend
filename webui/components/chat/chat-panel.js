// components/chat/chat-panel.js
import Chat from "./chat.js";

const ChatPanel = {
  render() {
    const container = document.getElementById("panel-container");

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

    // Initialize chat logic (tool-aware)
    Chat.init();
  }
};

export default ChatPanel;
