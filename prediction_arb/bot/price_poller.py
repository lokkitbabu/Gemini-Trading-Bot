"""
PricePoller — fast-loop orderbook polling for all active matched pairs.

For each active MatchedPair:
  - Fetches Kalshi orderbook (per-ticker REST)
  - Fetches Polymarket orderbooks (single batched POST up to 500 token IDs)
  - Fetches Gemini orderbook (per-symbol REST)

Persists each fetched orderbook as OrderbookSnapshot to DB (via StateStore stub)
and updates the in-memory OrderbookCache.

On per-market fetch failure: logs WARNING, retains previous snapshot with original
fetched_at, continues polling other markets.

Records arb_orderbook_fetch_duration_seconds histogram labeled by platform.
Emits WARNING when any platform's p95 fetch latency exceeds 5s over a rolling
5-minute window.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import structlog

from prediction_arb.bot.orderbook_cache import OrderbookCache, OrderbookSnapshot

log = structlog.get_logger(__name__)

# Rolling window for p95 latency tracking (5 minutes)
_P95_WINDOW_SECONDS = 300
_P95_THRESHOLD_SECONDS = 5.0

# Lazy import of metrics to avoid circular imports
try:
    from prediction_arb.bot.metrics import ORDERBOOK_FETCH_DURATION_SECONDS  # type: ignore[import]
except ImportError:  # pragma: no cover
    ORDERBOOK_FETCH_DURATION_SECONDS = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Rolling latency tracker for p95 warning
# ---------------------------------------------------------------------------


class _LatencyTracker:
    """Tracks fetch latencies in a rolling time window for p95 computation."""

    def __init__(self, window_seconds: float = _P95_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        # deque of (timestamp, latency_seconds)
        self._samples: deque[tuple[float, float]] = deque()

    def record(self, latency: float) -> None:
        now = time.monotonic()
        self._samples.append((now, latency))
        self._prune(now)

    def p95(self) -> float | None:
        now = time.monotonic()
        self._prune(now)
        if not self._samples:
            return None
        sorted_latencies = sorted(s[1] for s in self._samples)
        idx = max(0, int(len(sorted_latencies) * 0.95) - 1)
        return sorted_latencies[idx]

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()


# ---------------------------------------------------------------------------
# StateStore stub (wired in Group 7)
# ---------------------------------------------------------------------------


class _StateStoreStub:
    """Stub StateStore for persisting snapshots. Wired in Group 7."""

    async def save_orderbook_snapshot(self, snapshot: OrderbookSnapshot) -> None:
        pass


# ---------------------------------------------------------------------------
# PricePoller
# ---------------------------------------------------------------------------


class PricePoller:
    """
    Fast-loop orderbook poller for all active matched pairs.

    Parameters
    ----------
    kalshi_client:
        KalshiClient instance for fetching Kalshi orderbooks.
    poly_client:
        PolymarketClient instance for fetching Polymarket orderbooks.
    gemini_client:
        GeminiClient instance for fetching Gemini orderbooks.
    orderbook_cache:
        In-memory OrderbookCache to update after each fetch.
    state_store:
        StateStore (or stub) for persisting snapshots to DB.
    matched_pairs:
        List of active MatchedPairs updated by the slow loop.
    """

    def __init__(
        self,
        kalshi_client: Any,
        poly_client: Any,
        gemini_client: Any,
        orderbook_cache: OrderbookCache,
        state_store: Any = None,
        matched_pairs: list[Any] | None = None,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._gemini = gemini_client
        self._cache = orderbook_cache
        self._state_store = state_store or _StateStoreStub()
        self._matched_pairs: list[Any] = matched_pairs or []

        # Per-platform latency trackers
        self._latency_trackers: dict[str, _LatencyTracker] = {
            "kalshi": _LatencyTracker(),
            "polymarket": _LatencyTracker(),
            "gemini": _LatencyTracker(),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll_once(self) -> list[OrderbookSnapshot]:
        """
        Fetch orderbooks for all active matched pairs.

        Returns list of all snapshots fetched in this poll cycle.
        On per-market failure: logs WARNING, retains previous snapshot,
        continues polling other markets.
        """
        if not self._matched_pairs:
            return []

        snapshots: list[OrderbookSnapshot] = []

        # Collect all tickers/token_ids to batch where possible
        kalshi_tickers: list[str] = []
        poly_token_ids: list[str] = []
        gemini_symbols: list[str] = []

        for pair in self._matched_pairs:
            ref = pair.ref
            target = pair.target

            if ref.platform == "kalshi":
                kalshi_tickers.append(ref.id)
            elif ref.platform == "polymarket":
                poly_token_ids.append(ref.id)

            if target.platform == "gemini":
                gemini_symbols.append(target.id)

        # Deduplicate
        kalshi_tickers = list(dict.fromkeys(kalshi_tickers))
        poly_token_ids = list(dict.fromkeys(poly_token_ids))
        gemini_symbols = list(dict.fromkeys(gemini_symbols))

        # Fetch Kalshi orderbooks (per-ticker REST)
        kalshi_snapshots = await self._fetch_kalshi_orderbooks(kalshi_tickers)
        snapshots.extend(kalshi_snapshots)

        # Fetch Polymarket orderbooks (batched POST)
        poly_snapshots = await self._fetch_poly_orderbooks(poly_token_ids)
        snapshots.extend(poly_snapshots)

        # Fetch Gemini orderbooks (per-symbol REST)
        gemini_snapshots = await self._fetch_gemini_orderbooks(gemini_symbols)
        snapshots.extend(gemini_snapshots)

        # Persist and cache all snapshots
        for snapshot in snapshots:
            await self._cache.update(snapshot)
            await self._state_store.save_orderbook_snapshot(snapshot)

        # Check p95 latency thresholds
        self._check_p95_warnings()

        return snapshots

    # ------------------------------------------------------------------
    # Per-platform fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_kalshi_orderbooks(
        self, tickers: list[str]
    ) -> list[OrderbookSnapshot]:
        snapshots: list[OrderbookSnapshot] = []
        for ticker in tickers:
            snapshot = await self._fetch_kalshi_one(ticker)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    async def _fetch_kalshi_one(self, ticker: str) -> OrderbookSnapshot | None:
        start = time.monotonic()
        try:
            ob = await self._kalshi.get_orderbook(ticker)
            latency = time.monotonic() - start
            self._record_latency("kalshi", latency)

            # Convert Decimal fields to float
            best_bid = float(ob.best_yes_bid) if ob.best_yes_bid is not None else None
            best_ask = float(ob.best_yes_ask) if ob.best_yes_ask is not None else None
            yes_mid = float(ob.yes_mid) if ob.yes_mid is not None else None
            depth_5pct = float(ob.depth_5pct)

            return OrderbookSnapshot(
                platform="kalshi",
                ticker=ticker,
                best_bid=best_bid,
                best_ask=best_ask,
                yes_mid=yes_mid,
                depth_5pct=depth_5pct,
                depth_3pct_usd=0.0,
                volume_24h=None,
                fetched_at=datetime.now(tz=timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            latency = time.monotonic() - start
            self._record_latency("kalshi", latency)
            log.warning(
                "kalshi_orderbook_fetch_failed",
                ticker=ticker,
                error=str(exc),
            )
            # Retain previous snapshot with original fetched_at
            return None

    async def _fetch_poly_orderbooks(
        self, token_ids: list[str]
    ) -> list[OrderbookSnapshot]:
        if not token_ids:
            return []

        start = time.monotonic()
        try:
            orderbooks = await self._poly.get_orderbooks(token_ids)
            latency = time.monotonic() - start
            self._record_latency("polymarket", latency)

            snapshots: list[OrderbookSnapshot] = []
            for ob in orderbooks:
                yes_mid: float | None = None
                if ob.best_bid is not None and ob.best_ask is not None:
                    yes_mid = (ob.best_bid + ob.best_ask) / 2

                snapshots.append(
                    OrderbookSnapshot(
                        platform="polymarket",
                        ticker=ob.token_id,
                        best_bid=ob.best_bid,
                        best_ask=ob.best_ask,
                        yes_mid=yes_mid,
                        depth_5pct=ob.depth_5pct,
                        depth_3pct_usd=0.0,
                        volume_24h=None,
                        fetched_at=datetime.now(tz=timezone.utc),
                    )
                )
            return snapshots

        except Exception as exc:  # noqa: BLE001
            latency = time.monotonic() - start
            self._record_latency("polymarket", latency)
            log.warning(
                "polymarket_orderbook_fetch_failed",
                token_count=len(token_ids),
                error=str(exc),
            )
            return []

    async def _fetch_gemini_orderbooks(
        self, symbols: list[str]
    ) -> list[OrderbookSnapshot]:
        snapshots: list[OrderbookSnapshot] = []
        for symbol in symbols:
            snapshot = await self._fetch_gemini_one(symbol)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    async def _fetch_gemini_one(self, symbol: str) -> OrderbookSnapshot | None:
        start = time.monotonic()
        try:
            ob = await self._gemini.get_orderbook(symbol)
            latency = time.monotonic() - start
            self._record_latency("gemini", latency)

            return OrderbookSnapshot(
                platform="gemini",
                ticker=symbol,
                best_bid=ob.best_bid,
                best_ask=ob.best_ask,
                yes_mid=ob.yes_mid,
                depth_5pct=0.0,
                depth_3pct_usd=ob.depth_3pct_usd,
                volume_24h=None,
                fetched_at=datetime.now(tz=timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            latency = time.monotonic() - start
            self._record_latency("gemini", latency)
            log.warning(
                "gemini_orderbook_fetch_failed",
                symbol=symbol,
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------
    # Metrics helpers
    # ------------------------------------------------------------------

    def _record_latency(self, platform: str, latency: float) -> None:
        """Record fetch latency to Prometheus histogram and rolling tracker."""
        if ORDERBOOK_FETCH_DURATION_SECONDS is not None:
            ORDERBOOK_FETCH_DURATION_SECONDS.labels(platform=platform).observe(latency)

        tracker = self._latency_trackers.get(platform)
        if tracker is not None:
            tracker.record(latency)

    def _check_p95_warnings(self) -> None:
        """Emit WARNING if any platform's p95 fetch latency exceeds 5s."""
        for platform, tracker in self._latency_trackers.items():
            p95 = tracker.p95()
            if p95 is not None and p95 > _P95_THRESHOLD_SECONDS:
                log.warning(
                    "orderbook_fetch_p95_exceeded",
                    platform=platform,
                    p95_seconds=round(p95, 3),
                    threshold_seconds=_P95_THRESHOLD_SECONDS,
                    window_seconds=_P95_WINDOW_SECONDS,
                )
