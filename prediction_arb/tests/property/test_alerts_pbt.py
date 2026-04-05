# Feature: prediction-arbitrage-production
# Property 20: fire identical alert N times within dedup window → exactly 1 notification sent
# Property 21: AlertManager enqueues exactly 1 alert for high-spread opportunity

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from prediction_arb.bot.alerts import AlertManager
from prediction_arb.bot.engine import Opportunity

_ALERT_SPREAD_THRESHOLD = 0.20


# ---------------------------------------------------------------------------
# Property 20: deduplication — N identical alerts within window → exactly 1 sent
# ---------------------------------------------------------------------------

@given(
    st.integers(min_value=2, max_value=20),
    st.floats(min_value=0.0, max_value=299.0, allow_nan=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_20_alert_deduplication(n_alerts: int, elapsed_seconds: float) -> None:
    """
    Property 20: fire identical alert N times within ALERT_DEDUP_WINDOW seconds;
    assert exactly 1 notification is sent (all subsequent are deduplicated).
    """
    dedup_window = 300  # seconds

    delivery_count = 0

    async def _mock_deliver(message, level, alert_type, skip_dedup=False):
        nonlocal delivery_count
        delivery_count += 1

    manager = AlertManager(
        channel="webhook",
        webhook_url="http://fake-webhook.example.com",
        dedup_window_seconds=dedup_window,
        alert_spread_threshold=_ALERT_SPREAD_THRESHOLD,
    )

    # Patch the internal delivery to count calls without actually sending
    async def _run():
        with patch.object(manager, "_attempt_delivery", new=AsyncMock(return_value=True)) as mock_deliver:
            # Fire N identical alerts
            for _ in range(n_alerts):
                await manager.send_alert(
                    message="Test alert",
                    level="info",
                    alert_type="test_dedup_alert",
                )
            return mock_deliver.call_count

    call_count = asyncio.get_event_loop().run_until_complete(_run())

    # Only 1 delivery should have been attempted (first alert goes through, rest are deduped)
    assert call_count == 1, (
        f"Expected exactly 1 delivery for {n_alerts} identical alerts within dedup window, "
        f"got {call_count}"
    )


# ---------------------------------------------------------------------------
# Property 21: high-spread opportunity triggers exactly 1 alert
# ---------------------------------------------------------------------------

@given(
    st.floats(min_value=_ALERT_SPREAD_THRESHOLD + 0.001, max_value=1.0, allow_nan=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_21_high_spread_triggers_alert(spread_pct: float) -> None:
    """
    Property 21: for any opportunity with spread_pct > ALERT_SPREAD_THRESHOLD,
    AlertManager sends exactly 1 alert (subject to dedup).
    """
    manager = AlertManager(
        channel="webhook",
        webhook_url="http://fake-webhook.example.com",
        dedup_window_seconds=300,
        alert_spread_threshold=_ALERT_SPREAD_THRESHOLD,
    )

    opp = Opportunity(
        id="test-opp-1",
        spread_pct=spread_pct,
        event_title="BTC above 95k",
        signal_platform="kalshi",
        gemini_event_id="g1",
        direction="buy_yes",
        entry_price=0.50,
        match_confidence=0.90,
        risk_score=0.20,
    )

    async def _run():
        with patch.object(manager, "_attempt_delivery", new=AsyncMock(return_value=True)) as mock_deliver:
            await manager.send_high_spread_alert(
                opportunity_id=opp.id,
                spread_pct=opp.spread_pct,
            )
            return mock_deliver.call_count

    call_count = asyncio.get_event_loop().run_until_complete(_run())

    assert call_count == 1, (
        f"Expected exactly 1 alert for spread_pct={spread_pct:.4f} "
        f"(threshold={_ALERT_SPREAD_THRESHOLD}), got {call_count}"
    )


# ---------------------------------------------------------------------------
# Sanity: below-threshold spread does NOT trigger alert
# ---------------------------------------------------------------------------

@given(
    st.floats(min_value=0.0, max_value=_ALERT_SPREAD_THRESHOLD, allow_nan=False),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_21b_below_threshold_no_alert(spread_pct: float) -> None:
    """
    Sanity: spread_pct <= ALERT_SPREAD_THRESHOLD does NOT trigger an alert.
    """
    manager = AlertManager(
        channel="webhook",
        webhook_url="http://fake-webhook.example.com",
        dedup_window_seconds=300,
        alert_spread_threshold=_ALERT_SPREAD_THRESHOLD,
    )

    async def _run():
        with patch.object(manager, "_attempt_delivery", new=AsyncMock(return_value=True)) as mock_deliver:
            await manager.send_high_spread_alert(
                opportunity_id="test-opp",
                spread_pct=spread_pct,
            )
            return mock_deliver.call_count

    call_count = asyncio.get_event_loop().run_until_complete(_run())
    assert call_count == 0, (
        f"Expected 0 alerts for spread_pct={spread_pct:.4f} <= threshold, got {call_count}"
    )
