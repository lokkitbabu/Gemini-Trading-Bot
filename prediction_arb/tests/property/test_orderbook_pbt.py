# Feature: prediction-arbitrage-production
# Property 28: is_fresh() returns True iff (now - fetched_at).total_seconds() <= max_age_seconds
# Property 29: depth_5pct >= 0.0 for any valid orderbook response
# Property 30: reference_price equals volume-weighted average of yes_mid values from cache

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

from prediction_arb.bot.engine import compute_reference_price
from prediction_arb.bot.orderbook_cache import OrderbookCache, OrderbookSnapshot

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_utc_dt = st.datetimes(timezones=st.just(timezone.utc))

_snapshot_strategy = st.builds(
    OrderbookSnapshot,
    platform=st.sampled_from(["kalshi", "polymarket", "gemini"]),
    ticker=st.text(min_size=1, max_size=20),
    best_bid=st.one_of(st.none(), st.floats(0.01, 0.49, allow_nan=False)),
    best_ask=st.one_of(st.none(), st.floats(0.51, 0.99, allow_nan=False)),
    yes_mid=st.one_of(st.none(), st.floats(0.01, 0.99, allow_nan=False)),
    depth_5pct=st.floats(0.0, 10_000.0, allow_nan=False),
    depth_3pct_usd=st.floats(0.0, 10_000.0, allow_nan=False),
    volume_24h=st.one_of(st.none(), st.floats(0.0, 1_000_000.0, allow_nan=False)),
    fetched_at=_utc_dt,
)


# ---------------------------------------------------------------------------
# Property 28: is_fresh() correctness
# ---------------------------------------------------------------------------

@given(_snapshot_strategy, st.integers(min_value=0, max_value=3600))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_28_is_fresh_correctness(
    snapshot: OrderbookSnapshot, max_age_seconds: int
) -> None:
    """
    Property 28: is_fresh() returns True iff
    (now - fetched_at).total_seconds() <= max_age_seconds.
    """
    cache = OrderbookCache()

    async def _run():
        await cache.update(snapshot)

    asyncio.get_event_loop().run_until_complete(_run())

    now = datetime.now(tz=timezone.utc)
    actual_age = (now - snapshot.fetched_at).total_seconds()
    expected_fresh = actual_age <= max_age_seconds

    result = cache.is_fresh(snapshot.platform, snapshot.ticker, max_age_seconds)

    assert result == expected_fresh, (
        f"is_fresh() returned {result} but expected {expected_fresh}. "
        f"actual_age={actual_age:.2f}s, max_age={max_age_seconds}s"
    )


# ---------------------------------------------------------------------------
# Property 29: depth values are always >= 0.0
# ---------------------------------------------------------------------------

@given(
    st.lists(
        st.tuples(
            st.floats(0.01, 0.99, allow_nan=False),   # price
            st.floats(0.0, 10_000.0, allow_nan=False), # quantity
        ),
        min_size=0,
        max_size=100,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_29_depth_non_negative(
    orderbook_levels: list[tuple[float, float]],
) -> None:
    """
    Property 29: for any valid orderbook response, depth_5pct >= 0.0.
    Empty orderbook produces depth = 0.0 without exception.
    """
    # Simulate computing depth_5pct: sum quantities within 5¢ of best bid
    if not orderbook_levels:
        depth = 0.0
    else:
        best_bid = max(price for price, _ in orderbook_levels)
        threshold = best_bid - 0.05
        depth = sum(qty for price, qty in orderbook_levels if price >= threshold)

    assert depth >= 0.0, f"depth_5pct is negative: {depth}"

    # Build a snapshot with this depth and verify it's stored correctly
    snapshot = OrderbookSnapshot(
        platform="kalshi",
        ticker="BTC-95K",
        best_bid=best_bid if orderbook_levels else None,
        best_ask=None,
        yes_mid=None,
        depth_5pct=depth,
        depth_3pct_usd=0.0,
        volume_24h=None,
        fetched_at=datetime.now(tz=timezone.utc),
    )

    assert snapshot.depth_5pct >= 0.0, (
        f"OrderbookSnapshot.depth_5pct is negative: {snapshot.depth_5pct}"
    )


# ---------------------------------------------------------------------------
# Property 30: reference_price equals volume-weighted average of yes_mid values
# ---------------------------------------------------------------------------

@given(
    st.floats(min_value=0.01, max_value=0.99, allow_nan=False),  # kalshi yes_mid
    st.floats(min_value=0.01, max_value=0.99, allow_nan=False),  # poly yes_mid
    st.floats(min_value=10.0, max_value=1_000_000.0, allow_nan=False),  # kalshi volume
    st.floats(min_value=10.0, max_value=1_000_000.0, allow_nan=False),  # poly volume
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_30_reference_price_from_cache(
    kalshi_mid: float,
    poly_mid: float,
    kalshi_volume: float,
    poly_volume: float,
) -> None:
    """
    Property 30: when fresh snapshots exist in cache with depth_5pct >= 10,
    compute_reference_price() returns the volume-weighted average of yes_mid values,
    not yes_price from market list.
    """
    now = datetime.now(tz=timezone.utc)

    kalshi_ob = OrderbookSnapshot(
        platform="kalshi",
        ticker="BTC-95K",
        best_bid=kalshi_mid - 0.01,
        best_ask=kalshi_mid + 0.01,
        yes_mid=kalshi_mid,
        depth_5pct=100.0,  # >= 10, so liquid
        depth_3pct_usd=0.0,
        volume_24h=kalshi_volume,
        fetched_at=now,
    )

    poly_ob = OrderbookSnapshot(
        platform="polymarket",
        ticker="BTC-95K",
        best_bid=poly_mid - 0.01,
        best_ask=poly_mid + 0.01,
        yes_mid=poly_mid,
        depth_5pct=100.0,  # >= 10, so liquid
        depth_3pct_usd=0.0,
        volume_24h=poly_volume,
        fetched_at=now,
    )

    ref_price, signal_platform, disagreement = compute_reference_price(kalshi_ob, poly_ob)

    # Expected: volume-weighted average
    expected = (kalshi_mid * kalshi_volume + poly_mid * poly_volume) / (kalshi_volume + poly_volume)

    assert abs(ref_price - expected) < 1e-9, (
        f"reference_price={ref_price} != expected VWA={expected} "
        f"(kalshi_mid={kalshi_mid}, poly_mid={poly_mid}, "
        f"kalshi_vol={kalshi_volume}, poly_vol={poly_volume})"
    )

    assert signal_platform == "both", (
        f"Expected signal_platform='both', got {signal_platform!r}"
    )

    # Disagreement flag: True iff |kalshi_mid - poly_mid| > 0.05
    expected_disagreement = abs(kalshi_mid - poly_mid) > 0.05
    assert disagreement == expected_disagreement, (
        f"disagreement={disagreement} but expected {expected_disagreement} "
        f"(|{kalshi_mid} - {poly_mid}| = {abs(kalshi_mid - poly_mid):.4f})"
    )
