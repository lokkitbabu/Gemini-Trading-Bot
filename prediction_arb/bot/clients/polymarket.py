"""
PolymarketClient — read-only async client for the Polymarket prediction market.

Uses two base URLs:
  - Gamma API  (https://gamma-api.polymarket.com) — market metadata
  - CLOB API   (https://clob.polymarket.com)       — orderbooks / prices

No authentication is required for public endpoints.
No order-placement methods are implemented; the client is strictly read-only.

Optional WebSocket streaming (POLYMARKET_WS_ENABLED=true) subscribes to the
`book` channel and maintains an in-memory best-bid/ask state per token ID.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from prediction_arb.bot.clients.base import BaseClient

log = structlog.get_logger(__name__)

_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
_CLOB_BASE_URL = "https://clob.polymarket.com"
_POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polymarket CLOB batch limit
_BATCH_LIMIT = 500


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PolymarketOrderbook:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    depth_5pct: float


# ---------------------------------------------------------------------------
# In-memory best-bid/ask state for WebSocket streaming
# ---------------------------------------------------------------------------


class _InMemoryBBA:
    """Tracks best bid/ask for a single Polymarket token via WS updates."""

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        # Full bids/asks lists (populated on book snapshot)
        self.bids: list[dict[str, str]] = []
        self.asks: list[dict[str, str]] = []

    def apply_book_snapshot(self, bids: list[dict[str, str]], asks: list[dict[str, str]]) -> None:
        self.bids = bids
        self.asks = asks
        self.best_bid = max(float(b["price"]) for b in bids) if bids else None
        self.best_ask = min(float(a["price"]) for a in asks) if asks else None

    def apply_bba_update(self, best_bid: str, best_ask: str) -> None:
        self.best_bid = float(best_bid) if best_bid else None
        self.best_ask = float(best_ask) if best_ask else None

    def to_orderbook(self) -> PolymarketOrderbook:
        mid: float | None = None
        if self.best_bid is not None and self.best_ask is not None:
            mid = (self.best_bid + self.best_ask) / 2

        depth_5pct = 0.0
        if self.best_bid is not None:
            depth_5pct = sum(
                float(b["size"])
                for b in self.bids
                if self.best_bid - float(b["price"]) <= 0.05
            )

        return PolymarketOrderbook(
            token_id=self.token_id,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            mid=mid,
            depth_5pct=depth_5pct,
        )


# ---------------------------------------------------------------------------
# PolymarketClient
# ---------------------------------------------------------------------------


class PolymarketClient(BaseClient):
    """
    Read-only Polymarket client.

    Uses two separate httpx clients internally — one for the Gamma API and
    one for the CLOB API — both sharing the same retry / backoff logic from
    BaseClient via ``_request_clob()``.
    """

    platform = "polymarket"

    def __init__(
        self,
        ws_enabled: bool = False,
        timeout: float = 10.0,
    ) -> None:
        # BaseClient is initialised with the Gamma base URL; CLOB calls use
        # a second httpx client managed here.
        super().__init__(base_url=_GAMMA_BASE_URL, timeout=timeout)
        self._clob_http = httpx.AsyncClient(
            base_url=_CLOB_BASE_URL,
            timeout=httpx.Timeout(timeout),
        )
        self._ws_enabled = ws_enabled
        self._ws_state: dict[str, _InMemoryBBA] = {}
        self._ws_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Gamma API — market metadata
    # ------------------------------------------------------------------

    async def get_markets(self, **params: Any) -> list[dict[str, Any]]:
        """GET https://gamma-api.polymarket.com/markets"""
        response = await self._request(
            "GET", "/markets", params=params or None, endpoint_label="get_markets"
        )
        response.raise_for_status()
        data = response.json()
        # Gamma API returns either a list or {"markets": [...]}
        if isinstance(data, list):
            return data
        return data.get("markets", data)

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """GET https://gamma-api.polymarket.com/markets/{conditionId}"""
        response = await self._request(
            "GET",
            f"/markets/{condition_id}",
            endpoint_label="get_market",
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # CLOB API — orderbooks / prices
    # ------------------------------------------------------------------

    async def _request_clob(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        endpoint_label: str | None = None,
    ) -> httpx.Response:
        """
        Issue a request against the CLOB base URL using the same retry logic
        as BaseClient._request(), but with the CLOB httpx client.

        We temporarily swap self._http so the parent _request() method uses
        the CLOB client.
        """
        original_http = self._http
        self._http = self._clob_http
        try:
            return await self._request(
                method, path, json=json, endpoint_label=endpoint_label
            )
        finally:
            self._http = original_http

    async def get_orderbooks(self, token_ids: list[str]) -> list[PolymarketOrderbook]:
        """
        POST https://clob.polymarket.com/books

        Batches up to 500 token IDs per request.  Returns a list of
        PolymarketOrderbook objects.
        """
        results: list[PolymarketOrderbook] = []

        for i in range(0, len(token_ids), _BATCH_LIMIT):
            batch = token_ids[i : i + _BATCH_LIMIT]

            # Check WS cache first for tokens we have live data for
            rest_batch: list[str] = []
            for tid in batch:
                if self._ws_enabled and tid in self._ws_state:
                    results.append(self._ws_state[tid].to_orderbook())
                else:
                    rest_batch.append(tid)

            if not rest_batch:
                continue

            response = await self._request_clob(
                "POST",
                "/books",
                json={"token_ids": rest_batch},
                endpoint_label="get_orderbooks",
            )
            response.raise_for_status()
            raw_list: list[dict[str, Any]] = response.json()

            for item in raw_list:
                token_id = item.get("asset_id", item.get("token_id", ""))
                bids: list[dict[str, str]] = item.get("bids", [])
                asks: list[dict[str, str]] = item.get("asks", [])
                results.append(self._parse_orderbook(token_id, bids, asks))

        return results

    async def get_prices(self, token_ids: list[str]) -> dict[str, float]:
        """
        POST https://clob.polymarket.com/prices

        Lightweight fast-loop fallback returning {token_id: mid_price}.
        """
        prices: dict[str, float] = {}

        for i in range(0, len(token_ids), _BATCH_LIMIT):
            batch = token_ids[i : i + _BATCH_LIMIT]
            response = await self._request_clob(
                "POST",
                "/prices",
                json={"token_ids": batch},
                endpoint_label="get_prices",
            )
            response.raise_for_status()
            data = response.json()
            # Response may be a list or a dict keyed by token_id
            if isinstance(data, list):
                for item in data:
                    tid = item.get("asset_id", item.get("token_id", ""))
                    price = item.get("price")
                    if tid and price is not None:
                        prices[tid] = float(price)
            elif isinstance(data, dict):
                for tid, price in data.items():
                    prices[tid] = float(price)

        return prices

    # ------------------------------------------------------------------
    # Orderbook parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_orderbook(
        token_id: str,
        bids: list[dict[str, str]],
        asks: list[dict[str, str]],
    ) -> PolymarketOrderbook:
        best_bid = max(float(b["price"]) for b in bids) if bids else None
        best_ask = min(float(a["price"]) for a in asks) if asks else None
        mid: float | None = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2

        depth_5pct = 0.0
        if best_bid is not None:
            depth_5pct = sum(
                float(b["size"])
                for b in bids
                if best_bid - float(b["price"]) <= 0.05
            )

        return PolymarketOrderbook(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            depth_5pct=depth_5pct,
        )

    # ------------------------------------------------------------------
    # Authentication refresh (no-op — Polymarket public endpoints need none)
    # ------------------------------------------------------------------

    async def _reauthenticate(self) -> None:
        log.info("reauthenticate_called", platform=self.platform)

    # ------------------------------------------------------------------
    # Read-only enforcement
    # ------------------------------------------------------------------

    async def place_order(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        raise NotImplementedError(
            "PolymarketClient is read-only. Order placement on Polymarket is not permitted."
        )

    # ------------------------------------------------------------------
    # Optional WebSocket streaming
    # ------------------------------------------------------------------

    async def start_ws(self, token_ids: list[str]) -> None:
        """Start WebSocket subscription for the `book` channel."""
        if not self._ws_enabled:
            return

        for tid in token_ids:
            if tid not in self._ws_state:
                self._ws_state[tid] = _InMemoryBBA(tid)

        self._ws_task = asyncio.create_task(self._ws_loop(token_ids))

    async def _ws_loop(self, token_ids: list[str]) -> None:
        try:
            import websockets  # type: ignore[import]
        except ImportError:
            log.warning(
                "websockets_not_installed",
                message="Install 'websockets' to enable Polymarket WebSocket streaming",
            )
            return

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(_POLY_WS_URL) as ws:
                    backoff = 1.0
                    subscribe_msg = {
                        "auth": {},
                        "type": "Market",
                        "assets_ids": token_ids,
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    log.info(
                        "polymarket_ws_subscribed",
                        token_count=len(token_ids),
                        channel="book",
                    )

                    async for raw in ws:
                        msg = json.loads(raw)
                        await self._handle_ws_message(msg)

            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "polymarket_ws_disconnected",
                    error=str(exc),
                    reconnect_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type")
        token_id = msg.get("asset_id", "")

        if token_id not in self._ws_state:
            self._ws_state[token_id] = _InMemoryBBA(token_id)

        state = self._ws_state[token_id]

        if event_type == "book":
            state.apply_book_snapshot(
                bids=msg.get("bids", []),
                asks=msg.get("asks", []),
            )
        elif event_type == "best_bid_ask":
            state.apply_bba_update(
                best_bid=msg.get("best_bid", ""),
                best_ask=msg.get("best_ask", ""),
            )

    async def stop_ws(self) -> None:
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._clob_http.aclose()
        await super().close()
