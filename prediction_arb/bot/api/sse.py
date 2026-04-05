"""
SSEBroadcaster — fan-out Server-Sent Events to all connected subscribers.

Each subscriber gets its own asyncio.Queue. The broadcaster pushes formatted
SSE strings to every active queue. Disconnected subscribers are cleaned up
lazily when their queue is detected as closed/unreachable.

Heartbeat events are emitted every 15 seconds to keep connections alive
through proxies and load balancers.

Supported event types:
  opportunity_detected, position_opened, position_closed,
  risk_suspended, heartbeat
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

import structlog

log = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL = 15  # seconds

VALID_EVENT_TYPES = frozenset(
    {
        "opportunity_detected",
        "position_opened",
        "position_closed",
        "risk_suspended",
        "heartbeat",
    }
)


class SSEBroadcaster:
    """
    Fan-out SSE broadcaster using one asyncio.Queue per subscriber.

    Usage::

        broadcaster = SSEBroadcaster()
        asyncio.create_task(broadcaster.start_heartbeat())

        # In a route handler:
        async for chunk in broadcaster.subscribe():
            yield chunk

        # From anywhere in the bot:
        await broadcaster.publish("position_opened", {...})
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[str | None]] = []
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, event_type: str, data: dict) -> None:
        """
        Push an SSE-formatted message to all active subscriber queues.

        Disconnected subscribers (whose queues are full or closed) are
        removed from the active list.
        """
        if event_type not in VALID_EVENT_TYPES:
            log.warning("sse_unknown_event_type", event_type=event_type)

        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

        dead: list[asyncio.Queue[str | None]] = []
        for q in list(self._queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("sse_queue_full_dropping_subscriber")
                dead.append(q)

        for q in dead:
            self._remove_queue(q)

        log.debug("sse_published", event_type=event_type, subscribers=len(self._queues))

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """
        Async generator that yields SSE-formatted strings for one subscriber.

        Creates a dedicated queue, registers it, and yields messages until
        the generator is closed (client disconnects). Cleans up the queue
        on exit.
        """
        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=100)
        self._queues.append(q)
        log.info("sse_subscriber_connected", total=len(self._queues))

        try:
            while True:
                item = await q.get()
                if item is None:
                    # Sentinel: broadcaster is shutting down
                    break
                yield item
        except asyncio.CancelledError:
            pass
        finally:
            self._remove_queue(q)
            log.info("sse_subscriber_disconnected", total=len(self._queues))

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def start_heartbeat(self) -> None:
        """
        Background task: emit a heartbeat event every HEARTBEAT_INTERVAL seconds.

        Should be started as an asyncio task and runs until stop() is called.
        """
        self._running = True
        log.info("sse_heartbeat_started", interval_seconds=HEARTBEAT_INTERVAL)
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self._running:
                    break
                await self.publish(
                    "heartbeat",
                    {"ts": datetime.now(tz=timezone.utc).isoformat()},
                )
        except asyncio.CancelledError:
            pass
        finally:
            log.info("sse_heartbeat_stopped")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """
        Signal the heartbeat loop to stop and send a sentinel to all
        subscriber queues so they exit cleanly.
        """
        self._running = False
        for q in list(self._queues):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        log.info("sse_broadcaster_stopped", subscribers=len(self._queues))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _remove_queue(self, q: asyncio.Queue[str | None]) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass  # already removed

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)
