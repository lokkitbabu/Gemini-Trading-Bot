"""
Backtesting mode for the prediction arbitrage bot.

Loads historical Opportunity records from StateStore, replays them through
ArbitrageEngine.rank() and RiskManager.evaluate(), simulates GeminiPosition
fills at recorded entry prices, and computes a P&L summary.

Usage:
    python -m prediction_arb.bot.main --backtest [--from YYYY-MM-DD] [--to YYYY-MM-DD]

Output:
    stdout — structured JSON summary
    stderr — human-readable table
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from prediction_arb.bot.engine import ArbitrageEngine, Opportunity
from prediction_arb.bot.executor import GeminiPosition
from prediction_arb.bot.models import OpportunityModel
from prediction_arb.bot.risk import Portfolio, RiskManager

if TYPE_CHECKING:
    from prediction_arb.bot.config import Config
    from prediction_arb.bot.state import StateStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# DB query helper
# ---------------------------------------------------------------------------


async def _get_opportunities_in_window(
    state_store: "StateStore",
    from_ts: datetime,
    to_ts: datetime,
) -> list[Opportunity]:
    """
    Query OpportunityModel rows between from_ts and to_ts, ordered by detected_at.
    Returns a list of Opportunity domain objects.
    """
    if state_store._session_factory is None:
        raise RuntimeError("StateStore.init() must be called before use")

    async with state_store._session() as session:
        result = await session.execute(
            select(OpportunityModel)
            .where(
                OpportunityModel.detected_at >= from_ts,
                OpportunityModel.detected_at <= to_ts,
            )
            .order_by(OpportunityModel.detected_at)
        )
        rows = result.scalars().all()

    opportunities: list[Opportunity] = []
    for row in rows:
        opp = Opportunity(
            id=row.id,
            detected_at=row.detected_at,
            event_title=row.event_title,
            asset=row.asset,
            price_level=row.price_level,
            resolution_date=row.resolution_date,
            signal_platform=row.signal_platform,
            signal_event_id=row.signal_event_id,
            signal_yes_price=row.signal_yes_price,
            signal_volume=row.signal_volume,
            gemini_event_id=row.gemini_event_id,
            gemini_yes_price=row.gemini_yes_price,
            gemini_volume=row.gemini_volume,
            gemini_bid=row.gemini_bid,
            gemini_ask=row.gemini_ask,
            gemini_depth=row.gemini_depth,
            spread=row.spread,
            spread_pct=row.spread_pct,
            direction=row.direction,
            entry_price=row.entry_price,
            kelly_fraction=row.kelly_fraction,
            match_confidence=row.match_confidence,
            days_to_resolution=row.days_to_resolution,
            risk_score=row.risk_score,
            status=row.status,
            signal_disagreement=row.signal_disagreement,
            inverted=row.inverted,
            price_age_seconds=row.price_age_seconds,
        )
        opportunities.append(opp)

    return opportunities


# ---------------------------------------------------------------------------
# Equity curve statistics
# ---------------------------------------------------------------------------


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum peak-to-trough drawdown from an equity curve."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compute_sharpe(equity_curve: list[float]) -> float:
    """
    Compute annualised Sharpe ratio from an equity curve.

    Assumes 252 trading days per year and risk-free rate of 0.
    Returns 0.0 if std of daily returns is 0 or fewer than 2 data points.
    """
    if len(equity_curve) < 2:
        return 0.0

    daily_returns = [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        if equity_curve[i - 1] != 0 else 0.0
        for i in range(1, len(equity_curve))
    ]

    n = len(daily_returns)
    mean_r = sum(daily_returns) / n
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / n
    std_r = math.sqrt(variance)

    if std_r == 0.0:
        return 0.0

    return (mean_r / std_r) * math.sqrt(252)


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------


def _simulate(
    opportunities: list[Opportunity],
    config: "Config",
) -> dict:
    """
    Replay opportunities through ArbitrageEngine.rank() and RiskManager.evaluate().

    Returns a summary dict with all required fields.
    """
    engine = ArbitrageEngine(
        orderbook_cache=None,
        max_price_age_seconds=config.max_price_age_seconds,
        max_position_pct=config.max_position_pct,
    )
    risk_manager = RiskManager(
        max_positions=config.max_positions,
        max_position_pct=config.max_position_pct,
        max_drawdown_pct=config.max_drawdown_pct,
        min_spread_pct=config.min_spread_pct,
        min_confidence=config.min_confidence,
        max_risk=config.max_risk,
        max_price_age_seconds=config.max_price_age_seconds,
        min_gemini_depth_usd=config.min_gemini_depth_usd,
        max_opportunities_per_scan=config.max_opportunities_per_scan,
    )

    # Sort by detected_at for determinism (already ordered from DB, but enforce here)
    sorted_opps = sorted(opportunities, key=lambda o: o.detected_at)

    # Rank using engine (spread_pct desc, risk_score asc)
    ranked = engine.rank(sorted_opps)

    portfolio = Portfolio(
        open_positions=0,
        available_capital=config.capital,
        peak_capital=config.capital,
        realized_pnl=0.0,
    )

    # Equity curve starts at initial capital
    equity_curve: list[float] = [config.capital]
    simulated_positions: list[GeminiPosition] = []

    risk_manager.reset_scan_counter()

    for opp in ranked:
        decision = risk_manager.evaluate(opp, portfolio)
        if not decision.allowed:
            continue

        # Compute position size
        size_usd = decision.clamped_size or (opp.kelly_fraction * portfolio.available_capital)
        if size_usd <= 0 or opp.entry_price <= 0:
            continue

        quantity = math.floor(size_usd / opp.entry_price)
        if quantity <= 0:
            continue

        # Use signal_yes_price as the resolved price proxy
        resolved_price = opp.signal_yes_price

        # Compute gross P&L for this position
        # For YES positions: profit = (resolved - entry) * qty
        # For NO positions: profit = (entry - resolved) * qty  (NO wins when price falls)
        if opp.direction == "buy_yes":
            gross_pnl_pos = (resolved_price - opp.entry_price) * quantity
        else:
            gross_pnl_pos = (opp.entry_price - resolved_price) * quantity

        fee = quantity * config.fee_per_contract
        net_pnl_pos = gross_pnl_pos - fee

        # Build simulated position record
        side = "yes" if opp.direction == "buy_yes" else "no"
        pos = GeminiPosition(
            opportunity_id=opp.id,
            event_id=opp.gemini_event_id,
            side=side,
            quantity=quantity,
            entry_price=opp.entry_price,
            size_usd=size_usd,
            exit_strategy="hold_to_resolution",
            target_exit_price=resolved_price,
            stop_loss_price=opp.entry_price * (1.0 - config.stop_loss_pct),
            status="closed",
            ref_price=opp.signal_yes_price,
            days_to_resolution=opp.days_to_resolution,
            exit_price=resolved_price,
            realized_pnl=net_pnl_pos,
        )
        simulated_positions.append(pos)

        # Update portfolio state
        portfolio.open_positions += 1
        portfolio.available_capital += net_pnl_pos
        if portfolio.available_capital > portfolio.peak_capital:
            portfolio.peak_capital = portfolio.available_capital
        portfolio.realized_pnl += net_pnl_pos

        # Append to equity curve after each trade
        equity_curve.append(portfolio.available_capital)

    # Aggregate summary
    total_opportunities = len(opportunities)
    trades_simulated = len(simulated_positions)

    gross_pnl = sum(
        (pos.exit_price - pos.entry_price) * pos.quantity
        if pos.side == "yes"
        else (pos.entry_price - pos.exit_price) * pos.quantity  # type: ignore[operator]
        for pos in simulated_positions
    )
    total_fees = sum(pos.quantity * config.fee_per_contract for pos in simulated_positions)
    net_pnl = gross_pnl - total_fees

    winning = sum(
        1 for pos in simulated_positions
        if pos.realized_pnl is not None and pos.realized_pnl > 0
    )
    win_rate = winning / trades_simulated if trades_simulated > 0 else 0.0

    max_drawdown = _compute_max_drawdown(equity_curve)
    sharpe_ratio = _compute_sharpe(equity_curve)

    return {
        "total_opportunities": total_opportunities,
        "trades_simulated": trades_simulated,
        "gross_pnl": round(gross_pnl, 6),
        "net_pnl": round(net_pnl, 6),
        "win_rate": round(win_rate, 6),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe_ratio": round(sharpe_ratio, 6),
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit_json(summary: dict) -> None:
    """Emit summary as structured JSON to stdout."""
    print(json.dumps(summary, indent=2))


def _emit_table(summary: dict, from_ts: datetime, to_ts: datetime) -> None:
    """Emit human-readable summary table to stderr."""
    lines = [
        "",
        "=" * 52,
        "  BACKTEST SUMMARY",
        f"  Period : {from_ts.date()} → {to_ts.date()}",
        "=" * 52,
        f"  Total opportunities loaded : {summary['total_opportunities']:>10}",
        f"  Trades simulated           : {summary['trades_simulated']:>10}",
        f"  Gross P&L                  : {summary['gross_pnl']:>10.4f}",
        f"  Net P&L (after fees)       : {summary['net_pnl']:>10.4f}",
        f"  Win rate                   : {summary['win_rate']:>9.1%}",
        f"  Max drawdown               : {summary['max_drawdown']:>9.1%}",
        f"  Sharpe ratio (ann.)        : {summary['sharpe_ratio']:>10.4f}",
        "=" * 52,
        "",
    ]
    for line in lines:
        print(line, file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> tuple[datetime, datetime]:
    """Parse --from and --to CLI args; default to last 30 days."""
    parser = argparse.ArgumentParser(
        description="Prediction Arbitrage Backtester",
        add_help=False,
    )
    parser.add_argument("--from", dest="from_date", type=str, default=None)
    parser.add_argument("--to", dest="to_date", type=str, default=None)
    # Ignore unknown args (e.g. --backtest itself)
    args, _ = parser.parse_known_args()

    now = datetime.now(tz=timezone.utc)
    if args.to_date:
        to_ts = datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)
    else:
        to_ts = now

    if args.from_date:
        from_ts = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
    else:
        from_ts = to_ts - timedelta(days=30)

    return from_ts, to_ts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def backtest_main() -> None:
    """
    Main async entry point for backtesting mode.

    1. Loads config via ConfigService.load()
    2. Initialises StateStore and calls await state_store.init()
    3. Parses --from / --to CLI args (default: last 30 days)
    4. Loads historical Opportunity records from StateStore
    5. Replays through ArbitrageEngine + RiskManager (no API calls)
    6. Simulates GeminiPosition fills and computes P&L
    7. Emits JSON to stdout and human-readable table to stderr
    """
    from prediction_arb.bot.config import ConfigService
    from prediction_arb.bot.state import StateStore

    # Load config
    config_service = ConfigService()
    config = config_service.load()

    # Parse time window
    from_ts, to_ts = _parse_args()

    log.info(
        "backtest_starting",
        from_ts=from_ts.isoformat(),
        to_ts=to_ts.isoformat(),
    )

    # Initialise StateStore (no migrations in backtest mode)
    state_store = StateStore(config.database_url)
    await state_store.init()

    try:
        # Load historical opportunities
        opportunities = await _get_opportunities_in_window(state_store, from_ts, to_ts)

        log.info(
            "backtest_opportunities_loaded",
            count=len(opportunities),
            from_ts=from_ts.isoformat(),
            to_ts=to_ts.isoformat(),
        )

        # Run simulation (pure in-memory, no API calls)
        summary = _simulate(opportunities, config)

        log.info("backtest_complete", **summary)

        # Emit outputs
        _emit_json(summary)
        _emit_table(summary, from_ts, to_ts)

    finally:
        await state_store.close()
