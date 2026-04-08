"""WebSocket handler for explorer real-time notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# All currently connected WebSocket send callbacks.
_connected_clients: set[Any] = set()


async def on_connect(send: Any) -> None:
    """Called when a WebSocket client connects to /ws/explorer."""
    _connected_clients.add(send)
    logger.info("Explorer WS client connected (total: %d)", len(_connected_clients))


async def on_disconnect(send: Any) -> None:
    """Called when a WebSocket client disconnects."""
    _connected_clients.discard(send)
    logger.info("Explorer WS client disconnected (total: %d)", len(_connected_clients))


async def ws_explorer(msg: str, send: Any) -> None:
    """Handle incoming messages from the client (currently unused)."""
    # Clients don't send meaningful messages; this is a push-only channel.
    pass


async def notify_sync_complete() -> None:
    """Broadcast a sync-complete notification to all connected explorer clients.

    Called from the sync orchestrator after a successful data refresh.
    """
    if not _connected_clients:
        logger.debug("No connected WS clients to notify")
        return

    payload = '{"event":"sync_complete","message":"Andmebaas uuendatud"}'
    disconnected: list[Any] = []

    for send in _connected_clients:
        try:
            await send(payload)
        except Exception:
            logger.debug("Failed to send WS notification, marking client for removal")
            disconnected.append(send)

    for client in disconnected:
        _connected_clients.discard(client)

    logger.info(
        "Notified %d WS clients of sync completion",
        len(_connected_clients) + len(disconnected),
    )


def notify_sync_complete_sync() -> None:
    """Synchronous wrapper for notify_sync_complete.

    Can be called from synchronous code (e.g., the sync orchestrator).
    Schedules the async notification on the running event loop if available,
    otherwise creates a new one.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(notify_sync_complete())
    except RuntimeError:
        # No running event loop — run in a new one.
        asyncio.run(notify_sync_complete())


def register_ws_routes(app: Any) -> None:
    """Register WebSocket routes directly on the FastHTML *app* instance."""
    app.ws("/ws/explorer", conn=on_connect, disconn=on_disconnect)(ws_explorer)
