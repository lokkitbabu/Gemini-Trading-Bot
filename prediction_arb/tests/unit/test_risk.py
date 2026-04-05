"""
Unit tests for RiskManager (risk.py) — task 5.2.

Tests cover all 9 rejection conditions, suspension/resume, scan cap,
and the Portfolio.drawdown_pct property.
"""

from __future__ import annotations

import pytest

from prediction_arb.bot.engine import Opportunity
from prediction_arb.bot.risk import (
    MAX_DRAWDOWN_PCT,
    MAX_OPPORTUNITIES_PER_SCAN,
    MAX_POSITION_PCT,
    MAX_POSITIONS,
    MAX_PRICE_AGE_SECONDS,
    MIN_CONFIDENCE,
    MIN_GEMINI_DEPTH_USD,
    MIN_SPREAD_PCT,
    MAX_RISK,
    Portfolio,
    RiskDecision,
    RiskManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_portfolio(
    open_positions: int = 0,
    available_capital: float = 1000.0,
    peak_capital: float = 1000.0,
) -> Portfolio:
    return Portfolio(
        open_positions=open_positions,
        available_capital=available_capital,
        peak_capital=peak_capital,
    )


def make_opportunity(
    spread_pct: float = 0.15,
    match_confidence: float = 0.85,
    risk_score: float = 0.30,
    price_age_seconds: float = 10.0,
    gemini_depth: float = 200.0,
    gemini_bid: float | None = 0.56,
    gemini_ask: float | None = 0.60,
    spread: float = 0.14,
    kelly_fraction: float = 0.05,
) -> Opportunity:
    opp = Opportunity()
    opp.spread_pct = spread_pct
    opp.match_confidence = match_confidence
    opp.risk_score = risk_score
    opp.price_age_seconds = price_age_seconds
    opp.gemini_depth = gemini_depth
    opp.gemini_bid = gemini_bid
    opp.gemini_ask = gemini_ask
    opp.spread = spread
    opp.kelly_fraction = kelly_fraction
    return opp


def make_risk_manager(**kwargs) -> RiskManager:
    """Create a RiskManager with default config, overridable via kwargs."""
    defaults = dict(
        max_positions=MAX_POSITIONS,
        max_position_pct=MAX_POSITION_PCT,
        max_drawdown_pct=MAX_DRAWDOWN_PCT,
        min_spread_pct=MIN_SPREAD_PCT,
        min_confidence=MIN_CONFIDENCE,
        max_risk=MAX_RISK,
        max_price_age_seconds=MAX_PRICE_AGE_SECONDS,
        min_gemini_depth_usd=MIN_GEMINI_DEPTH_USD,
        max_opportunities_per_scan=MAX_OPPORTUNITIES_PER_SCAN,
    )
    defaults.update(kwargs)
    return RiskManager(**defaults)


# ---------------------------------------------------------------------------
# Portfolio.drawdown_pct tests
# ---------------------------------------------------------------------------


class TestPortfolioDrawdown:
    def test_zero_drawdown_when_at_peak(self):
        p = Portfolio(available_capital=1000.0, peak_capital=1000.0)
        assert p.drawdown_pct == pytest.approx(0.0)

    def test_drawdown_computed_correctly(self):
        p = Portfolio(available_capital=800.0, peak_capital=1000.0)
        assert p.drawdown_pct == pytest.approx(0.20)

    def test_zero_drawdown_when_above_peak(self):
        # available > peak (shouldn't happen in practice, but must not go negative)
        p = Portfolio(available_capital=1100.0, peak_capital=1000.0)
        assert p.drawdown_pct == pytest.approx(0.0)

    def test_zero_drawdown_when_peak_is_zero(self):
        p = Portfolio(available_capital=0.0, peak_capital=0.0)
        assert p.drawdown_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Check 1: position cap
# ---------------------------------------------------------------------------


class TestPositionCap:
    def test_deny_when_at_max_positions(self):
        rm = make_risk_manager(max_positions=5)
        portfolio = make_portfolio(open_positions=5)
        opp = make_opportunity()
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "position cap"

    def test_allow_when_below_max_positions(self):
        rm = make_risk_manager(max_positions=5)
        portfolio = make_portfolio(open_positions=4)
        opp = make_opportunity()
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Check 2: position size clamp
# ---------------------------------------------------------------------------


class TestPositionSizeClamp:
    def test_clamp_when_size_exceeds_max(self):
        rm = make_risk_manager(max_position_pct=0.05)
        portfolio = make_portfolio(available_capital=1000.0)
        opp = make_opportunity()
        # Pass a size larger than 5% of 1000 = 50
        decision = rm.evaluate(opp, portfolio, position_size=200.0)
        assert decision.allowed is True
        assert decision.clamped_size == pytest.approx(50.0)

    def test_no_clamp_when_size_within_limit(self):
        rm = make_risk_manager(max_position_pct=0.05)
        portfolio = make_portfolio(available_capital=1000.0)
        opp = make_opportunity()
        decision = rm.evaluate(opp, portfolio, position_size=40.0)
        assert decision.allowed is True
        assert decision.clamped_size is None


# ---------------------------------------------------------------------------
# Check 3: drawdown kill-switch
# ---------------------------------------------------------------------------


class TestDrawdownKillSwitch:
    def test_deny_and_suspend_when_drawdown_exceeded(self):
        rm = make_risk_manager(max_drawdown_pct=0.20)
        # 21% drawdown
        portfolio = make_portfolio(available_capital=790.0, peak_capital=1000.0)
        opp = make_opportunity()
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "drawdown"
        assert rm.is_suspended() is True

    def test_allow_when_drawdown_at_threshold(self):
        rm = make_risk_manager(max_drawdown_pct=0.20)
        # Exactly 20% drawdown — NOT exceeded (must be strictly greater)
        portfolio = make_portfolio(available_capital=800.0, peak_capital=1000.0)
        opp = make_opportunity()
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True

    def test_suspended_denies_all_subsequent(self):
        rm = make_risk_manager(max_drawdown_pct=0.20)
        portfolio_bad = make_portfolio(available_capital=790.0, peak_capital=1000.0)
        portfolio_good = make_portfolio(available_capital=1000.0, peak_capital=1000.0)
        opp = make_opportunity()

        # First call triggers suspension
        rm.evaluate(opp, portfolio_bad)
        assert rm.is_suspended() is True

        # Subsequent call with good portfolio still denied
        decision = rm.evaluate(opp, portfolio_good)
        assert decision.allowed is False
        assert decision.reason == "suspended"

    def test_resume_lifts_suspension(self):
        rm = make_risk_manager(max_drawdown_pct=0.20)
        portfolio_bad = make_portfolio(available_capital=790.0, peak_capital=1000.0)
        portfolio_good = make_portfolio(available_capital=1000.0, peak_capital=1000.0)
        opp = make_opportunity()

        rm.evaluate(opp, portfolio_bad)
        assert rm.is_suspended() is True

        rm.resume()
        assert rm.is_suspended() is False

        # Now a good portfolio should be allowed
        decision = rm.evaluate(opp, portfolio_good)
        assert decision.allowed is True

    def test_resume_is_idempotent_when_not_suspended(self):
        rm = make_risk_manager()
        rm.resume()  # should not raise
        assert rm.is_suspended() is False


# ---------------------------------------------------------------------------
# Check 4: minimum spread
# ---------------------------------------------------------------------------


class TestMinSpread:
    def test_deny_when_spread_too_small(self):
        rm = make_risk_manager(min_spread_pct=0.08)
        portfolio = make_portfolio()
        opp = make_opportunity(spread_pct=0.05)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "spread too small"

    def test_allow_when_spread_at_minimum(self):
        rm = make_risk_manager(min_spread_pct=0.08)
        portfolio = make_portfolio()
        opp = make_opportunity(spread_pct=0.08)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Check 5: minimum confidence
# ---------------------------------------------------------------------------


class TestMinConfidence:
    def test_deny_when_confidence_too_low(self):
        rm = make_risk_manager(min_confidence=0.70)
        portfolio = make_portfolio()
        opp = make_opportunity(match_confidence=0.65)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "low confidence"

    def test_allow_when_confidence_at_minimum(self):
        rm = make_risk_manager(min_confidence=0.70)
        portfolio = make_portfolio()
        opp = make_opportunity(match_confidence=0.70)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Check 6: maximum risk score
# ---------------------------------------------------------------------------


class TestMaxRisk:
    def test_deny_when_risk_too_high(self):
        rm = make_risk_manager(max_risk=0.80)
        portfolio = make_portfolio()
        opp = make_opportunity(risk_score=0.85)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "risk too high"

    def test_allow_when_risk_at_maximum(self):
        rm = make_risk_manager(max_risk=0.80)
        portfolio = make_portfolio()
        opp = make_opportunity(risk_score=0.80)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Check 7: stale price
# ---------------------------------------------------------------------------


class TestStalePrice:
    def test_deny_when_price_stale(self):
        rm = make_risk_manager(max_price_age_seconds=60)
        portfolio = make_portfolio()
        opp = make_opportunity(price_age_seconds=90.0)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "stale_price"

    def test_allow_when_price_fresh(self):
        rm = make_risk_manager(max_price_age_seconds=60)
        portfolio = make_portfolio()
        opp = make_opportunity(price_age_seconds=30.0)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Check 8: insufficient liquidity
# ---------------------------------------------------------------------------


class TestInsufficientLiquidity:
    def test_deny_when_depth_too_low(self):
        rm = make_risk_manager(min_gemini_depth_usd=50.0)
        portfolio = make_portfolio()
        opp = make_opportunity(gemini_depth=30.0)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "insufficient_liquidity"

    def test_allow_when_depth_at_minimum(self):
        rm = make_risk_manager(min_gemini_depth_usd=50.0)
        portfolio = make_portfolio()
        opp = make_opportunity(gemini_depth=50.0)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Check 9: spread inside noise
# ---------------------------------------------------------------------------


class TestSpreadInsideNoise:
    def test_deny_when_spread_inside_bid_ask(self):
        rm = make_risk_manager()
        portfolio = make_portfolio()
        # gemini bid=0.68, ask=0.72 → gemini_spread=0.04
        # opp.spread=0.01 ≤ 0.04/2=0.02 → inside noise
        opp = make_opportunity(
            gemini_bid=0.68,
            gemini_ask=0.72,
            spread=0.01,
        )
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is False
        assert decision.reason == "spread_inside_noise"

    def test_allow_when_spread_outside_bid_ask(self):
        rm = make_risk_manager()
        portfolio = make_portfolio()
        # gemini bid=0.56, ask=0.60 → gemini_spread=0.04
        # opp.spread=0.14 >> 0.04/2=0.02 → outside noise
        opp = make_opportunity(
            gemini_bid=0.56,
            gemini_ask=0.60,
            spread=0.14,
        )
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True

    def test_no_noise_check_when_no_bid_ask(self):
        rm = make_risk_manager()
        portfolio = make_portfolio()
        opp = make_opportunity(gemini_bid=None, gemini_ask=None, spread=0.14)
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Scan cap
# ---------------------------------------------------------------------------


class TestScanCap:
    def test_deny_when_scan_cap_exceeded(self):
        rm = make_risk_manager(max_opportunities_per_scan=3)
        portfolio = make_portfolio()
        opp = make_opportunity()

        # First 3 should pass
        for _ in range(3):
            d = rm.evaluate(opp, portfolio)
            assert d.allowed is True

        # 4th should be denied
        d = rm.evaluate(opp, portfolio)
        assert d.allowed is False
        assert d.reason == "scan cap exceeded"

    def test_reset_scan_counter_allows_again(self):
        rm = make_risk_manager(max_opportunities_per_scan=2)
        portfolio = make_portfolio()
        opp = make_opportunity()

        rm.evaluate(opp, portfolio)
        rm.evaluate(opp, portfolio)
        d = rm.evaluate(opp, portfolio)
        assert d.allowed is False

        rm.reset_scan_counter()
        d = rm.evaluate(opp, portfolio)
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Order error handling
# ---------------------------------------------------------------------------


class TestOrderErrorHandling:
    def test_handle_order_error_does_not_raise(self):
        rm = make_risk_manager()
        # Should log and not raise
        rm.handle_order_error("opp-123", ValueError("order rejected"), 50.0)


# ---------------------------------------------------------------------------
# Full allow path
# ---------------------------------------------------------------------------


class TestFullAllowPath:
    def test_all_checks_pass(self):
        rm = make_risk_manager()
        portfolio = make_portfolio(open_positions=0, available_capital=1000.0, peak_capital=1000.0)
        opp = make_opportunity(
            spread_pct=0.15,
            match_confidence=0.85,
            risk_score=0.30,
            price_age_seconds=10.0,
            gemini_depth=200.0,
            gemini_bid=0.56,
            gemini_ask=0.60,
            spread=0.14,
        )
        decision = rm.evaluate(opp, portfolio)
        assert decision.allowed is True
        assert decision.reason == "allowed"
