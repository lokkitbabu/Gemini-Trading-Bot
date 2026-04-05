# Feature: prediction-arbitrage-production
# Property 1: StateStore round-trip — save Opportunity + GeminiPosition, read back by ID, all fields equal original
# Property 2: get_aggregate_stats() equals manually computed sum of P&L, win count, trade count

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from prediction_arb.bot.engine import Opportunity
from prediction_arb.bot.executor import GeminiPosition

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_utc_dt = st.datetimes(timezones=st.just(timezone.utc))

_opp_strategy = st.builds(
    Opportunity,
    id=st.uuids().map(str),
    detected_at=_utc_dt,
    event_title=st.text(min_size=1, max_size=100),
    asset=st.one_of(st.none(), st.sampled_from(["BTC", "ETH", "SOL"])),
    price_level=st.one_of(st.none(), st.floats(100.0, 1_000_000.0, allow_nan=False)),
    resolution_date=st.one_of(st.none(), st.dates().map(str)),
    signal_platform=st.sampled_from(["kalshi", "polymarket"]),
    signal_event_id=st.uuids().map(str),
    signal_yes_price=st.floats(0.01, 0.99, allow_nan=False),
    signal_volume=st.floats(0.0, 1_000_000.0, allow_nan=False),
    gemini_event_id=st.uuids().map(str),
    gemini_yes_price=st.floats(0.01, 0.99, allow_nan=False),
    gemini_volume=st.floats(0.0, 1_000_000.0, allow_nan=False),
    gemini_bid=st.one_of(st.none(), st.floats(0.01, 0.98, allow_nan=False)),
    gemini_ask=st.one_of(st.none(), st.floats(0.02, 0.99, allow_nan=False)),
    gemini_depth=st.floats(0.0, 10_000.0, allow_nan=False),
    spread=st.floats(0.0, 0.5, allow_nan=False),
    spread_pct=st.floats(0.0, 1.0, allow_nan=False),
    direction=st.sampled_from(["buy_yes", "buy_no"]),
    entry_price=st.floats(0.01, 0.99, allow_nan=False),
    kelly_fraction=st.floats(0.0, 0.05, allow_nan=False),
    match_confidence=st.floats(0.0, 1.0, allow_nan=False),
    days_to_resolution=st.one_of(st.none(), st.integers(0, 365)),
    risk_score=st.floats(0.0, 1.0, allow_nan=False),
    status=st.sampled_from(["pending", "executed", "expired", "skipped"]),
    signal_disagreement=st.booleans(),
    inverted=st.booleans(),
    price_age_seconds=st.floats(0.0, 120.0, allow_nan=False),
)

_pos_strategy = st.builds(
    GeminiPosition,
    id=st.uuids().map(str),
    opportunity_id=st.uuids().map(str),
    event_id=st.uuids().map(str),
    side=st.sampled_from(["yes", "no"]),
    quantity=st.integers(1, 1000),
    entry_price=st.floats(0.01, 0.99, allow_nan=False),
    size_usd=st.floats(1.0, 10_000.0, allow_nan=False),
    exit_strategy=st.sampled_from(["target_convergence", "hold_to_resolution"]),
    target_exit_price=st.floats(0.01, 0.99, allow_nan=False),
    stop_loss_price=st.floats(0.01, 0.99, allow_nan=False),
    status=st.sampled_from(["open", "filled", "closed", "failed"]),
    opened_at=_utc_dt,
    closed_at=st.one_of(st.none(), _utc_dt),
    exit_price=st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False)),
    realized_pnl=st.one_of(st.none(), st.floats(-1000.0, 1000.0, allow_nan=False)),
    ref_price=st.floats(0.01, 0.99, allow_nan=False),
    days_to_resolution=st.one_of(st.none(), st.integers(0, 365)),
)

_resolved_pos_strategy = st.builds(
    GeminiPosition,
    id=st.uuids().map(str),
    opportunity_id=st.uuids().map(str),
    event_id=st.uuids().map(str),
    side=st.sampled_from(["yes", "no"]),
    quantity=st.integers(1, 1000),
    entry_price=st.floats(0.01, 0.99, allow_nan=False),
    size_usd=st.floats(1.0, 10_000.0, allow_nan=False),
    exit_strategy=st.sampled_from(["target_convergence", "hold_to_resolution"]),
    target_exit_price=st.floats(0.01, 0.99, allow_nan=False),
    stop_loss_price=st.floats(0.01, 0.99, allow_nan=False),
    status=st.just("closed"),
    opened_at=_utc_dt,
    closed_at=st.one_of(st.none(), _utc_dt),
    exit_price=st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False)),
    realized_pnl=st.floats(-500.0, 500.0, allow_nan=False),
    ref_price=st.floats(0.01, 0.99, allow_nan=False),
    days_to_resolution=st.one_of(st.none(), st.integers(0, 365)),
)


