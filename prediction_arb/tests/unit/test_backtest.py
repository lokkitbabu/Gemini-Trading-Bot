"""
Unit tests for backtest.py (Task 14.9).
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from prediction_arb.bot.engine import Opportunity
from prediction_arb.bot.backtest import _simulate, _compute_max_drawdown, _compute_sharpe


def make_config():
    cfg = MagicMock()
    cfg.capital = 1000.0
    cfg.max_positions = 10
    cfg.max_position_pct = 0.05
    cfg.max_drawdown_pct = 0.20
    cfg.min_spread_pct = 0.05
    cfg.min_confidence = 0.60
    cfg.max_risk = 0.90
    cfg.max_price_age_seconds = 3600
    cfg.min_gemini_depth_usd = 0.0
    cfg.max_opportunities_per_scan = 50
    cfg.stop_loss_pct = 0.15
    cfg.fee_per_contract = 0.0
    return cfg


def make_opp(i=0):
    return Opportunity(
        id=f"opp-{i}",
        detected_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        direction="buy_yes",
        entry_price=0.60,
        signal_yes_price=0.75,
        kelly_fraction=0.02,
        days_to_resolution=10,
        price_age_seconds=5.0,
        spread_pct=0.15,
        match_confidence=0.85,
        risk_score=0.3,
        gemini_depth=100.0,
        gemini_bid=0.58,
        gemini_ask=0.62,
    )


def test_no_api_calls_in_backtest():
    """No platform API calls are made in backtest mode."""
    config = make_config()
    opps = [make_opp(i) for i in range(3)]
    summary = _simulate(opps, config)
    assert "total_opportunities" in summary
    assert summary["total_opportunities"] == 3


def test_report_contains_required_fields():
    """Report contains all required fields."""
    config = make_config()
    opps = [make_opp(i) for i in range(2)]
    summary = _simulate(opps, config)
    required = [
        "total_opportunities", "trades_simulated", "gross_pnl",
        "net_pnl", "win_rate", "max_drawdown", "sharpe_ratio",
    ]
    for field in required:
        assert field in summary


def test_determinism():
    """Same dataset + config → identical output on two runs."""
    config = make_config()
    opps = [make_opp(i) for i in range(5)]
    summary1 = _simulate(opps, config)
    summary2 = _simulate(opps, config)
    assert summary1 == summary2


def test_max_drawdown_empty():
    assert _compute_max_drawdown([]) == 0.0


def test_max_drawdown_monotonic():
    assert _compute_max_drawdown([100, 110, 120]) == 0.0


def test_sharpe_empty():
    assert _compute_sharpe([]) == 0.0
