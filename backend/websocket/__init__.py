# backend/websocket/__init__.py
"""
WebSocket backend package for ARIA Lite.

Exposes:
- WebSocketHandler: main handler for frontend WebSocket connections
"""

from .handlers import WebSocketHandler

__all__ = ["WebSocketHandler"]
