"""
KalshiClient — read-only async client for the Kalshi prediction market API.

Authenticated endpoints use RSA-SHA256 request signing.
No order-placement methods are implemented; calling any order method raises
NotImplementedError to enforce the read-only constraint.

Optional WebSocket streaming (KALSHI_WS_ENABLED=true) subscribes to the
`orderbook_delta` channel and maintains an in-memory orderbook state.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from prediction_arb.bot.clients.base import BaseClient

log = structlog.get_logger(__name__)

_KALSHI_BASE_URL = "https://api.elections.kalshi.com"
_KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class KalshiOrderbook:
    ticker: str
    best_yes_bid: Decimal | None
    best_yes_ask: Decimal | None
    yes_mid: Decimal | None
    depth_5pct: Decimal


# ---------------------------------------------------------------------------
# In-memory orderbook state for WebSocket streaming
# ---------------------------------------------------------------------------


class _InMemoryOrderbook:
    """Maintains a local copy of a Kalshi orderbook updated via WS deltas."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        # yes_dollars / no_dollars: dict[price_str, contracts_int]
        self.yes: dict[str, int] = {}
        self.no: dict[str, int] = {}

    def apply_snapshot(self, yes: list[list[str]], no: list[list[str]]) -> None:
        self.yes = {p: int(c) for p, c in yes}
        self.no = {p: int(c) for p, c in no}

    def apply_delta(self, price: str, delta: int, side: str) -> None:
        book = self.yes if side == "yes" else self.no
        current = book.get(price, 0)
        new_val = current + delta
        if new_val <= 0:
            book.pop(price, None)
        else:
            book[price] = new_val

    def to_orderbook(self) -> KalshiOrderbook:
        yes_sorted = sorted(self.yes.items(), key=lambda x: Decimal(x[0]))
        no_sorted = sorted(self.no.items(), key=lambda x: Decimal(x[0]))

        best_yes_bid: Decimal | None = None
        best_yes_ask: Decimal | None = None

        if yes_sorted:
            best_yes_bid = Decimal(yes_sorted[-1][0])
        if no_sorted:
            best_yes_ask = Decimal("1.00") - Decimal(no_sorted[-1][0])

        yes_mid: Decimal | None = None
        if best_yes_bid is not None and best_yes_ask is not None:
            yes_mid = (best_yes_bid + best_yes_ask) / 2

        depth_5pct = Decimal("0")
        if best_yes_bid is not None:
            for p, c in reversed(yes_sorted):
                if best_yes_bid - Decimal(p) <= Decimal("0.05"):
                    depth_5pct += Decimal(c)
                else:
                    break

        return KalshiOrderbook(
            ticker=self.ticker,
            best_yes_bid=best_yes_bid,
            best_yes_ask=best_yes_ask,
            yes_mid=yes_mid,
            depth_5pct=depth_5pct,
        )


# ---------------------------------------------------------------------------
# KalshiClient
# ---------------------------------------------------------------------------


