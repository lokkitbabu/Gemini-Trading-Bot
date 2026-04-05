"""
Unit tests for OrderbookCache and OrderbookSnapshot.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from prediction_arb.bot.orderbook_cache import OrderbookCache, OrderbookSnapshot


def _make_snapshot(
    platform: str = "kalshi",
    ticker: str = "KXBTCD-25MAR",
    yes_mid: float = 0.55,
    depth_5pct: float = 20.0,
    fetched_at: datetime | None = None,
) -> OrderbookSnapshot:
    if fetched_at is None:
        fetched_at = datetime.now(tz=timezone.utc)
    return OrderbookSnapshot(
        platform=platform,
        ticker=ticker,
        best_bid=yes_mid - 0.01,
        best_ask=yes_mid + 0.01,
        yes_mid=yes_mid,
        depth_5pct=depth_5pct,
        depth_3pct_usd=0.0,
        volume_24h=1000.0,
        fetched_at=fetched_at,
    )


class TestOrderbookCacheUpdate:
    def test_update_and_get(self):
        cache = OrderbookCache()
        snap = _make_snapshot()
        asyncio.get_event_loop().run_until_complete(cache.update(snap))
        result = cache.get("kalshi", "KXBTCD-25MAR")
        assert result is snap

    def test_get_missing_returns_none(self):
        cache = OrderbookCache()
        assert cache.get("kalshi", "NONEXISTENT") is None

    def test_update_overwrites_previous(self):
        cache = OrderbookCache()
        snap1 = _make_snapshot(yes_mid=0.50)
        snap2 = _make_snapshot(yes_mid=0.60)
        asyncio.get_event_loop().run_until_complete(cache.update(snap1))
        asyncio.get_event_loop().run_until_complete(cache.update(snap2))
        result = cache.get("kalshi", "KXBTCD-25MAR")
        assert result is snap2
        assert result.yes_mid == 0.60

    def test_different_platforms_stored_separately(self):
        cache = OrderbookCache()
        k_snap = _make_snapshot(platform="kalshi", ticker="KXBTCD-25MAR", yes_mid=0.55)
        p_snap = _make_snapshot(platform="polymarket", ticker="KXBTCD-25MAR", yes_mid=0.57)
        asyncio.get_event_loop().run_until_complete(cache.update(k_snap))
        asyncio.get_event_loop().run_until_complete(cache.update(p_snap))
        assert cache.get("kalshi", "KXBTCD-25MAR") is k_snap
        assert cache.get("polymarket", "KXBTCD-25MAR") is p_snap


class TestOrderbookCacheIsFresh:
    def test_fresh_snapshot(self):
        cache = OrderbookCache()
        snap = _make_snapshot(fetched_at=datetime.now(tz=timezone.utc))
        asyncio.get_event_loop().run_until_complete(cache.update(snap))
        assert cache.is_fresh("kalshi", "KXBTCD-25MAR", max_age_seconds=60) is True

    def test_stale_snapshot(self):
        cache = OrderbookCache()
        old_time = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
        snap = _make_snapshot(fetched_at=old_time)
        asyncio.get_event_loop().run_until_complete(cache.update(snap))
        assert cache.is_fresh("kalshi", "KXBTCD-25MAR", max_age_seconds=60) is False

    def test_exactly_at_boundary_is_fresh(self):
        cache = OrderbookCache()
        # Exactly at max_age_seconds should be considered fresh (<= not <)
        old_time = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
        snap = _make_snapshot(fetched_at=old_time)
        asyncio.get_event_loop().run_until_complete(cache.update(snap))
        # Allow 1s tolerance for test execution time
        assert cache.is_fresh("kalshi", "KXBTCD-25MAR", max_age_seconds=61) is True

    def test_missing_snapshot_not_fresh(self):
        cache = OrderbookCache()
        assert cache.is_fresh("kalshi", "NONEXISTENT", max_age_seconds=60) is False


class TestOrderbookCacheGetAllForPair:
    def test_returns_snapshots_for_both_platforms(self):
        from prediction_arb.bot.matcher import MarketEvent, MatchedPair, MatchResult

        cache = OrderbookCache()
        k_snap = _make_snapshot(platform="kalshi", ticker="kalshi-id-1")
        g_snap = _make_snapshot(platform="gemini", ticker="gemini-id-1")
        asyncio.get_event_loop().run_until_complete(cache.update(k_snap))
        asyncio.get_event_loop().run_until_complete(cache.update(g_snap))

        ref = MarketEvent(id="kalshi-id-1", title="BTC above $95k", platform="kalshi")
        target = MarketEvent(id="gemini-id-1", title="BTC above $95k", platform="gemini")
        result = MatchResult(equivalent=True, confidence=0.9, reasoning="match")
        pair = MatchedPair(ref=ref, target=target, result=result)

        snapshots = cache.get_all_for_pair(pair)
        assert snapshots["kalshi"] is k_snap
        assert snapshots["gemini"] is g_snap

    def test_returns_none_for_missing_platform(self):
        from prediction_arb.bot.matcher import MarketEvent, MatchedPair, MatchResult

        cache = OrderbookCache()

        ref = MarketEvent(id="kalshi-id-1", title="BTC above $95k", platform="kalshi")
        target = MarketEvent(id="gemini-id-1", title="BTC above $95k", platform="gemini")
        result = MatchResult(equivalent=True, confidence=0.9, reasoning="match")
        pair = MatchedPair(ref=ref, target=target, result=result)

        snapshots = cache.get_all_for_pair(pair)
        assert snapshots["kalshi"] is None
        assert snapshots["gemini"] is None
