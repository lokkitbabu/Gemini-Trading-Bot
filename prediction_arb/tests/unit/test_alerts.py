"""
Unit tests for AlertManager (Task 14.7).
"""

import pytest
from unittest.mock import AsyncMock, patch

from prediction_arb.bot.alerts import AlertManager


@pytest.mark.asyncio
async def test_drawdown_alert_sent():
    """Alert sent on drawdown suspension."""
    am = AlertManager(channel="webhook", webhook_url="http://test", dedup_window_seconds=0)
    with patch.object(am, "_send_webhook", new_callable=AsyncMock) as mock_send:
        await am.send_drawdown_alert(drawdown_pct=0.25, available_capital=750.0)
        mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_suppresses_second_alert():
    """Second identical alert within dedup window is suppressed."""
    am = AlertManager(channel="webhook", webhook_url="http://test", dedup_window_seconds=300)
    with patch.object(am, "_send_webhook", new_callable=AsyncMock) as mock_send:
        await am.send_alert("test", alert_type="test_type")
        await am.send_alert("test", alert_type="test_type")
        assert mock_send.call_count == 1


@pytest.mark.asyncio
async def test_graceful_failure_on_unavailable_channel():
    """Graceful failure when channel is unavailable — no exception raised."""
    am = AlertManager(channel="webhook", webhook_url="http://unreachable-host-xyz")
    # Should not raise
    await am.send_alert("test message", alert_type="test")


@pytest.mark.asyncio
async def test_high_spread_triggers_alert():
    """High-spread opportunity triggers alert."""
    am = AlertManager(
        channel="webhook",
        webhook_url="http://test",
        alert_spread_threshold=0.10,
        dedup_window_seconds=0,
    )
    with patch.object(am, "_send_webhook", new_callable=AsyncMock) as mock_send:
        await am.send_high_spread_alert(opportunity_id="opp-1", spread_pct=0.25)
        mock_send.assert_called_once()
