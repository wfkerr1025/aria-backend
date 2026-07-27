// components/chat/chat-panel.js
import Chat from "./chat.js";

const ChatPanel = {
  render() {
    const container = document.getElementById("panel-container");

    container.innerHTML = `
      <div id="chat-panel">
        <div id="chat-history" class="chat-history"></div>

        <div id="typing-indicator" class="typing-indicator"></div>

        <div id="chat-input-area">
          <textarea id="chat-input" placeholder="Type a message..."></textarea>
          <button id="chat-send-btn">Send</button>
        </div>
      </div>
    `;

    Chat.init();
  }
};

export default ChatPanel;
