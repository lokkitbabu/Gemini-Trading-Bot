"""
PositionMonitor — monitors open positions for stop-loss and convergence exit conditions.

Runs as an asyncio task every MONITOR_INTERVAL_SECONDS (default 60s).
For each open position it fetches the current Gemini orderbook and checks:
  1. Stop-loss: gemini_mid has moved STOP_LOSS_PCT against entry price
  2. Convergence exit: |gemini_mid - ref_price| < CONVERGENCE_THRESHOLD and bid > entry_price
  3. Near-resolution (< 4h to expiry): disables early exit, holds to resolution only
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from prediction_arb.bot.clients.gemini import GeminiClient
    from prediction_arb.bot.executor import Executor, GeminiPosition

log = structlog.get_logger(__name__)

# Near-resolution threshold: 4 hours
_NEAR_RESOLUTION_HOURS = 4


class PositionMonitor:
    """
    Monitors open positions and triggers exits when conditions are met.

    Parameters
    ----------
    executor:
        Executor instance used to close positions.
    gemini_client:
        GeminiClient for fetching current orderbooks.
    state_store:
        Duck-typed store with get_open_positions() -> list[GeminiPosition].
    convergence_threshold:
        Maximum |gemini_mid - ref_price| to trigger convergence exit (default 2¢).
    stop_loss_pct:
        Fraction of entry price defining the stop-loss level (default 15%).
    monitor_interval_seconds:
        Seconds between run_once() calls when running as a background task.
    """

    def __init__(
        self,
        executor: "Executor",
        gemini_client: "GeminiClient",
        state_store: Any,
        convergence_threshold: float = 0.02,
        stop_loss_pct: float = 0.15,
        monitor_interval_seconds: int = 60,
    ) -> None:
        self._executor = executor
        self._gemini = gemini_client
        self._state = state_store
        self._convergence_threshold = convergence_threshold
        self._stop_loss_pct = stop_loss_pct
        self._monitor_interval_seconds = monitor_interval_seconds
        self._running = False

    async def run_once(self) -> None:
        """
        Check all open positions once for stop-loss and convergence exit conditions.

        For each position:
        - Fetch current Gemini orderbook; skip with WARNING if unavailable.
        - Handle near-resolution events (< 4h to expiry): hold to resolution only.
        - Check stop-loss trigger.
        - Check convergence exit trigger.
        """
        positions: list["GeminiPosition"] = await self._state.get_open_positions()

        if not positions:
            log.debug("monitor_no_open_positions")
            return

        log.info("monitor_run_once_start", open_positions=len(positions))

        for pos in positions:
            await self._check_position(pos)

    async def _check_position(self, pos: "GeminiPosition") -> None:
        """Evaluate a single position against exit conditions."""
        # Fetch current orderbook
        try:
            ob = await self._gemini.get_orderbook(pos.event_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "monitor_orderbook_fetch_failed",
                position_id=pos.id,
                event_id=pos.event_id,
                error=str(exc),
            )
            return

        if ob is None or (ob.best_bid is None and ob.best_ask is None):
            log.warning(
                "monitor_orderbook_unavailable",
                position_id=pos.id,
                event_id=pos.event_id,
            )
            return

        # Compute gemini_mid
        if ob.best_bid is not None and ob.best_ask is not None:
            gemini_mid = (ob.best_bid + ob.best_ask) / 2.0
        elif ob.best_bid is not None:
            gemini_mid = ob.best_bid
        elif ob.best_ask is not None:
            gemini_mid = ob.best_ask
        else:
            log.warning(
                "monitor_no_mid_price",
                position_id=pos.id,
                event_id=pos.event_id,
            )
            return

        # ------------------------------------------------------------------
        # Near-resolution check: disable early exit if < 4h to expiry
        # ------------------------------------------------------------------
        if self._is_near_resolution(pos):
            log.info(
                "monitor_near_resolution_hold",
                position_id=pos.id,
                event_id=pos.event_id,
                days_to_resolution=pos.days_to_resolution,
                message="Near resolution — holding to resolution, early exit disabled",
            )
            # Override exit strategy to hold_to_resolution
            if pos.exit_strategy != "hold_to_resolution":
                pos.exit_strategy = "hold_to_resolution"
                await self._state.update_position(pos)
            return

        # ------------------------------------------------------------------
        # Stop-loss check
        # ------------------------------------------------------------------
        if self._stop_loss_triggered(pos, gemini_mid):
            log.info(
                "monitor_stop_loss_triggered",
                position_id=pos.id,
                event_id=pos.event_id,
                side=pos.side,
                entry_price=pos.entry_price,
                gemini_mid=gemini_mid,
                stop_loss_price=pos.stop_loss_price,
            )
            await self._executor.close_position(pos, reason="stop_loss")
            return

        # ------------------------------------------------------------------
        # Convergence exit check
        # ------------------------------------------------------------------
        if pos.exit_strategy == "target_convergence":
            if self._convergence_triggered(pos, gemini_mid, ob.best_bid):
                log.info(
                    "monitor_convergence_triggered",
                    position_id=pos.id,
                    event_id=pos.event_id,
                    gemini_mid=gemini_mid,
                    ref_price=pos.ref_price,
                    convergence_threshold=self._convergence_threshold,
                    gemini_bid=ob.best_bid,
                    entry_price=pos.entry_price,
                )
                await self._executor.close_position(pos, reason="convergence")

    def _stop_loss_triggered(self, pos: "GeminiPosition", gemini_mid: float) -> bool:
        """
        Return True if the stop-loss condition is met.

        YES positions: stop-loss when gemini_mid < entry_price * (1 - stop_loss_pct)
        NO positions:  stop-loss when gemini_mid > entry_price * (1 + stop_loss_pct)
          (NO ask = 1 - YES bid, so rising YES mid hurts NO positions)
        """
        if pos.side == "yes":
            return gemini_mid < pos.entry_price * (1.0 - self._stop_loss_pct)
        else:
            return gemini_mid > pos.entry_price * (1.0 + self._stop_loss_pct)

    def _convergence_triggered(
        self,
        pos: "GeminiPosition",
        gemini_mid: float,
        gemini_bid: float | None,
    ) -> bool:
        """
        Return True if convergence exit conditions are met:
        - |gemini_mid - ref_price| < convergence_threshold
        - gemini_bid > entry_price (can exit profitably)
        """
        price_gap = abs(gemini_mid - pos.ref_price)
        if price_gap >= self._convergence_threshold:
            return False
        if gemini_bid is None or gemini_bid <= pos.entry_price:
            return False
        return True

    def _is_near_resolution(self, pos: "GeminiPosition") -> bool:
        """
        Return True if the position's event resolves within 4 hours.

        Uses days_to_resolution as a proxy; if days_to_resolution == 0
        we treat it as near-resolution.
        """
        if pos.days_to_resolution is None:
            return False
        # days_to_resolution == 0 means resolves today (within ~24h)
        # We use a conservative check: < 1 day remaining maps to near-resolution
        # For sub-day precision, the caller should pass fractional days or a timestamp.
        # Here we treat 0 days as near-resolution (< 4h is a subset of same-day).
        return pos.days_to_resolution == 0

    # ------------------------------------------------------------------
    # Background task runner
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """
        Run run_once() in a loop every monitor_interval_seconds.

        Designed to be started as an asyncio.Task.
        Catches and logs all exceptions to prevent the loop from dying silently.
        """
        self._running = True
        log.info(
            "monitor_loop_started",
            interval_seconds=self._monitor_interval_seconds,
        )
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "monitor_run_once_error",
                    error=str(exc),
                    message="PositionMonitor run_once raised an unexpected error",
                )
            await asyncio.sleep(self._monitor_interval_seconds)

    def stop(self) -> None:
        """Signal the run_loop to stop after the current iteration."""
        self._running = False
