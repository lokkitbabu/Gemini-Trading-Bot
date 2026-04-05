"""
GeminiClient — read + write async client for the Gemini Predictions API.

All authenticated endpoints use HMAC-SHA384 request signing:
  - X-GEMINI-APIKEY:    the API key
  - X-GEMINI-PAYLOAD:   base64-encoded JSON payload
  - X-GEMINI-SIGNATURE: HMAC-SHA384 of the payload using the API secret

Maintains a persistent authenticated WebSocket for:
  1. Streaming {symbol}@bookTicker for all active matched markets
  2. Receiving orders@account fill notifications

Falls back to REST polling at PRICE_POLL_INTERVAL_SECONDS on WS disconnect,
reconnecting with exponential backoff.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import structlog

from prediction_arb.bot.clients.base import BaseClient

log = structlog.get_logger(__name__)

_GEMINI_BASE_URL = "https://api.gemini.com"
_GEMINI_WS_URL = "wss://ws.gemini.com"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class GeminiOrderbook:
    symbol: str
    best_bid: float | None
    best_ask: float | None
    yes_mid: float | None
    depth_3pct_usd: float


@dataclass
class GeminiOrder:
    order_id: str
    event_id: str
    side: str
    qty: float
    price: float
    status: str
    raw: dict[str, Any]


# ---------------------------------------------------------------------------
# GeminiClient
# ---------------------------------------------------------------------------


class GeminiClient(BaseClient):
    """
    Gemini Predictions API client (read + write).

    Parameters
    ----------
    api_key:
        Gemini API key.
    api_secret:
        Gemini API secret (used for HMAC-SHA384 signing).
    price_poll_interval:
        Seconds between REST orderbook polls when WebSocket is unavailable.
    on_fill:
        Optional async callback invoked when an ``orders@account`` fill
        notification is received via WebSocket.
    """

    platform = "gemini"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        price_poll_interval: float = 30.0,
        on_fill: Callable[[dict[str, Any]], Any] | None = None,
        base_url: str = _GEMINI_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(base_url=base_url, timeout=timeout)
        self._api_key = api_key
        self._api_secret = api_secret
        self._price_poll_interval = price_poll_interval
        self._on_fill = on_fill

        # In-memory best-bid/ask per symbol (populated by WS bookTicker)
        self._bba: dict[str, dict[str, float | None]] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_connected = False
        self._subscribed_symbols: list[str] = []

    # ------------------------------------------------------------------
    # HMAC-SHA384 signing
    # ------------------------------------------------------------------

    def _signed_headers(self, payload: dict[str, Any]) -> dict[str, str]:
        """
        Build HMAC-SHA384 signed headers for authenticated Gemini endpoints.

        The payload must include a ``nonce`` (millisecond timestamp) and the
        ``request`` path.
        """
        payload_json = json.dumps(payload)
        payload_b64 = base64.b64encode(payload_json.encode()).decode()
        signature = hmac.new(
            self._api_secret.encode(),
            payload_b64.encode(),
            hashlib.sha384,
        ).hexdigest()

        return {
            "X-GEMINI-APIKEY": self._api_key,
            "X-GEMINI-PAYLOAD": payload_b64,
            "X-GEMINI-SIGNATURE": signature,
            "Content-Type": "text/plain",
        }

    def _nonce(self) -> int:
        return int(time.time() * 1000)

    # ------------------------------------------------------------------
    # Public REST endpoints (no auth required)
    # ------------------------------------------------------------------

    async def get_events(self) -> list[dict[str, Any]]:
        """GET /v1/prediction-markets/events?status=active"""
        response = await self._request(
            "GET",
            "/v1/prediction-markets/events",
            params={"status": "active"},
            endpoint_label="get_events",
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        return data.get("events", data)

    async def get_event(self, event_id: str) -> dict[str, Any]:
        """GET /v1/prediction-markets/events/{eventId}"""
        response = await self._request(
            "GET",
            f"/v1/prediction-markets/events/{event_id}",
            endpoint_label="get_event",
        )
        response.raise_for_status()
        return response.json()

    async def get_orderbook(self, symbol: str) -> GeminiOrderbook:
        """
        GET /v1/book/{symbol}

        Computes best_bid, best_ask, yes_mid, and depth_3pct_usd.
        Falls back to WS in-memory state if available.
        """
        # Use WS cache if available
        if symbol in self._bba:
            cached = self._bba[symbol]
            best_bid = cached.get("best_bid")
            best_ask = cached.get("best_ask")
            yes_mid: float | None = None
            if best_bid is not None and best_ask is not None:
                yes_mid = (best_bid + best_ask) / 2
            return GeminiOrderbook(
                symbol=symbol,
                best_bid=best_bid,
                best_ask=best_ask,
                yes_mid=yes_mid,
                depth_3pct_usd=0.0,  # WS bookTicker doesn't carry depth
            )

        response = await self._request(
            "GET",
            f"/v1/book/{symbol}",
            endpoint_label="get_orderbook",
        )
        response.raise_for_status()
        data = response.json()
        return self._parse_orderbook(symbol, data)

    # ------------------------------------------------------------------
    # Authenticated REST endpoints
    # ------------------------------------------------------------------

    async def place_order(
        self,
        event_id: str,
        side: str,
        qty: float,
        price: float,
    ) -> GeminiOrder:
        """POST /v1/order/new"""
        payload = {
            "request": "/v1/order/new",
            "nonce": self._nonce(),
            "event_id": event_id,
            "side": side,
            "amount": str(qty),
            "price": str(price),
            "type": "exchange limit",
        }
        headers = self._signed_headers(payload)
        response = await self._request(
            "POST",
            "/v1/order/new",
            headers=headers,
            endpoint_label="place_order",
        )
        response.raise_for_status()
        raw = response.json()
        return GeminiOrder(
            order_id=str(raw.get("order_id", "")),
            event_id=event_id,
            side=side,
            qty=qty,
            price=price,
            status=raw.get("is_live", False) and "live" or raw.get("is_cancelled", False) and "cancelled" or "filled",
            raw=raw,
        )

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """POST /v1/order/cancel"""
        payload = {
            "request": "/v1/order/cancel",
            "nonce": self._nonce(),
            "order_id": order_id,
        }
        headers = self._signed_headers(payload)
        response = await self._request(
            "POST",
            "/v1/order/cancel",
            headers=headers,
            endpoint_label="cancel_order",
        )
        response.raise_for_status()
        return response.json()

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """POST /v1/order/status"""
        payload = {
            "request": "/v1/order/status",
            "nonce": self._nonce(),
            "order_id": order_id,
        }
        headers = self._signed_headers(payload)
        response = await self._request(
            "POST",
            "/v1/order/status",
            headers=headers,
            endpoint_label="get_order_status",
        )
        response.raise_for_status()
        return response.json()

    async def get_open_orders(self) -> list[dict[str, Any]]:
        """POST /v1/orders — used on startup to restore open positions."""
        payload = {
            "request": "/v1/orders",
            "nonce": self._nonce(),
        }
        headers = self._signed_headers(payload)
        response = await self._request(
            "POST",
            "/v1/orders",
            headers=headers,
            endpoint_label="get_open_orders",
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        return data.get("orders", [])

    # ------------------------------------------------------------------
    # Orderbook parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_orderbook(symbol: str, data: dict[str, Any]) -> GeminiOrderbook:
        bids: list[dict[str, str]] = data.get("bids", [])
        asks: list[dict[str, str]] = data.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        yes_mid: float | None = None
        if best_bid is not None and best_ask is not None:
            yes_mid = (best_bid + best_ask) / 2

        depth_3pct_usd = 0.0
        if best_ask is not None:
            depth_3pct_usd = sum(
                float(a["price"]) * float(a["amount"])
                for a in asks
                if float(a["price"]) - best_ask <= 0.03
            )

        return GeminiOrderbook(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            yes_mid=yes_mid,
            depth_3pct_usd=depth_3pct_usd,
        )

    # ------------------------------------------------------------------
    # Authentication refresh
    # ------------------------------------------------------------------

    async def _reauthenticate(self) -> None:
        """
        Refresh HMAC credentials.

        HMAC keys don't expire in the traditional sense; this is a hook for
        operators to rotate keys at runtime by updating the config and calling
        this method.
        """
        log.info("reauthenticate_called", platform=self.platform)

    # ------------------------------------------------------------------
    # Persistent WebSocket
    # ------------------------------------------------------------------

    async def start_ws(self, symbols: list[str]) -> None:
        """
        Start the persistent authenticated WebSocket.

        Subscribes to:
          - ``{symbol}@bookTicker`` for each symbol in ``symbols``
          - ``orders@account`` for fill notifications
        """
        self._subscribed_symbols = list(symbols)
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self) -> None:
        """WebSocket event loop with exponential backoff reconnect."""
        try:
            import websockets  # type: ignore[import]
        except ImportError:
            log.warning(
                "websockets_not_installed",
                message="Install 'websockets' to enable Gemini WebSocket streaming",
            )
            return

        backoff = 1.0
        while True:
            try:
                ws_url = self._build_ws_url()
                async with websockets.connect(ws_url) as ws:
                    backoff = 1.0
                    self._ws_connected = True
                    log.info(
                        "gemini_ws_connected",
                        symbols=self._subscribed_symbols,
                    )

                    async for raw in ws:
                        msg = json.loads(raw)
                        await self._handle_ws_message(msg)

            except Exception as exc:  # noqa: BLE001
                self._ws_connected = False
                log.warning(
                    "gemini_ws_disconnected",
                    error=str(exc),
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _build_ws_url(self) -> str:
        """Build authenticated WebSocket URL with HMAC-signed query params."""
        nonce = self._nonce()
        streams = [f"{s}@bookTicker" for s in self._subscribed_symbols]
        streams.append("orders@account")
        stream_str = "/".join(streams)

        payload = {
            "request": f"/ws/{stream_str}",
            "nonce": nonce,
        }
        payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        signature = hmac.new(
            self._api_secret.encode(),
            payload_b64.encode(),
            hashlib.sha384,
        ).hexdigest()

        return (
            f"{_GEMINI_WS_URL}/{stream_str}"
            f"?X-GEMINI-APIKEY={self._api_key}"
            f"&X-GEMINI-PAYLOAD={payload_b64}"
            f"&X-GEMINI-SIGNATURE={signature}"
        )

    async def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("type", msg.get("e", ""))

        # bookTicker update: {s: symbol, b: best_bid, a: best_ask, ...}
        if event_type == "bookTicker" or ("b" in msg and "a" in msg and "s" in msg):
            symbol = msg.get("s", "")
            if symbol:
                self._bba[symbol] = {
                    "best_bid": float(msg["b"]) if msg.get("b") else None,
                    "best_ask": float(msg["a"]) if msg.get("a") else None,
                }

        # Order fill notification
        elif event_type in ("executionReport", "fill", "order") and self._on_fill:
            try:
                await self._on_fill(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("fill_callback_error", error=str(exc))

    async def stop_ws(self) -> None:
        """Cancel the WebSocket background task."""
        self._ws_connected = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected
