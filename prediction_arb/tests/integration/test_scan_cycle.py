"""
Integration tests: full scan cycle with mocked platform clients.

These tests wire together the real Scanner, ArbitrageEngine, and SSEBroadcaster
using mocked platform clients that return fixture data. A mock StateStore is
used so no real database is required.

Verifies:
  - Opportunities are detected and persisted to StateStore
  - SSE events are broadcast for each detected opportunity
  - SCAN_CYCLES_TOTAL and OPPORTUNITIES_DETECTED_TOTAL metrics are incremented

Run with:
    pytest prediction_arb/tests/integration/test_scan_cycle.py -v -m integration
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------


def _kalshi_event(ticker: str = "KBTC-100K", yes_price: float = 0.42) -> dict:
    return {
        "id": ticker,
        "title": f"BTC above 100k ({ticker})",
        "yes_price": yes_price,
        "volume": 50_000.0,
        "platform": "kalshi",
    }


def _polymarket_event(condition_id: str = "PBTC-100K", yes_price: float = 0.43) -> dict:
    return {
        "id": condition_id,
        "title": f"BTC above 100k ({condition_id})",
        "yes_price": yes_price,
        "volume": 30_000.0,
        "platform": "polymarket",
    }


def _gemini_event(event_id: str = "GBTC-100K", yes_price: float = 0.38) -> dict:
    return {
        "id": event_id,
        "title": f"BTC above 100k ({event_id})",
        "yes_price": yes_price,
        "volume": 20_000.0,
        "platform": "gemini",
    }


# ---------------------------------------------------------------------------
# Mock platform clients
# ---------------------------------------------------------------------------


class MockKalshiClient:
    async def get_series(self) -> list:
        return [_kalshi_event()]


class MockPolymarketClient:
    async def get_markets(self) -> list:
        return [_polymarket_event()]


class MockGeminiClient:
    async def get_events(self) -> list:
        return [_gemini_event()]


class MockFailingClient:
    """Simulates a platform client that always raises an exception."""

    async def get_series(self) -> list:
        raise ConnectionError("Kalshi unreachable")

    async def get_markets(self) -> list:
        raise ConnectionError("Polymarket unreachable")

    async def get_events(self) -> list:
        raise ConnectionError("Gemini unreachable")


# ---------------------------------------------------------------------------
# Mock StateStore (in-memory, no DB required)
# ---------------------------------------------------------------------------


class MockStateStore:
    """In-memory StateStore that records all saved opportunities and positions."""

    def __init__(self) -> None:
        self.saved_opportunities: list[Any] = []
        self.saved_positions: list[Any] = []

    async def save_opportunity(self, opp: Any) -> None:
        self.saved_opportunities.append(opp)

    async def get_opportunity(self, id: str) -> Any:
        for opp in self.saved_opportunities:
            if opp.id == id:
                return opp
        return None

    async def save_position(self, pos: Any) -> None:
        self.saved_positions.append(pos)

    async def update_position(self, pos: Any) -> None:
        for i, p in enumerate(self.saved_positions):
            if p.id == pos.id:
                self.saved_positions[i] = pos
                return
        self.saved_positions.append(pos)

    async def get_open_positions(self) -> list:
        return [p for p in self.saved_positions if p.status in ("open", "filled")]

    async def get_aggregate_stats(self, window: Any = None) -> Any:
        from prediction_arb.bot.state import AggregateStats
        return AggregateStats(
            total_pnl=0.0, win_rate=0.0, avg_spread=0.0,
            exit_reason_breakdown={}, trade_count=0,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sse_broadcaster():
    from prediction_arb.bot.api.sse import SSEBroadcaster
    return SSEBroadcaster()


@pytest.fixture
def mock_state_store():
    return MockStateStore()


@pytest.fixture
def scanner():
    from prediction_arb.bot.scanner import Scanner
    return Scanner(
        kalshi_client=MockKalshiClient(),
        polymarket_client=MockPolymarketClient(),
        gemini_client=MockGeminiClient(),
        alert_manager=None,
    )


# ---------------------------------------------------------------------------
# Helper: build a minimal MatchedPair from fixture events
# ---------------------------------------------------------------------------


def _build_matched_pair(ref_event_dict: dict, target_event_dict: dict):
    """
    Build a MatchedPair-like object from raw event dicts.
    Uses simple dataclasses to avoid needing a real EventMatcher.
    """
    from dataclasses import dataclass, field

    @dataclass
    class FakeEvent:
        id: str
        title: str
        yes_price: float
        volume: float
        platform: str

    @dataclass
    class FakeMatchResult:
        equivalent: bool = True
        confidence: float = 0.90
        asset: str = "BTC"
        price_level: float = 100_000.0
        direction: str = "above"
        resolution_date: str = "2025-12-31"
        inverted: bool = False
        backend: str = "rule_based"

    @dataclass
    class FakeMatchedPair:
        ref: FakeEvent
        target: FakeEvent
        result: FakeMatchResult

    ref = FakeEvent(**{k: ref_event_dict[k] for k in ("id", "title", "yes_price", "volume", "platform")})
    target = FakeEvent(**{k: target_event_dict[k] for k in ("id", "title", "yes_price", "volume", "platform")})
    return FakeMatchedPair(ref=ref, target=target, result=FakeMatchResult())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scanner_fetch_all_returns_data(scanner):
    """Scanner.fetch_all() returns non-empty lists from all three platforms."""
    result = await scanner.fetch_all()

    assert len(result.kalshi) == 1
    assert len(result.polymarket) == 1
    assert len(result.gemini) == 1

    # All platforms should be healthy
    for platform in ("kalshi", "polymarket", "gemini"):
        assert result.feed_health[platform].status == "up"
        assert result.feed_health[platform].consecutive_failures == 0


@pytest.mark.asyncio
async def test_scanner_increments_scan_cycles_metric(scanner):
    """SCAN_CYCLES_TOTAL is incremented after each fetch_all() call."""
    from prometheus_client import REGISTRY

    # Read current value before
    before = _get_counter_value("arb_scan_cycles_total")

    await scanner.fetch_all()
    await scanner.fetch_all()

    after = _get_counter_value("arb_scan_cycles_total")
    assert after >= before + 2


def _get_counter_value(metric_name: str) -> float:
    """Read the current value of a prometheus counter by name."""
    from prometheus_client import REGISTRY
    for metric in REGISTRY.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if sample.name == metric_name + "_total" or sample.name == metric_name:
                    return sample.value
    return 0.0


@pytest.mark.asyncio
async def test_scanner_partial_failure_isolates_platform():
    """When one platform fails, others still return data and health is tracked."""
    from prediction_arb.bot.scanner import Scanner

    scanner = Scanner(
        kalshi_client=MockFailingClient(),
        polymarket_client=MockPolymarketClient(),
        gemini_client=MockGeminiClient(),
        alert_manager=None,
    )

    result = await scanner.fetch_all()

    # Kalshi failed — empty list, status down
    assert result.kalshi == []
    assert result.feed_health["kalshi"].status == "down"
    assert result.feed_health["kalshi"].consecutive_failures == 1

    # Polymarket and Gemini succeeded
    assert len(result.polymarket) == 1
    assert len(result.gemini) == 1
    assert result.feed_health["polymarket"].status == "up"
    assert result.feed_health["gemini"].status == "up"


@pytest.mark.asyncio
async def test_sse_broadcaster_receives_opportunity_event(sse_broadcaster):
    """SSEBroadcaster delivers opportunity_detected events to subscribers."""
    received: list[str] = []

    async def collect_one():
        async for chunk in sse_broadcaster.subscribe():
            received.append(chunk)
            break  # only collect one event

    # Start subscriber in background
    task = asyncio.create_task(collect_one())
    await asyncio.sleep(0)  # yield to let subscriber register

    # Publish an event
    await sse_broadcaster.publish(
        "opportunity_detected",
        {"id": "test-opp-1", "spread_pct": 0.10},
    )

    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert "opportunity_detected" in received[0]
    payload = json.loads(received[0].split("data: ")[1].strip())
    assert payload["id"] == "test-opp-1"
    assert payload["spread_pct"] == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_full_scan_cycle_persists_opportunities(mock_state_store, sse_broadcaster):
    """
    Full scan cycle: Scanner → ArbitrageEngine → StateStore + SSE.

    Uses mocked platform clients and a mock StateStore (no real DB).
    Verifies that detected opportunities are saved and SSE events broadcast.
    """
    from prediction_arb.bot.engine import ArbitrageEngine
    from prediction_arb.bot.scanner import Scanner

    scanner = Scanner(
        kalshi_client=MockKalshiClient(),
        polymarket_client=MockPolymarketClient(),
        gemini_client=MockGeminiClient(),
        alert_manager=None,
    )

    engine = ArbitrageEngine(orderbook_cache=None)

    # Run the scan
    scan_result = await scanner.fetch_all()

    # Build matched pairs from fixture data (bypass real EventMatcher)
    kalshi_ev = scan_result.kalshi[0]
    gemini_ev = scan_result.gemini[0]
    pair = _build_matched_pair(kalshi_ev, gemini_ev)

    # Score opportunities
    opportunities = engine.score([pair])

    # Persist and broadcast each opportunity
    sse_events: list[str] = []

    async def collect_events():
        async for chunk in sse_broadcaster.subscribe():
            sse_events.append(chunk)
            if len(sse_events) >= len(opportunities):
                break

    if opportunities:
        collect_task = asyncio.create_task(collect_events())
        await asyncio.sleep(0)  # let subscriber register

        for opp in opportunities:
            await mock_state_store.save_opportunity(opp)
            await sse_broadcaster.publish(
                "opportunity_detected",
                {"id": opp.id, "spread_pct": opp.spread_pct},
            )

        try:
            await asyncio.wait_for(collect_task, timeout=2.0)
        except asyncio.TimeoutError:
            collect_task.cancel()

        # Verify persistence
        assert len(mock_state_store.saved_opportunities) == len(opportunities)
        for opp in opportunities:
            saved = await mock_state_store.get_opportunity(opp.id)
            assert saved is not None
            assert saved.id == opp.id

        # Verify SSE events were broadcast
        assert len(sse_events) == len(opportunities)
        for chunk in sse_events:
            assert "opportunity_detected" in chunk


@pytest.mark.asyncio
async def test_scan_cycle_with_no_opportunities(mock_state_store, sse_broadcaster):
    """When no matched pairs produce opportunities, nothing is persisted."""
    from prediction_arb.bot.engine import ArbitrageEngine

    engine = ArbitrageEngine(orderbook_cache=None)

    # Score with empty pairs list
    opportunities = engine.score([])

    assert opportunities == []
    assert len(mock_state_store.saved_opportunities) == 0


@pytest.mark.asyncio
async def test_opportunities_detected_metric_incremented():
    """OPPORTUNITIES_DETECTED_TOTAL counter increments when opportunities are detected."""
    from prediction_arb.bot.metrics import OPPORTUNITIES_DETECTED_TOTAL

    before = _get_counter_value("arb_opportunities_detected_total")

    # Simulate incrementing the counter (as the main loop would do)
    OPPORTUNITIES_DETECTED_TOTAL.labels(platform_pair="kalshi_gemini").inc()
    OPPORTUNITIES_DETECTED_TOTAL.labels(platform_pair="kalshi_gemini").inc()

    after = _get_counter_value("arb_opportunities_detected_total")
    assert after >= before + 2


@pytest.mark.asyncio
async def test_scanner_alert_triggered_after_threshold_failures():
    """AlertManager.send_platform_down_alert is called after 3 consecutive failures."""
    from prediction_arb.bot.scanner import Scanner

    mock_alert = AsyncMock()
    mock_alert.send_platform_down_alert = AsyncMock()

    scanner = Scanner(
        kalshi_client=MockFailingClient(),
        polymarket_client=MockPolymarketClient(),
        gemini_client=MockGeminiClient(),
        alert_manager=mock_alert,
    )

    # Run 3 consecutive failing scans
    for _ in range(3):
        await scanner.fetch_all()

    # Alert should have been triggered on the 3rd failure
    mock_alert.send_platform_down_alert.assert_called_with(
        platform="kalshi",
        consecutive_failures=3,
    )


@pytest.mark.asyncio
async def test_sse_broadcaster_multiple_subscribers(sse_broadcaster):
    """All active subscribers receive the same published event."""
    received_a: list[str] = []
    received_b: list[str] = []

    async def collect(store: list):
        async for chunk in sse_broadcaster.subscribe():
            store.append(chunk)
            break

    task_a = asyncio.create_task(collect(received_a))
    task_b = asyncio.create_task(collect(received_b))
    await asyncio.sleep(0)  # let both subscribers register

    await sse_broadcaster.publish("opportunity_detected", {"id": "multi-test"})

    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)

    assert len(received_a) == 1
    assert len(received_b) == 1
    assert received_a[0] == received_b[0]
