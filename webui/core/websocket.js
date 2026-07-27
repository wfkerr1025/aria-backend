// webui/core/websocket.js

class ARIAWebSocket {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.reconnectDelay = 1000;
    this.listeners = [];
  }

  connect() {
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      console.log("[WS] Connected to backend");
    };

    this.ws.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        console.warn("[WS] Received non‑JSON message:", event.data);
        return;
      }

      this.listeners.forEach(fn => fn(data));
    };

    this.ws.onclose = () => {
      console.warn("[WS] Connection closed. Reconnecting...");
      setTimeout(() => this.connect(), this.reconnectDelay);
    };

    this.ws.onerror = (err) => {
      console.error("[WS] Error:", err);
      this.ws.close();
    };
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    } else {
      console.warn("[WS] Tried to send but socket not open:", obj);
    }
  }

  onMessage(fn) {
    this.listeners.push(fn);
  }
}

export const ariaWS = new ARIAWebSocket("ws://localhost:8765");