class KalshiClient(BaseClient):
    """
    Read-only Kalshi API client.

    Parameters
    ----------
    api_key:
        Kalshi API key (used in RSA-signed requests).
    private_key_pem:
        PEM-encoded RSA private key for request signing.
    ws_enabled:
        When True, start a WebSocket connection for real-time orderbook deltas.
    """

    platform = "kalshi"

    def __init__(
        self,
        api_key: str = "",
        private_key_pem: str = "",
        ws_enabled: bool = False,
        base_url: str = _KALSHI_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(base_url=base_url, timeout=timeout)
        self._api_key = api_key
        self._private_key_pem = private_key_pem
        self._ws_enabled = ws_enabled
        # In-memory orderbooks keyed by ticker (populated by WS)
        self._ws_orderbooks: dict[str, _InMemoryOrderbook] = {}
        self._ws_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # RSA signing
    # ------------------------------------------------------------------

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """
        Build RSA-SHA256 signed headers for authenticated Kalshi endpoints.

        Returns a dict of extra headers to merge into the request.
        """
        if not self._api_key or not self._private_key_pem:
            return {}

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            log.warning(
                "cryptography_not_installed",
                message="Install 'cryptography' for Kalshi RSA signing",
            )
            return {}

        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}"

        private_key = serialization.load_pem_private_key(
            self._private_key_pem.encode(), password=None
        )
        signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    async def get_markets(self, series_ticker: str | None = None) -> list[dict[str, Any]]:
        """GET /trade-api/v2/markets — returns list of market dicts."""
        params: dict[str, Any] = {}
        if series_ticker:
            params["series_ticker"] = series_ticker

        response = await self._request("GET", "/trade-api/v2/markets", params=params)
        response.raise_for_status()
        return response.json().get("markets", [])

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """GET /trade-api/v2/markets/{ticker} — single market detail."""
        response = await self._request(
            "GET",
            f"/trade-api/v2/markets/{ticker}",
            endpoint_label="get_market",
        )
        response.raise_for_status()
        return response.json().get("market", {})

    async def get_orderbook(self, ticker: str) -> KalshiOrderbook:
        """
        GET /trade-api/v2/markets/{ticker}/orderbook

        Parses ``orderbook_fp`` to extract best_yes_bid, best_yes_ask,
        yes_mid, and depth_5pct.
        """
        # If WS is active and we have a live snapshot, use it
        if self._ws_enabled and ticker in self._ws_orderbooks:
            return self._ws_orderbooks[ticker].to_orderbook()

        response = await self._request(
            "GET",
            f"/trade-api/v2/markets/{ticker}/orderbook",
            endpoint_label="get_orderbook",
        )
        response.raise_for_status()
        data = response.json()
        return self._parse_orderbook_fp(ticker, data.get("orderbook", data))

    async def get_series(self) -> list[dict[str, Any]]:
        """GET /trade-api/v2/series — for slow-loop market discovery."""
        response = await self._request("GET", "/trade-api/v2/series")
        response.raise_for_status()
        return response.json().get("series", [])

    # ------------------------------------------------------------------
    # Orderbook parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_orderbook_fp(ticker: str, data: dict[str, Any]) -> KalshiOrderbook:
        """
        Parse Kalshi's ``orderbook_fp`` format.

        yes_dollars / no_dollars are lists of [price_str, contracts_str]
        sorted ascending by price.  Best bid = last element.
        """
        yes_dollars: list[list[str]] = data.get("yes_dollars", data.get("yes", []))
        no_dollars: list[list[str]] = data.get("no_dollars", data.get("no", []))

        best_yes_bid: Decimal | None = None
        best_yes_ask: Decimal | None = None

        if yes_dollars:
            best_yes_bid = Decimal(yes_dollars[-1][0])
        if no_dollars:
            best_yes_ask = Decimal("1.00") - Decimal(no_dollars[-1][0])

        yes_mid: Decimal | None = None
        if best_yes_bid is not None and best_yes_ask is not None:
            yes_mid = (best_yes_bid + best_yes_ask) / 2

        depth_5pct = Decimal("0")
        if best_yes_bid is not None:
            for p, c in reversed(yes_dollars):
                if best_yes_bid - Decimal(p) <= Decimal("0.05"):
                    depth_5pct += Decimal(c)
                else:
                    break

        return KalshiOrderbook(
            ticker=ticker,
            best_yes_bid=best_yes_bid,
            best_yes_ask=best_yes_ask,
            yes_mid=yes_mid,
            depth_5pct=depth_5pct,
        )

    # ------------------------------------------------------------------
    # Authentication refresh
    # ------------------------------------------------------------------

    async def _reauthenticate(self) -> None:
        """
        Refresh Kalshi credentials.

        For RSA-key-based auth the key itself doesn't expire; this is a
        no-op unless a token-based flow is added in the future.
        """
        log.info("reauthenticate_called", platform=self.platform)

    # ------------------------------------------------------------------
    # Read-only enforcement
    # ------------------------------------------------------------------

    async def place_order(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        raise NotImplementedError(
            "KalshiClient is read-only. Order placement on Kalshi is not permitted."
        )

    async def cancel_order(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        raise NotImplementedError(
            "KalshiClient is read-only. Order cancellation on Kalshi is not permitted."
        )

    # ------------------------------------------------------------------
    # Optional WebSocket streaming
    # ------------------------------------------------------------------

    async def start_ws(self, tickers: list[str]) -> None:
        """
        Start the WebSocket client for ``orderbook_delta`` channel.

        Initialises in-memory orderbook state for each ticker and begins
        applying snapshot / delta messages.  Runs as a background asyncio task.
        """
        if not self._ws_enabled:
            return

        for ticker in tickers:
            if ticker not in self._ws_orderbooks:
                self._ws_orderbooks[ticker] = _InMemoryOrderbook(ticker)

        self._ws_task = asyncio.create_task(self._ws_loop(tickers))

    async def _ws_loop(self, tickers: list[str]) -> None:
        """WebSocket event loop with reconnect on disconnect."""
        try:
            import websockets  # type: ignore[import]
        except ImportError:
            log.warning(
                "websockets_not_installed",
                message="Install 'websockets' to enable Kalshi WebSocket streaming",
            )
            return

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(_KALSHI_WS_URL) as ws:
                    backoff = 1.0  # reset on successful connect
                    subscribe_msg = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": tickers,
                        },
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    log.info(
                        "kalshi_ws_subscribed",
                        tickers=tickers,
                        channel="orderbook_delta",
                    )

                    async for raw in ws:
                        msg = json.loads(raw)
                        await self._handle_ws_message(msg)

            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "kalshi_ws_disconnected",
                    error=str(exc),
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        payload = msg.get("msg", {})
        ticker = payload.get("market_ticker")

        if ticker not in self._ws_orderbooks:
            self._ws_orderbooks[ticker] = _InMemoryOrderbook(ticker)

        ob = self._ws_orderbooks[ticker]

        if msg_type == "orderbook_snapshot":
            ob.apply_snapshot(
                yes=payload.get("yes", []),
                no=payload.get("no", []),
            )
        elif msg_type == "orderbook_delta":
            ob.apply_delta(
                price=payload["price"],
                delta=int(payload["delta"]),
                side=payload["side"],
            )

    async def stop_ws(self) -> None:
        """Cancel the WebSocket background task."""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
