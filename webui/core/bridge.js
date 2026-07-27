// core/bridge.js
export class Bridge {
    constructor(url = "ws://localhost:8765") {
        console.log("[Bridge] Loaded");

        this.url = url;
        this.ws = null;
        this.handlers = {};
    }

    connect() {
        console.log("[Bridge] Connecting to backend:", this.url);

        this.ws = new WebSocket(this.url);

        this.ws.onopen = () => {
            console.log("[Bridge] Connected");
        };

        this.ws.onerror = (err) => {
            console.error("[Bridge] Error:", err);
        };

        this.ws.onclose = () => {
            console.warn("[Bridge] Closed");
        };

        this.ws.onmessage = (event) => {
            let packet;

            try {
                packet = JSON.parse(event.data);
            } catch (e) {
                console.error("[Bridge] Invalid JSON:", e);
                return;
            }

            const { type, payload } = packet;

            if (this.handlers[type]) {
                this.handlers[type](payload);
            } else {
                console.warn("[Bridge] No handler for packet type:", type);
            }
        };
    }

    send(type, payload = {}) {
        const packet = { type, payload };
        console.log("[Bridge] SEND:", packet);

        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn("[Bridge] Cannot send, socket not open");
            return;
        }

        this.ws.send(JSON.stringify(packet));
    }

    on(type, callback) {
        this.handlers[type] = callback;
    }
}

export const bridge = new Bridge();
