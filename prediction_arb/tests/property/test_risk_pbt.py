# Feature: prediction-arbitrage-production
# Property 7: evaluate() returns allowed=False iff any rejection condition holds
# Property 8: once suspended, is_suspended() returns True until resume() called
# Property 9: execution count equals min(len(opps), MAX_OPPORTUNITIES_PER_SCAN)

from __future__ import annotations

from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

from prediction_arb.bot.engine import Opportunity
from prediction_arb.bot.risk import (
    MAX_DRAWDOWN_PCT,
    MAX_OPPORTUNITIES_PER_SCAN,
    MAX_POSITION_PCT,
    MAX_POSITIONS,
    MAX_PRICE_AGE_SECONDS,
    MAX_RISK,
    MIN_CONFIDENCE,
    MIN_GEMINI_DEPTH_USD,
    MIN_SPREAD_PCT,
    Portfolio,
    RiskManager,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_opp_strategy = st.builds(
    Opportunity,
    spread_pct=st.floats(0.0, 0.5, allow_nan=False),
    match_confidence=st.floats(0.0, 1.0, allow_nan=False),
    risk_score=st.floats(0.0, 1.0, allow_nan=False),
    price_age_seconds=st.floats(0.0, 200.0, allow_nan=False),
    gemini_depth=st.floats(0.0, 500.0, allow_nan=False),
    gemini_bid=st.one_of(st.none(), st.floats(0.01, 0.49, allow_nan=False)),
    gemini_ask=st.one_of(st.none(), st.floats(0.51, 0.99, allow_nan=False)),
    spread=st.floats(0.0, 0.5, allow_nan=False),
    kelly_fraction=st.floats(0.0, 0.05, allow_nan=False),
    entry_price=st.floats(0.01, 0.99, allow_nan=False),
    direction=st.sampled_from(["buy_yes", "buy_no"]),
    status=st.just("pending"),
)

_portfolio_strategy = st.builds(
    Portfolio,
    open_positions=st.integers(0, MAX_POSITIONS + 2),
    available_capital=st.floats(100.0, 100_000.0, allow_nan=False),
    peak_capital=st.floats(100.0, 100_000.0, allow_nan=False),
    realized_pnl=st.floats(-10_000.0, 10_000.0, allow_nan=False),
)


def _any_rejection_condition(opp: Opportunity, portfolio: Portfolio) -> bool:
    """Return True if any rejection condition holds (mirrors RiskManager logic)."""
    # Check 1: position cap
    if portfolio.open_positions >= MAX_POSITIONS:
        return True
    # Check 3: drawdown
    if portfolio.drawdown_pct > MAX_DRAWDOWN_PCT:
        return True
    # Check 4: spread too small
    if opp.spread_pct < MIN_SPREAD_PCT:
        return True
    # Check 5: low confidence
    if opp.match_confidence < MIN_CONFIDENCE:
        return True
    # Check 6: risk too high
    if opp.risk_score > MAX_RISK:
        return True
    # Check 7: stale price
    if opp.price_age_seconds > MAX_PRICE_AGE_SECONDS:
        return True
    # Check 8: insufficient liquidity
    if opp.gemini_depth < MIN_GEMINI_DEPTH_USD:
        return True
    # Check 9: spread inside noise
    if opp.gemini_bid is not None and opp.gemini_ask is not None:
        gemini_spread = opp.gemini_ask - opp.gemini_bid
        if opp.spread <= gemini_spread / 2.0:
            return True
    return False


# ---------------------------------------------------------------------------
# Property 7: evaluate() returns allowed=False iff any rejection condition holds
# ---------------------------------------------------------------------------

@given(_opp_strategy, _portfolio_strategy)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_7_risk_evaluation_correctness(
    opp: Opportunity, portfolio: Portfolio
) -> None:
    """
    Property 7: evaluate() returns allowed=False iff any rejection condition holds.
    When allowed, clamped_size <= MAX_POSITION_PCT * available_capital.
    """
    # Ensure peak_capital >= available_capital to avoid negative drawdown
    if portfolio.peak_capital < portfolio.available_capital:
        portfolio.peak_capital = portfolio.available_capital

    rm = RiskManager()
    rm.reset_scan_counter()

    decision = rm.evaluate(opp, portfolio)

    should_deny = _any_rejection_condition(opp, portfolio)

    if should_deny:
        assert not decision.allowed, (
            f"Expected denied but got allowed. "
            f"opp.spread_pct={opp.spread_pct}, opp.match_confidence={opp.match_confidence}, "
            f"portfolio.open_positions={portfolio.open_positions}, "
            f"portfolio.drawdown_pct={portfolio.drawdown_pct:.4f}"
        )
    else:
        assert decision.allowed, (
            f"Expected allowed but got denied: reason={decision.reason!r}. "
            f"opp.spread_pct={opp.spread_pct}, opp.match_confidence={opp.match_confidence}"
        )
        # When allowed, clamped_size must be within bounds
        if decision.clamped_size is not None:
            max_size = MAX_POSITION_PCT * portfolio.available_capital
            assert decision.clamped_size <= max_size + 1e-9, (
                f"clamped_size {decision.clamped_size} > max_size {max_size}"
            )


# ---------------------------------------------------------------------------
# Property 8: once suspended, is_suspended() returns True until resume()
# ---------------------------------------------------------------------------

@given(
    st.floats(min_value=MAX_DRAWDOWN_PCT + 0.001, max_value=1.0, allow_nan=False),
    st.integers(min_value=1, max_value=10),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_8_suspension_persists_until_resume(
    drawdown_pct: float, n_subsequent_calls: int
) -> None:
    """
    Property 8: once suspended via drawdown breach, is_suspended() returns True
    for all subsequent calls until resume() is explicitly called.
    """
    rm = RiskManager()

    # Trigger suspension via drawdown
    peak = 1000.0
    available = peak * (1.0 - drawdown_pct)
    portfolio = Portfolio(
        open_positions=0,
        available_capital=available,
        peak_capital=peak,
        realized_pnl=0.0,
    )

    opp = Opportunity(
        spread_pct=0.20,
        match_confidence=0.90,
        risk_score=0.10,
        price_age_seconds=5.0,
        gemini_depth=200.0,
        kelly_fraction=0.02,
        entry_price=0.50,
        direction="buy_yes",
    )

    rm.reset_scan_counter()
    decision = rm.evaluate(opp, portfolio)
    assert not decision.allowed, "Expected denial due to drawdown"
    assert rm.is_suspended(), "Expected suspension after drawdown breach"

    # All subsequent calls must return is_suspended() = True
    for _ in range(n_subsequent_calls):
        assert rm.is_suspended(), "is_suspended() should remain True until resume()"

    # After resume(), is_suspended() must return False
    rm.resume()
    assert not rm.is_suspended(), "is_suspended() should return False after resume()"


# ---------------------------------------------------------------------------
# Property 9: execution count equals min(len(opps), MAX_OPPORTUNITIES_PER_SCAN)
# ---------------------------------------------------------------------------

@given(st.lists(_opp_strategy, min_size=0, max_size=100))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_9_scan_cap(opps: list[Opportunity]) -> None:
    """
    Property 9: the number of opportunities that pass the scan cap check equals
    min(len(opps), MAX_OPPORTUNITIES_PER_SCAN).
    """
    rm = RiskManager()
    rm.reset_scan_counter()

    # Use a portfolio that passes all other checks
    portfolio = Portfolio(
        open_positions=0,
        available_capital=100_000.0,
        peak_capital=100_000.0,
        realized_pnl=0.0,
    )

    # Use opportunities that pass all checks except the scan cap
    passing_opp = Opportunity(
        spread_pct=MIN_SPREAD_PCT + 0.05,
        match_confidence=MIN_CONFIDENCE + 0.05,
        risk_score=MAX_RISK - 0.05,
        price_age_seconds=MAX_PRICE_AGE_SECONDS - 5,
        gemini_depth=MIN_GEMINI_DEPTH_USD + 10.0,
        gemini_bid=None,
        gemini_ask=None,
        kelly_fraction=0.02,
        entry_price=0.50,
        direction="buy_yes",
    )

    allowed_count = 0
    for _ in opps:
        decision = rm.evaluate(passing_opp, portfolio)
        if decision.allowed:
            allowed_count += 1

    expected = min(len(opps), MAX_OPPORTUNITIES_PER_SCAN)
    assert allowed_count == expected, (
        f"Expected {expected} allowed, got {allowed_count} (len={len(opps)})"
    )
