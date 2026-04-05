"""
Unit tests for Executor (Task 14.6).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from prediction_arb.bot.executor import Executor, GeminiPosition
from prediction_arb.bot.engine import Opportunity


def make_opp(**kwargs):
    defaults = dict(
        id="opp-1",
        gemini_event_id="evt-1",
        direction="buy_yes",
        entry_price=0.60,
        signal_yes_price=0.75,
        kelly_fraction=0.02,
        days_to_resolution=10,
        price_age_seconds=5.0,
        gemini_bid=0.58,
        gemini_ask=0.62,
        gemini_depth=100.0,
        spread_pct=0.15,
        match_confidence=0.85,
        risk_score=0.3,
    )
    defaults.update(kwargs)
    return Opportunity(**defaults)


def make_executor(dry_run=True):
    gemini = AsyncMock()
    state = AsyncMock()
    sse = AsyncMock()
    cache = MagicMock()
    return Executor(
        gemini_client=gemini,
        state_store=state,
        sse_broadcaster=sse,
        orderbook_cache=cache,
        dry_run=dry_run,
    ), gemini, state, sse


@pytest.mark.asyncio
async def test_dry_run_no_api_call():
    """Dry-run: no Gemini API call; position persisted with simulated fill."""
    executor, gemini, state, sse = make_executor(dry_run=True)
    opp = make_opp()
    pos = await executor.execute(opp, size_usd=100.0)
    gemini.place_order.assert_not_called()
    state.save_position.assert_called_once()
    assert pos.status == "filled"


@pytest.mark.asyncio
async def test_live_mode_calls_place_order():
    """Live mode: GeminiClient.place_order called with correct args."""
    executor, gemini, state, sse = make_executor(dry_run=False)
    gemini.place_order.return_value = MagicMock(order_id="ord-1")
    opp = make_opp()
    pos = await executor.execute(opp, size_usd=100.0)
    gemini.place_order.assert_called_once()
    call_kwargs = gemini.place_order.call_args.kwargs
    assert call_kwargs["event_id"] == "evt-1"
    assert call_kwargs["side"] == "yes"
    assert pos.status == "filled"


@pytest.mark.asyncio
async def test_stale_price_aborts():
    """Freshness re-check: abort if price is stale."""
    executor, _, _, _ = make_executor()
    opp = make_opp(price_age_seconds=999.0)
    with pytest.raises(ValueError, match="Stale"):
        await executor.execute(opp, size_usd=100.0)


@pytest.mark.asyncio
async def test_exit_strategy_assignment():
    """exit_strategy set based on days_to_resolution."""
    executor, _, _, _ = make_executor()
    opp_long = make_opp(days_to_resolution=30)
    pos_long = await executor.execute(opp_long, size_usd=100.0)
    assert pos_long.exit_strategy == "target_convergence"

    opp_short = make_opp(days_to_resolution=3)
    pos_short = await executor.execute(opp_short, size_usd=100.0)
    assert pos_short.exit_strategy == "hold_to_resolution"


@pytest.mark.asyncio
async def test_close_position_persists_exit_fields():
    """close_position places limit sell and persists exit fields."""
    executor, gemini, state, sse = make_executor(dry_run=False)
    ob = MagicMock()
    ob.best_bid = 0.70
    gemini.get_orderbook.return_value = ob
    gemini.place_order.return_value = MagicMock(order_id="ord-2")
    pos = GeminiPosition(
        id="pos-1", event_id="evt-1", side="yes",
        quantity=100, entry_price=0.60, size_usd=60.0,
        exit_strategy="target_convergence",
        target_exit_price=0.72, stop_loss_price=0.51,
        status="filled", ref_price=0.75,
    )
    await executor.close_position(pos, reason="convergence")
    assert pos.status == "closed"
    assert pos.exit_price == 0.70
    assert pos.realized_pnl is not None
    state.update_position.assert_called_once()
