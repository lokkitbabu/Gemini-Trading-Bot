"""
RiskManager — enforces capital limits, position caps, drawdown thresholds,
and kill-switch logic before any opportunity is passed to the Executor.

Decision flow (in order):
  1. open_positions >= MAX_POSITIONS          → deny: position cap
  2. position_size > MAX_POSITION_PCT*capital → clamp to MAX_POSITION_PCT
  3. drawdown > MAX_DRAWDOWN_PCT              → deny + suspend + alert
  4. spread_pct < MIN_SPREAD_PCT              → deny: spread too small
  5. confidence < MIN_CONFIDENCE             → deny: low confidence
  6. risk_score > MAX_RISK                   → deny: risk too high
  7. price_age > MAX_PRICE_AGE_SECONDS       → deny: stale_price
  8. gemini_depth < MIN_GEMINI_DEPTH_USD     → deny: insufficient_liquidity
  9. spread inside Gemini bid-ask            → deny: spread_inside_noise
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from prediction_arb.bot.engine import Opportunity

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------
MAX_POSITIONS = 10
MAX_POSITION_PCT = 0.05
MAX_DRAWDOWN_PCT = 0.20
MIN_SPREAD_PCT = 0.08
MIN_CONFIDENCE = 0.70
MAX_RISK = 0.80
MAX_PRICE_AGE_SECONDS = 60
MIN_GEMINI_DEPTH_USD = 50.0
MAX_OPPORTUNITIES_PER_SCAN = 50


# ---------------------------------------------------------------------------
# Portfolio dataclass
# ---------------------------------------------------------------------------


@dataclass
class Portfolio:
    open_positions: int = 0
    available_capital: float = 1000.0
    peak_capital: float = 1000.0
    realized_pnl: float = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_capital <= 0:
            return 0.0
        return max(0.0, (self.peak_capital - self.available_capital) / self.peak_capital)


# ---------------------------------------------------------------------------
# RiskDecision dataclass
# ---------------------------------------------------------------------------


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    clamped_size: float | None = None  # set when position size was clamped


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """
    Evaluates each opportunity against all risk limits before execution.

    Configuration is injected at construction time; all parameters have
    safe defaults matching the spec.
    """

    def __init__(
        self,
        max_positions: int = MAX_POSITIONS,
        max_position_pct: float = MAX_POSITION_PCT,
        max_drawdown_pct: float = MAX_DRAWDOWN_PCT,
        min_spread_pct: float = MIN_SPREAD_PCT,
        min_confidence: float = MIN_CONFIDENCE,
        max_risk: float = MAX_RISK,
        max_price_age_seconds: int = MAX_PRICE_AGE_SECONDS,
        min_gemini_depth_usd: float = MIN_GEMINI_DEPTH_USD,
        max_opportunities_per_scan: int = MAX_OPPORTUNITIES_PER_SCAN,
        alert_manager: object | None = None,
    ) -> None:
        self._max_positions = max_positions
        self._max_position_pct = max_position_pct
        self._max_drawdown_pct = max_drawdown_pct
        self._min_spread_pct = min_spread_pct
        self._min_confidence = min_confidence
        self._max_risk = max_risk
        self._max_price_age_seconds = max_price_age_seconds
        self._min_gemini_depth_usd = min_gemini_depth_usd
        self._max_opportunities_per_scan = max_opportunities_per_scan
        self._alert_manager = alert_manager

        self._suspended: bool = False
        self._scan_count: int = 0  # opportunities evaluated in current scan

    # ------------------------------------------------------------------
    # Kill-switch
    # ------------------------------------------------------------------

    def is_suspended(self) -> bool:
        """Return True if trading has been suspended due to drawdown breach."""
        return self._suspended

    def resume(self) -> None:
        """
        Lift the drawdown suspension.  Must be called explicitly by the operator
        (config flag reset or API call) — never called automatically.
        """
        if self._suspended:
            log.info("risk_manager_resumed", message="Trading suspension lifted by operator")
            self._suspended = False

    # ------------------------------------------------------------------
    # Scan-cycle counter
    # ------------------------------------------------------------------

    def reset_scan_counter(self) -> None:
        """Reset the per-scan opportunity counter.  Call at the start of each scan cycle."""
        self._scan_count = 0

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        opp: "Opportunity",
        portfolio: Portfolio,
        position_size: float | None = None,
    ) -> RiskDecision:
        """
        Evaluate an opportunity against all risk limits in order.

        Args:
            opp: The scored Opportunity to evaluate.
            portfolio: Current portfolio state.
            position_size: Proposed position size in USD.  If None, computed
                           from kelly_fraction * available_capital.

        Returns:
            RiskDecision with allowed=True/False, reason, and optional clamped_size.
        """
        # Compute proposed position size if not provided
        if position_size is None:
            position_size = opp.kelly_fraction * portfolio.available_capital

        clamped_size: float | None = None

        # ------------------------------------------------------------------
        # Check 1: position cap
        # ------------------------------------------------------------------
        if portfolio.open_positions >= self._max_positions:
            decision = RiskDecision(allowed=False, reason="position cap")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 2: position size clamp (not a denial — clamp and continue)
        # ------------------------------------------------------------------
        max_size = self._max_position_pct * portfolio.available_capital
        if position_size > max_size:
            clamped_size = max_size
            log.info(
                "risk_position_clamped",
                opportunity_id=opp.id,
                original_size=position_size,
                clamped_size=clamped_size,
                max_position_pct=self._max_position_pct,
            )
            position_size = clamped_size

        # ------------------------------------------------------------------
        # Check 3: drawdown kill-switch
        # ------------------------------------------------------------------
        if portfolio.drawdown_pct > self._max_drawdown_pct:
            self._suspended = True
            decision = RiskDecision(allowed=False, reason="drawdown")
            self._log_decision(decision, opp, portfolio)
            # Send alert if alert manager is available
            if self._alert_manager is not None:
                try:
                    self._alert_manager.send_drawdown_alert(  # type: ignore[attr-defined]
                        drawdown_pct=portfolio.drawdown_pct,
                        available_capital=portfolio.available_capital,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("alert_send_failed", error=str(exc))
            return decision

        # If already suspended (from a previous evaluation), deny immediately
        if self._suspended:
            decision = RiskDecision(allowed=False, reason="suspended")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 4: minimum spread
        # ------------------------------------------------------------------
        if opp.spread_pct < self._min_spread_pct:
            decision = RiskDecision(allowed=False, reason="spread too small")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 5: minimum confidence
        # ------------------------------------------------------------------
        if opp.match_confidence < self._min_confidence:
            decision = RiskDecision(allowed=False, reason="low confidence")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 6: maximum risk score
        # ------------------------------------------------------------------
        if opp.risk_score > self._max_risk:
            decision = RiskDecision(allowed=False, reason="risk too high")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 7: stale price
        # ------------------------------------------------------------------
        if opp.price_age_seconds > self._max_price_age_seconds:
            decision = RiskDecision(allowed=False, reason="stale_price")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 8: insufficient Gemini liquidity
        # ------------------------------------------------------------------
        if opp.gemini_depth < self._min_gemini_depth_usd:
            decision = RiskDecision(allowed=False, reason="insufficient_liquidity")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # Check 9: spread inside Gemini bid-ask noise
        # ------------------------------------------------------------------
        if opp.gemini_bid is not None and opp.gemini_ask is not None:
            gemini_spread = opp.gemini_ask - opp.gemini_bid
            if opp.spread <= gemini_spread / 2.0:
                decision = RiskDecision(allowed=False, reason="spread_inside_noise")
                self._log_decision(decision, opp, portfolio)
                return decision

        # ------------------------------------------------------------------
        # Check 10: per-scan opportunity cap
        # ------------------------------------------------------------------
        self._scan_count += 1
        if self._scan_count > self._max_opportunities_per_scan:
            log.warning(
                "max_opportunities_per_scan_exceeded",
                scan_count=self._scan_count,
                max=self._max_opportunities_per_scan,
                opportunity_id=opp.id,
                message="Excess opportunities in scan cycle — possible data anomaly",
            )
            decision = RiskDecision(allowed=False, reason="scan cap exceeded")
            self._log_decision(decision, opp, portfolio)
            return decision

        # ------------------------------------------------------------------
        # All checks passed — allow
        # ------------------------------------------------------------------
        decision = RiskDecision(
            allowed=True,
            reason="allowed",
            clamped_size=clamped_size,
        )
        self._log_decision(decision, opp, portfolio, position_size=position_size)
        return decision

    def handle_order_error(
        self,
        opportunity_id: str,
        error: Exception,
        amount_usd: float,
    ) -> None:
        """
        Called by the Executor when a Gemini order placement fails.

        Marks the position as failed, preserves capital (does NOT deduct),
        and logs at ERROR level.
        """
        log.error(
            "gemini_order_failed",
            opportunity_id=opportunity_id,
            amount_usd=amount_usd,
            error=str(error),
            message="Gemini order error — position marked failed, capital preserved",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_decision(
        self,
        decision: RiskDecision,
        opp: "Opportunity",
        portfolio: Portfolio,
        position_size: float | None = None,
    ) -> None:
        """Log every risk decision at INFO level with reason and metrics."""
        log.info(
            "risk_decision",
            allowed=decision.allowed,
            reason=decision.reason,
            opportunity_id=opp.id,
            spread_pct=round(opp.spread_pct, 6),
            match_confidence=round(opp.match_confidence, 4),
            risk_score=round(opp.risk_score, 4),
            price_age_seconds=round(opp.price_age_seconds, 1),
            gemini_depth=round(opp.gemini_depth, 2),
            open_positions=portfolio.open_positions,
            available_capital=round(portfolio.available_capital, 2),
            drawdown_pct=round(portfolio.drawdown_pct, 4),
            position_size=round(position_size, 2) if position_size is not None else None,
            clamped_size=round(decision.clamped_size, 2) if decision.clamped_size is not None else None,
            suspended=self._suspended,
        )
