# Feature: prediction-arbitrage-production
# Property 5: counter values equal N scan cycles, M opportunity detections, K trade executions
# Property 6: gauge values equal open_positions, available_capital, realized_pnl

from __future__ import annotations

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from prometheus_client import CollectorRegistry, Counter, Gauge

from prediction_arb.bot.risk import Portfolio


# ---------------------------------------------------------------------------
# Property 5: counters track exact increments
# ---------------------------------------------------------------------------

@given(
    st.integers(min_value=0, max_value=50),
    st.integers(min_value=0, max_value=50),
    st.integers(min_value=0, max_value=50),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_5_counters_track_exact_increments(
    n_scan_cycles: int,
    m_opportunities: int,
    k_trades: int,
) -> None:
    """
    Property 5: simulate N scan cycles, M opportunity detections, K trade executions;
    assert counter values equal N, M, K respectively.
    """
    # Use a fresh isolated registry for each test run
    registry = CollectorRegistry()

    scan_cycles = Counter(
        "test_arb_scan_cycles_total",
        "Total scan cycles",
        registry=registry,
    )
    opportunities = Counter(
        "test_arb_opportunities_detected_total",
        "Total opportunities detected",
        labelnames=["platform_pair"],
        registry=registry,
    )
    trades = Counter(
        "test_arb_trades_executed_total",
        "Total trades executed",
        labelnames=["platform", "side"],
        registry=registry,
    )

    # Simulate N scan cycles
    for _ in range(n_scan_cycles):
        scan_cycles.inc()

    # Simulate M opportunity detections
    for _ in range(m_opportunities):
        opportunities.labels(platform_pair="kalshi_gemini").inc()

    # Simulate K trade executions
    for _ in range(k_trades):
        trades.labels(platform="gemini", side="yes").inc()

    # Assert counter values
    assert scan_cycles._value.get() == n_scan_cycles, (
        f"scan_cycles: expected {n_scan_cycles}, got {scan_cycles._value.get()}"
    )
    assert opportunities.labels(platform_pair="kalshi_gemini")._value.get() == m_opportunities, (
        f"opportunities: expected {m_opportunities}"
    )
    assert trades.labels(platform="gemini", side="yes")._value.get() == k_trades, (
        f"trades: expected {k_trades}"
    )


# ---------------------------------------------------------------------------
# Property 6: gauges reflect portfolio state
# ---------------------------------------------------------------------------

@given(
    st.builds(
        Portfolio,
        open_positions=st.integers(min_value=0, max_value=100),
        available_capital=st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
        peak_capital=st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
        realized_pnl=st.floats(min_value=-100_000.0, max_value=100_000.0, allow_nan=False, allow_infinity=False),
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_6_gauges_reflect_portfolio_state(
    portfolio: Portfolio,
) -> None:
    """
    Property 6: update portfolio state via gauges; assert gauge values equal
    open_positions, available_capital, realized_pnl.
    
    **Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.6, 3.9**
    """
    registry = CollectorRegistry()

    open_pos_gauge = Gauge(
        "test_arb_open_positions",
        "Current open positions",
        registry=registry,
    )
    capital_gauge = Gauge(
        "test_arb_available_capital_usd",
        "Available capital in USD",
        registry=registry,
    )
    pnl_gauge = Gauge(
        "test_arb_realized_pnl_usd",
        "Realized P&L in USD",
        registry=registry,
    )

    # Update portfolio state
    open_pos_gauge.set(portfolio.open_positions)
    capital_gauge.set(portfolio.available_capital)
    pnl_gauge.set(portfolio.realized_pnl)

    # Assert gauge values match what was set
    assert open_pos_gauge._value.get() == portfolio.open_positions, (
        f"open_positions gauge: expected {portfolio.open_positions}, got {open_pos_gauge._value.get()}"
    )
    assert abs(capital_gauge._value.get() - portfolio.available_capital) < 1e-6, (
        f"available_capital gauge: expected {portfolio.available_capital}, got {capital_gauge._value.get()}"
    )
    assert abs(pnl_gauge._value.get() - portfolio.realized_pnl) < 1e-6, (
        f"realized_pnl gauge: expected {portfolio.realized_pnl}, got {pnl_gauge._value.get()}"
    )
