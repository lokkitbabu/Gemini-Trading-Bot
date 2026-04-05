"""
Executor — places orders on Gemini Predictions and manages position lifecycle.

In DRY_RUN mode, simulates fills without calling the Gemini API.
In live mode, calls GeminiClient.place_order() and handles errors gracefully.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from prediction_arb.bot.clients.gemini import GeminiClient
    from prediction_arb.bot.engine import Opportunity
    from prediction_arb.bot.orderbook_cache import OrderbookCache

log = structlog.get_logger(__name__)

# Default config values
CONVERGENCE_EXIT_DAYS = 7
STOP_LOSS_PCT = 0.15
MAX_PRICE_AGE_SECONDS = 60


# ---------------------------------------------------------------------------
# GeminiPosition dataclass
# ---------------------------------------------------------------------------


@dataclass
class GeminiPosition:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    opportunity_id: str = ""
    event_id: str = ""
    side: str = ""                  # "yes" | "no"
    quantity: int = 0
    entry_price: float = 0.0
    size_usd: float = 0.0
    exit_strategy: str = ""         # "target_convergence" | "hold_to_resolution"
    target_exit_price: float = 0.0
    stop_loss_price: float = 0.0
    status: str = "open"            # "open" | "filled" | "closed" | "failed"
    opened_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    closed_at: datetime | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    ref_price: float = 0.0          # reference price at entry
    days_to_resolution: int | None = None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """
    Places orders on Gemini Predictions and manages position lifecycle.

    Parameters
    ----------
    gemini_client:
        GeminiClient instance for order placement and orderbook queries.
    state_store:
        Duck-typed store with save_position(pos) and update_position(pos).
    sse_broadcaster:
        Duck-typed broadcaster with publish(event_type, data).
    orderbook_cache:
        OrderbookCache for fresh price lookups before placement.
    dry_run:
        If True, simulate fills without calling the Gemini API.
    max_price_age_seconds:
        Maximum age of orderbook data before aborting placement.
    convergence_exit_days:
        Days to resolution threshold for target_convergence strategy.
    stop_loss_pct:
        Fraction of entry price used to compute stop-loss level.
    alert_manager:
        Optional duck-typed alert manager with send_alert(message, level).
    """

    def __init__(
        self,
        gemini_client: "GeminiClient",
        state_store: Any,
        sse_broadcaster: Any,
        orderbook_cache: "OrderbookCache",
        dry_run: bool = True,
        max_price_age_seconds: int = MAX_PRICE_AGE_SECONDS,
        convergence_exit_days: int = CONVERGENCE_EXIT_DAYS,
        stop_loss_pct: float = STOP_LOSS_PCT,
        alert_manager: Any = None,
    ) -> None:
        self._gemini = gemini_client
        self._state = state_store
        self._sse = sse_broadcaster
        self._cache = orderbook_cache
        self._dry_run = dry_run
        self._max_price_age_seconds = max_price_age_seconds
        self._convergence_exit_days = convergence_exit_days
        self._stop_loss_pct = stop_loss_pct
        self._alert_manager = alert_manager

    async def execute(self, opp: "Opportunity", size_usd: float) -> GeminiPosition:
        """
        Execute an arbitrage opportunity by placing an order on Gemini.

        Steps:
        1. Re-check price freshness; abort if stale.
        2. Compute quantity = floor(size_usd / entry_price).
        3. Determine exit strategy and compute target/stop-loss prices.
        4. In dry-run: simulate fill. In live: call GeminiClient.place_order().
        5. Persist position to StateStore and broadcast SSE event.

        Returns the created GeminiPosition.
        """
        # ------------------------------------------------------------------
        # Step 1: Re-check price freshness immediately before placement
        # ------------------------------------------------------------------
        if opp.price_age_seconds > self._max_price_age_seconds:
            log.warning(
                "executor_aborted_stale_price",
                opportunity_id=opp.id,
                price_age_seconds=opp.price_age_seconds,
                max_price_age_seconds=self._max_price_age_seconds,
            )
            raise ValueError(
                f"Stale price: {opp.price_age_seconds:.1f}s > {self._max_price_age_seconds}s"
            )

        entry_price = opp.entry_price
        ref_price = opp.signal_yes_price
        side = "yes" if opp.direction == "buy_yes" else "no"

        # ------------------------------------------------------------------
        # Step 2: Compute quantity
        # ------------------------------------------------------------------
        if entry_price <= 0:
            raise ValueError(f"Invalid entry_price: {entry_price}")

        quantity = math.floor(size_usd / entry_price)
        if quantity <= 0:
            raise ValueError(
                f"Computed quantity {quantity} <= 0 for size_usd={size_usd}, entry_price={entry_price}"
            )

        # ------------------------------------------------------------------
        # Step 3: Exit strategy and price targets
        # ------------------------------------------------------------------
        days = opp.days_to_resolution
        if days is not None and days > self._convergence_exit_days:
            exit_strategy = "target_convergence"
        else:
            exit_strategy = "hold_to_resolution"

        # target_exit_price: 80% convergence toward ref_price
        target_exit_price = entry_price + (ref_price - entry_price) * 0.8

        # stop_loss_price: entry_price ± stop_loss_pct depending on side
        if side == "yes":
            stop_loss_price = entry_price * (1.0 - self._stop_loss_pct)
        else:
            stop_loss_price = entry_price * (1.0 + self._stop_loss_pct)

        # ------------------------------------------------------------------
        # Step 4: Build position record
        # ------------------------------------------------------------------
        pos = GeminiPosition(
            opportunity_id=opp.id,
            event_id=opp.gemini_event_id,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            size_usd=size_usd,
            exit_strategy=exit_strategy,
            target_exit_price=target_exit_price,
            stop_loss_price=stop_loss_price,
            status="open",
            ref_price=ref_price,
            days_to_resolution=days,
        )

        # ------------------------------------------------------------------
        # Step 5: Place order (dry-run or live)
        # ------------------------------------------------------------------
        if self._dry_run:
            # Simulate fill at current ask
            pos.status = "filled"
            log.info(
                "executor_dry_run_fill",
                position_id=pos.id,
                opportunity_id=opp.id,
                event_id=pos.event_id,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                size_usd=size_usd,
                exit_strategy=exit_strategy,
                target_exit_price=round(target_exit_price, 4),
                stop_loss_price=round(stop_loss_price, 4),
                message="DRY_RUN: simulated fill at current ask",
            )
        else:
            try:
                order = await self._gemini.place_order(
                    event_id=opp.gemini_event_id,
                    side=side,
                    qty=float(quantity),
                    price=entry_price,
                )
                pos.status = "filled"
                log.info(
                    "executor_order_placed",
                    position_id=pos.id,
                    order_id=order.order_id,
                    opportunity_id=opp.id,
                    event_id=pos.event_id,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    size_usd=size_usd,
                )
            except Exception as exc:  # noqa: BLE001
                pos.status = "failed"
                log.error(
                    "executor_order_failed",
                    position_id=pos.id,
                    opportunity_id=opp.id,
                    event_id=pos.event_id,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    error=str(exc),
                    message="Gemini order failed — position marked failed, capital preserved",
                )
                if self._alert_manager is not None:
                    try:
                        await self._alert_manager.send_alert(  # type: ignore[attr-defined]
                            message=f"Order placement failed for opportunity {opp.id}: {exc}",
                            level="error",
                        )
                    except Exception as alert_exc:  # noqa: BLE001
                        log.warning("alert_send_failed", error=str(alert_exc))

        # ------------------------------------------------------------------
        # Step 6: Persist and broadcast
        # ------------------------------------------------------------------
        await self._state.save_position(pos)
        await self._sse.publish(
            "position_opened",
            {
                "position_id": pos.id,
                "opportunity_id": pos.opportunity_id,
                "event_id": pos.event_id,
                "side": pos.side,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "size_usd": pos.size_usd,
                "exit_strategy": pos.exit_strategy,
                "status": pos.status,
                "opened_at": pos.opened_at.isoformat(),
            },
        )

        return pos

    async def close_position(self, pos: GeminiPosition, reason: str) -> None:
        """
        Close an open position by placing a limit sell at the current Gemini bid.

        Persists exit fields (closed_at, exit_price, realized_pnl, status)
        and broadcasts a position_closed SSE event.
        """
        # Fetch current orderbook to get the bid price
        exit_price: float | None = None
        try:
            ob = await self._gemini.get_orderbook(pos.event_id)
            if ob is not None and ob.best_bid is not None:
                exit_price = ob.best_bid
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "executor_close_orderbook_fetch_failed",
                position_id=pos.id,
                event_id=pos.event_id,
                error=str(exc),
            )

        if exit_price is None:
            log.warning(
                "executor_close_no_bid",
                position_id=pos.id,
                event_id=pos.event_id,
                reason=reason,
                message="No bid available; using entry_price as fallback exit price",
            )
            exit_price = pos.entry_price

        # Place limit sell (skip in dry-run)
        if not self._dry_run:
            try:
                await self._gemini.place_order(
                    event_id=pos.event_id,
                    side="sell",
                    qty=float(pos.quantity),
                    price=exit_price,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "executor_close_order_failed",
                    position_id=pos.id,
                    event_id=pos.event_id,
                    reason=reason,
                    error=str(exc),
                )
                if self._alert_manager is not None:
                    try:
                        await self._alert_manager.send_alert(  # type: ignore[attr-defined]
                            message=f"Close order failed for position {pos.id}: {exc}",
                            level="error",
                        )
                    except Exception as alert_exc:  # noqa: BLE001
                        log.warning("alert_send_failed", error=str(alert_exc))
        else:
            log.info(
                "executor_dry_run_close",
                position_id=pos.id,
                event_id=pos.event_id,
                exit_price=exit_price,
                reason=reason,
                message="DRY_RUN: simulated close at current bid",
            )

        # Compute realized P&L
        if pos.side == "yes":
            realized_pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            # NO position: profit when exit_price < entry_price
            realized_pnl = (pos.entry_price - exit_price) * pos.quantity

        # Update position fields
        pos.status = "closed"
        pos.closed_at = datetime.now(tz=timezone.utc)
        pos.exit_price = exit_price
        pos.realized_pnl = realized_pnl

        log.info(
            "executor_position_closed",
            position_id=pos.id,
            event_id=pos.event_id,
            reason=reason,
            exit_price=exit_price,
            realized_pnl=round(realized_pnl, 4),
            side=pos.side,
            quantity=pos.quantity,
        )

        await self._state.update_position(pos)
        await self._sse.publish(
            "position_closed",
            {
                "position_id": pos.id,
                "event_id": pos.event_id,
                "reason": reason,
                "exit_price": exit_price,
                "realized_pnl": realized_pnl,
                "closed_at": pos.closed_at.isoformat(),
                "status": pos.status,
            },
        )