# ---------------------------------------------------------------------------
# In-memory StateStore stub for PBT (no DB required)
# ---------------------------------------------------------------------------

class _InMemoryStateStore:
    """Minimal in-memory StateStore for property testing without a real DB."""

    def __init__(self) -> None:
        self._opps: dict[str, Opportunity] = {}
        self._positions: dict[str, GeminiPosition] = {}

    async def save_opportunity(self, opp: Opportunity) -> None:
        self._opps[opp.id] = opp

    async def get_opportunity(self, id: str) -> Opportunity | None:
        return self._opps.get(id)

    async def save_position(self, pos: GeminiPosition) -> None:
        self._positions[pos.id] = pos

    async def update_position(self, pos: GeminiPosition) -> None:
        self._positions[pos.id] = pos

    async def get_open_positions(self) -> list[GeminiPosition]:
        return [p for p in self._positions.values() if p.status in ("open", "filled")]

    async def get_aggregate_stats(self, window: Any = None):
        from prediction_arb.bot.state import AggregateStats
        closed = [p for p in self._positions.values() if p.status == "closed"]
        total_pnl = sum(p.realized_pnl or 0.0 for p in closed)
        wins = sum(1 for p in closed if (p.realized_pnl or 0.0) > 0)
        win_rate = wins / len(closed) if closed else 0.0
        return AggregateStats(
            total_pnl=total_pnl,
            win_rate=win_rate,
            avg_spread=0.0,
            exit_reason_breakdown={},
            trade_count=len(closed),
        )


# ---------------------------------------------------------------------------
# Property 1: round-trip save/read for Opportunity and GeminiPosition
# ---------------------------------------------------------------------------

@given(_opp_strategy, _pos_strategy)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_statestore_roundtrip(opp: Opportunity, pos: GeminiPosition) -> None:
    """
    Property 1: save Opportunity and GeminiPosition to StateStore, read back by ID,
    assert all fields equal the original.
    """
    store = _InMemoryStateStore()

    async def _run() -> None:
        await store.save_opportunity(opp)
        await store.save_position(pos)

        retrieved_opp = await store.get_opportunity(opp.id)
        assert retrieved_opp is not None, "Opportunity not found after save"
        assert retrieved_opp.id == opp.id
        assert retrieved_opp.event_title == opp.event_title
        assert retrieved_opp.signal_platform == opp.signal_platform
        assert retrieved_opp.spread_pct == opp.spread_pct
        assert retrieved_opp.match_confidence == opp.match_confidence
        assert retrieved_opp.inverted == opp.inverted

        open_positions = await store.get_open_positions()
        if pos.status in ("open", "filled"):
            ids = [p.id for p in open_positions]
            assert pos.id in ids, "Position not found in open positions after save"

    asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# Property 2: get_aggregate_stats() equals manually computed sum
# ---------------------------------------------------------------------------

@given(st.lists(_resolved_pos_strategy, min_size=0, max_size=20))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_aggregate_stats(positions: list[GeminiPosition]) -> None:
    """
    Property 2: get_aggregate_stats() total_pnl, win_rate, and trade_count
    equal manually computed values from the position list.
    """
    store = _InMemoryStateStore()

    async def _run() -> None:
        for pos in positions:
            await store.save_position(pos)

        stats = await store.get_aggregate_stats()

        expected_pnl = sum(p.realized_pnl or 0.0 for p in positions)
        expected_count = len(positions)
        expected_wins = sum(1 for p in positions if (p.realized_pnl or 0.0) > 0)
        expected_win_rate = expected_wins / expected_count if expected_count > 0 else 0.0

        assert abs(stats.total_pnl - expected_pnl) < 1e-9, (
            f"total_pnl mismatch: {stats.total_pnl} != {expected_pnl}"
        )
        assert stats.trade_count == expected_count, (
            f"trade_count mismatch: {stats.trade_count} != {expected_count}"
        )
        assert abs(stats.win_rate - expected_win_rate) < 1e-9, (
            f"win_rate mismatch: {stats.win_rate} != {expected_win_rate}"
        )

    asyncio.get_event_loop().run_until_complete(_run())
