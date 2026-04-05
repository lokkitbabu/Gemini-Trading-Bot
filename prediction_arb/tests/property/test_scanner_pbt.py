# Feature: prediction-arbitrage-production
# Property 10: fetch_all() returns data from non-failing platforms, empty list for failing,
#              and FeedHealth.status="down" for each failing platform

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from prediction_arb.bot.scanner import Scanner

_ALL_PLATFORMS = ["kalshi", "polymarket", "gemini"]

_FIXTURE_EVENTS = {
    "kalshi": [{"id": "k1", "title": "BTC above 100k"}],
    "polymarket": [{"id": "p1", "title": "BTC above 100k by EOY"}],
    "gemini": [{"id": "g1", "title": "BTC above 100k Dec 2025"}],
}


def _make_scanner(failing_platforms: set[str]) -> Scanner:
    """Build a Scanner with mocked clients where specified platforms raise exceptions."""

    def _make_client(platform: str) -> MagicMock:
        client = MagicMock()
        if platform in failing_platforms:
            if platform == "kalshi":
                client.get_series = AsyncMock(side_effect=RuntimeError(f"{platform} down"))
            elif platform == "polymarket":
                client.get_markets = AsyncMock(side_effect=RuntimeError(f"{platform} down"))
            elif platform == "gemini":
                client.get_events = AsyncMock(side_effect=RuntimeError(f"{platform} down"))
        else:
            if platform == "kalshi":
                client.get_series = AsyncMock(return_value=_FIXTURE_EVENTS["kalshi"])
            elif platform == "polymarket":
                client.get_markets = AsyncMock(return_value=_FIXTURE_EVENTS["polymarket"])
            elif platform == "gemini":
                client.get_events = AsyncMock(return_value=_FIXTURE_EVENTS["gemini"])
        return client

    return Scanner(
        kalshi_client=_make_client("kalshi"),
        polymarket_client=_make_client("polymarket"),
        gemini_client=_make_client("gemini"),
        alert_manager=None,
    )


# ---------------------------------------------------------------------------
# Property 10: partial platform failure isolation
# ---------------------------------------------------------------------------

@given(st.frozensets(st.sampled_from(_ALL_PLATFORMS), max_size=2))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_10_scanner_partial_failure(failing_platforms: frozenset[str]) -> None:
    """
    Property 10: simulate the given set of platforms failing; assert:
    - fetch_all() returns data from non-failing platforms
    - empty list for failing platforms
    - FeedHealth.status="down" for each failing platform
    - FeedHealth.status="up" for each non-failing platform
    """
    scanner = _make_scanner(set(failing_platforms))

    async def _run():
        return await scanner.fetch_all()

    result = asyncio.get_event_loop().run_until_complete(_run())

    # Check per-platform results
    platform_results = {
        "kalshi": result.kalshi,
        "polymarket": result.polymarket,
        "gemini": result.gemini,
    }

    for platform in _ALL_PLATFORMS:
        events = platform_results[platform]
        health = result.feed_health[platform]

        if platform in failing_platforms:
            assert events == [], (
                f"Expected empty list for failing platform {platform}, got {events}"
            )
            assert health.status == "down", (
                f"Expected FeedHealth.status='down' for {platform}, got {health.status!r}"
            )
        else:
            assert events == _FIXTURE_EVENTS[platform], (
                f"Expected fixture events for {platform}, got {events}"
            )
            assert health.status == "up", (
                f"Expected FeedHealth.status='up' for {platform}, got {health.status!r}"
            )
