"""
Integration tests: StateStore against real PostgreSQL.

Requires DATABASE_URL env var pointing to a live PostgreSQL instance.
All tests are skipped automatically when DATABASE_URL is not set.

Run with:
    DATABASE_URL=postgresql+asyncpg://arb:arb@localhost:5432/arbdb \
        pytest prediction_arb/tests/integration/test_statestore.py -v -m integration

Alembic migration test requires the alembic.ini at prediction_arb/alembic.ini.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

# Skip entire module when no real DB is available
DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.integration

if not DATABASE_URL:
    pytest.skip("DATABASE_URL not set — skipping integration tests", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def store():
    """Create and initialise a StateStore against the real DB."""
    from prediction_arb.bot.state import StateStore

    s = StateStore(DATABASE_URL)
    await s.init()
    yield s
    await s.close()


def _make_opportunity(**overrides):
    from prediction_arb.bot.engine import Opportunity

    defaults = dict(
        id=str(uuid.uuid4()),
        detected_at=datetime.now(tz=timezone.utc),
        event_title="BTC above 100k by Dec 2025",
        asset="BTC",
        price_level=100_000.0,
        resolution_date="2025-12-31",
        signal_platform="kalshi",
        signal_event_id="KBTC-100K",
        signal_yes_price=0.42,
        signal_volume=50_000.0,
        gemini_event_id="GBTC-100K",
        gemini_yes_price=0.38,
        gemini_volume=20_000.0,
        gemini_bid=0.37,
        gemini_ask=0.39,
        gemini_depth=5_000.0,
        spread=0.04,
        spread_pct=0.095,
        direction="buy_yes",
        entry_price=0.39,
        kelly_fraction=0.02,
        match_confidence=0.85,
        days_to_resolution=90,
        risk_score=0.3,
        status="pending",
        signal_disagreement=False,
        inverted=False,
        price_age_seconds=5.0,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def _make_position(opportunity_id: str | None = None, **overrides):
    from prediction_arb.bot.executor import GeminiPosition

    defaults = dict(
        id=str(uuid.uuid4()),
        opportunity_id=opportunity_id or str(uuid.uuid4()),
        event_id="GBTC-100K",
        side="yes",
        quantity=10,
        entry_price=0.39,
        size_usd=39.0,
        exit_strategy="target_convergence",
        target_exit_price=0.42,
        stop_loss_price=0.33,
        status="open",
        opened_at=datetime.now(tz=timezone.utc),
        closed_at=None,
        exit_price=None,
        realized_pnl=None,
        ref_price=0.42,
        days_to_resolution=90,
    )
    defaults.update(overrides)
    return GeminiPosition(**defaults)


def _make_orderbook_snapshot(**overrides):
    from prediction_arb.bot.orderbook_cache import OrderbookSnapshot

    defaults = dict(
        platform="kalshi",
        ticker="KBTC-100K",
        best_bid=0.41,
        best_ask=0.43,
        yes_mid=0.42,
        depth_5pct=500.0,
        depth_3pct_usd=300.0,
        volume_24h=10_000.0,
        fetched_at=datetime.now(tz=timezone.utc),
    )
    defaults.update(overrides)
    return OrderbookSnapshot(**defaults)


# ---------------------------------------------------------------------------
# Opportunity round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_opportunity(store):
    """save_opportunity + get_opportunity returns identical fields."""
    opp = _make_opportunity()
    await store.save_opportunity(opp)

    fetched = await store.get_opportunity(opp.id)
    assert fetched is not None
    assert fetched.id == opp.id
    assert fetched.event_title == opp.event_title
    assert fetched.asset == opp.asset
    assert fetched.signal_platform == opp.signal_platform
    assert fetched.spread_pct == pytest.approx(opp.spread_pct, rel=1e-6)
    assert fetched.direction == opp.direction
    assert fetched.status == opp.status
    assert fetched.inverted == opp.inverted
    assert fetched.signal_disagreement == opp.signal_disagreement


@pytest.mark.asyncio
async def test_get_opportunity_missing_returns_none(store):
    """get_opportunity for unknown ID returns None."""
    result = await store.get_opportunity(str(uuid.uuid4()))
    assert result is None


# ---------------------------------------------------------------------------
# Position round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_open_positions(store):
    """save_position + get_open_positions includes the saved position."""
    # Save a linked opportunity first (FK constraint)
    opp = _make_opportunity()
    await store.save_opportunity(opp)

    pos = _make_position(opportunity_id=opp.id)
    await store.save_position(pos)

    open_positions = await store.get_open_positions()
    ids = [p.id for p in open_positions]
    assert pos.id in ids


@pytest.mark.asyncio
async def test_closed_position_excluded_from_open(store):
    """Positions with status='closed' are not returned by get_open_positions."""
    opp = _make_opportunity()
    await store.save_opportunity(opp)

    pos = _make_position(opportunity_id=opp.id, status="closed")
    await store.save_position(pos)

    open_positions = await store.get_open_positions()
    ids = [p.id for p in open_positions]
    assert pos.id not in ids


@pytest.mark.asyncio
async def test_update_position_round_trip(store):
    """update_position persists changed fields."""
    opp = _make_opportunity()
    await store.save_opportunity(opp)

    pos = _make_position(opportunity_id=opp.id)
    await store.save_position(pos)

    # Close the position
    pos.status = "closed"
    pos.exit_price = 0.44
    pos.realized_pnl = (0.44 - pos.entry_price) * pos.quantity
    pos.closed_at = datetime.now(tz=timezone.utc)
    await store.update_position(pos)

    # Verify it no longer appears in open positions
    open_positions = await store.get_open_positions()
    ids = [p.id for p in open_positions]
    assert pos.id not in ids


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_aggregate_stats_with_data(store):
    """get_aggregate_stats() returns correct totals for closed positions."""
    opp1 = _make_opportunity(spread_pct=0.10)
    opp2 = _make_opportunity(spread_pct=0.20)
    await store.save_opportunity(opp1)
    await store.save_opportunity(opp2)

    # Two closed positions: one winner, one loser
    pos_win = _make_position(
        opportunity_id=opp1.id,
        status="closed",
        realized_pnl=5.0,
    )
    pos_lose = _make_position(
        opportunity_id=opp2.id,
        status="closed",
        realized_pnl=-2.0,
    )
    await store.save_position(pos_win)
    await store.save_position(pos_lose)

    stats = await store.get_aggregate_stats()

    # total_pnl and trade_count should include our records (may include prior test data)
    assert stats.trade_count >= 2
    # win_rate is between 0 and 1
    assert 0.0 <= stats.win_rate <= 1.0
    # avg_spread is non-negative
    assert stats.avg_spread >= 0.0


@pytest.mark.asyncio
async def test_get_aggregate_stats_empty(store):
    """get_aggregate_stats() returns zero values when no closed positions exist."""
    # Use a very narrow time window that won't match any existing data
    from datetime import timedelta

    stats = await store.get_aggregate_stats(window=timedelta(seconds=0))
    assert stats.trade_count == 0
    assert stats.total_pnl == 0.0
    assert stats.win_rate == 0.0


# ---------------------------------------------------------------------------
# Orderbook snapshot round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_orderbook_snapshot(store):
    """save_orderbook_snapshot + get_orderbook_snapshot returns most recent."""
    ticker = f"TEST-{uuid.uuid4().hex[:8]}"
    snap = _make_orderbook_snapshot(platform="kalshi", ticker=ticker, yes_mid=0.55)
    await store.save_orderbook_snapshot(snap)

    fetched = await store.get_orderbook_snapshot("kalshi", ticker)
    assert fetched is not None
    assert fetched.platform == "kalshi"
    assert fetched.ticker == ticker
    assert fetched.yes_mid == pytest.approx(0.55, rel=1e-6)
    assert fetched.best_bid == pytest.approx(snap.best_bid, rel=1e-6)
    assert fetched.best_ask == pytest.approx(snap.best_ask, rel=1e-6)


@pytest.mark.asyncio
async def test_get_orderbook_snapshot_returns_most_recent(store):
    """When multiple snapshots exist, get_orderbook_snapshot returns the latest."""
    import asyncio

    ticker = f"TEST-{uuid.uuid4().hex[:8]}"
    snap_old = _make_orderbook_snapshot(
        platform="polymarket",
        ticker=ticker,
        yes_mid=0.30,
        fetched_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    snap_new = _make_orderbook_snapshot(
        platform="polymarket",
        ticker=ticker,
        yes_mid=0.60,
        fetched_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    await store.save_orderbook_snapshot(snap_old)
    await store.save_orderbook_snapshot(snap_new)

    fetched = await store.get_orderbook_snapshot("polymarket", ticker)
    assert fetched is not None
    assert fetched.yes_mid == pytest.approx(0.60, rel=1e-6)


@pytest.mark.asyncio
async def test_get_orderbook_snapshot_missing_returns_none(store):
    """get_orderbook_snapshot for unknown (platform, ticker) returns None."""
    result = await store.get_orderbook_snapshot("unknown_platform", "NONEXISTENT-TICKER")
    assert result is None


# ---------------------------------------------------------------------------
# Alembic migration: upgrade head → downgrade base
# ---------------------------------------------------------------------------


def test_alembic_upgrade_and_downgrade():
    """
    Verify Alembic can run 'upgrade head' then 'downgrade base' without error.

    Uses subprocess so the test is isolated from the async event loop.
    Requires DATABASE_URL to be set and the DB to be reachable.
    """
    env = {**os.environ, "DATABASE_URL": DATABASE_URL}

    # upgrade head
    result_up = subprocess.run(
        ["alembic", "-c", "prediction_arb/alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result_up.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT: {result_up.stdout}\nSTDERR: {result_up.stderr}"
    )

    # downgrade base
    result_down = subprocess.run(
        ["alembic", "-c", "prediction_arb/alembic.ini", "downgrade", "base"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result_down.returncode == 0, (
        f"alembic downgrade base failed:\nSTDOUT: {result_down.stdout}\nSTDERR: {result_down.stderr}"
    )

    # Re-apply migrations so subsequent tests can use the schema
    result_reup = subprocess.run(
        ["alembic", "-c", "prediction_arb/alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result_reup.returncode == 0, (
        f"alembic re-upgrade head failed:\nSTDOUT: {result_reup.stdout}\nSTDERR: {result_reup.stderr}"
    )
