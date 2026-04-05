"""
StateStore — async SQLAlchemy 2 persistence layer.

Converts between domain dataclasses (Opportunity, GeminiPosition,
OrderbookSnapshot) and ORM models internally. Callers never touch ORM
models directly.

Write-failure retry: up to 3 attempts with 0.5s / 1s / 2s backoff.
Logs CRITICAL on final failure and re-raises so the caller can decide
whether to continue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from prediction_arb.bot.models import (
    GeminiPositionModel,
    MatchCacheModel,
    OpportunityModel,
    OrderbookSnapshotModel,
    PnlSnapshotModel,
)

if TYPE_CHECKING:
    from prediction_arb.bot.engine import Opportunity
    from prediction_arb.bot.executor import GeminiPosition
    from prediction_arb.bot.orderbook_cache import OrderbookSnapshot

log = structlog.get_logger(__name__)

_RETRY_DELAYS = (0.5, 1.0, 2.0)


# ---------------------------------------------------------------------------
# AggregateStats
# ---------------------------------------------------------------------------


@dataclass
class AggregateStats:
    total_pnl: float
    win_rate: float
    avg_spread: float
    exit_reason_breakdown: dict[str, int]
    trade_count: int


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _retry_write(coro_factory, *, label: str) -> None:
    """
    Execute an async write operation with up to 3 retries.

    coro_factory is a zero-argument callable that returns a coroutine.
    Delays between attempts: 0.5s, 1s, 2s.
    Logs CRITICAL on final failure and re-raises.
    """
    last_exc: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        try:
            await coro_factory()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                log.warning(
                    "state_write_retry",
                    label=label,
                    attempt=attempt,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            else:
                log.critical(
                    "state_write_failed",
                    label=label,
                    attempts=attempt,
                    error=str(exc),
                )
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Conversion helpers: domain dataclass → ORM model
# ---------------------------------------------------------------------------


def _opp_to_model(opp: "Opportunity") -> OpportunityModel:
    return OpportunityModel(
        id=opp.id,
        detected_at=opp.detected_at,
        event_title=opp.event_title,
        asset=opp.asset,
        price_level=opp.price_level,
        resolution_date=opp.resolution_date,
        signal_platform=opp.signal_platform,
        signal_event_id=opp.signal_event_id,
        signal_yes_price=opp.signal_yes_price,
        signal_volume=opp.signal_volume,
        gemini_event_id=opp.gemini_event_id,
        gemini_yes_price=opp.gemini_yes_price,
        gemini_volume=opp.gemini_volume,
        gemini_bid=opp.gemini_bid,
        gemini_ask=opp.gemini_ask,
        gemini_depth=opp.gemini_depth,
        spread=opp.spread,
        spread_pct=opp.spread_pct,
        direction=opp.direction,
        entry_price=opp.entry_price,
        kelly_fraction=opp.kelly_fraction,
        match_confidence=opp.match_confidence,
        days_to_resolution=opp.days_to_resolution,
        risk_score=opp.risk_score,
        status=opp.status,
        signal_disagreement=opp.signal_disagreement,
        inverted=opp.inverted,
        price_age_seconds=opp.price_age_seconds,
    )


def _pos_to_model(pos: "GeminiPosition") -> GeminiPositionModel:
    return GeminiPositionModel(
        id=pos.id,
        opportunity_id=pos.opportunity_id or None,
        event_id=pos.event_id,
        side=pos.side,
        quantity=pos.quantity,
        entry_price=pos.entry_price,
        size_usd=pos.size_usd,
        exit_strategy=pos.exit_strategy,
        target_exit_price=pos.target_exit_price,
        stop_loss_price=pos.stop_loss_price,
        status=pos.status,
        opened_at=pos.opened_at,
        closed_at=pos.closed_at,
        exit_price=pos.exit_price,
        realized_pnl=pos.realized_pnl,
        ref_price=pos.ref_price,
        days_to_resolution=pos.days_to_resolution,
        updated_at=datetime.now(tz=timezone.utc),
    )


def _model_to_pos(row: GeminiPositionModel) -> "GeminiPosition":
    """Convert ORM row back to GeminiPosition dataclass."""
    from prediction_arb.bot.executor import GeminiPosition

    return GeminiPosition(
        id=row.id,
        opportunity_id=row.opportunity_id or "",
        event_id=row.event_id,
        side=row.side,
        quantity=row.quantity,
        entry_price=row.entry_price,
        size_usd=row.size_usd,
        exit_strategy=row.exit_strategy,
        target_exit_price=row.target_exit_price,
        stop_loss_price=row.stop_loss_price,
        status=row.status,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        exit_price=row.exit_price,
        realized_pnl=row.realized_pnl,
        ref_price=row.ref_price,
        days_to_resolution=row.days_to_resolution,
    )


def _ob_to_model(snapshot: "OrderbookSnapshot") -> OrderbookSnapshotModel:
    return OrderbookSnapshotModel(
        platform=snapshot.platform,
        ticker=snapshot.ticker,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        yes_mid=snapshot.yes_mid,
        depth_5pct=snapshot.depth_5pct,
        depth_3pct_usd=snapshot.depth_3pct_usd,
        volume_24h=snapshot.volume_24h,
        fetched_at=snapshot.fetched_at,
    )


def _model_to_ob(row: OrderbookSnapshotModel) -> "OrderbookSnapshot":
    from prediction_arb.bot.orderbook_cache import OrderbookSnapshot

    return OrderbookSnapshot(
        platform=row.platform,
        ticker=row.ticker,
        best_bid=row.best_bid,
        best_ask=row.best_ask,
        yes_mid=row.yes_mid,
        depth_5pct=row.depth_5pct,
        depth_3pct_usd=row.depth_3pct_usd,
        volume_24h=row.volume_24h,
        fetched_at=row.fetched_at,
    )


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


class StateStore:
    """
    Async SQLAlchemy 2 persistence layer.

    All write operations are retried up to 3 times with exponential backoff.
    Domain dataclasses are converted to/from ORM models internally.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._engine: Any = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def init(self) -> None:
        """Create async engine and session factory."""
        self._engine = create_async_engine(
            self._database_url,
            echo=False,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        log.info("state_store_initialized", database_url=self._database_url.split("@")[-1])

    async def close(self) -> None:
        """Dispose the engine and release all connections."""
        if self._engine is not None:
            await self._engine.dispose()
            log.info("state_store_closed")

    def _session(self) -> AsyncSession:
        if self._session_factory is None:
            raise RuntimeError("StateStore.init() must be called before use")
        return self._session_factory()

    # ------------------------------------------------------------------
    # Opportunity
    # ------------------------------------------------------------------

    async def save_opportunity(self, opp: "Opportunity") -> None:
        model = _opp_to_model(opp)

        async def _write() -> None:
            async with self._session() as session:
                session.add(model)
                await session.commit()

        await _retry_write(_write, label=f"save_opportunity:{opp.id}")

    async def get_opportunity(self, id: str) -> "Optional[Opportunity]":
        async with self._session() as session:
            row = await session.get(OpportunityModel, id)
        if row is None:
            return None
        from prediction_arb.bot.engine import Opportunity

        return Opportunity(
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

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    async def save_position(self, pos: "GeminiPosition") -> None:
        model = _pos_to_model(pos)

        async def _write() -> None:
            async with self._session() as session:
                session.add(model)
                await session.commit()

        await _retry_write(_write, label=f"save_position:{pos.id}")

    async def update_position(self, pos: "GeminiPosition") -> None:
        async def _write() -> None:
            async with self._session() as session:
                row = await session.get(GeminiPositionModel, pos.id)
                if row is None:
                    # Insert if not found (upsert semantics)
                    session.add(_pos_to_model(pos))
                else:
                    row.opportunity_id = pos.opportunity_id or None
                    row.event_id = pos.event_id
                    row.side = pos.side
                    row.quantity = pos.quantity
                    row.entry_price = pos.entry_price
                    row.size_usd = pos.size_usd
                    row.exit_strategy = pos.exit_strategy
                    row.target_exit_price = pos.target_exit_price
                    row.stop_loss_price = pos.stop_loss_price
                    row.status = pos.status
                    row.opened_at = pos.opened_at
                    row.closed_at = pos.closed_at
                    row.exit_price = pos.exit_price
                    row.realized_pnl = pos.realized_pnl
                    row.ref_price = pos.ref_price
                    row.days_to_resolution = pos.days_to_resolution
                    row.updated_at = datetime.now(tz=timezone.utc)
                await session.commit()

        await _retry_write(_write, label=f"update_position:{pos.id}")

    async def get_open_positions(self) -> list["GeminiPosition"]:
        """Return all positions with status in ('open', 'filled')."""
        async with self._session() as session:
            result = await session.execute(
                select(GeminiPositionModel).where(
                    GeminiPositionModel.status.in_(("open", "filled"))
                )
            )
            rows = result.scalars().all()
        return [_model_to_pos(r) for r in rows]

    # ------------------------------------------------------------------
    # P&L
    # ------------------------------------------------------------------

    async def get_pnl_history(
        self, from_ts: datetime, to_ts: datetime
    ) -> list[dict]:
        """Return pnl_snapshots rows between from_ts and to_ts as dicts."""
        async with self._session() as session:
            result = await session.execute(
                select(PnlSnapshotModel).where(
                    PnlSnapshotModel.snapshot_at >= from_ts,
                    PnlSnapshotModel.snapshot_at <= to_ts,
                ).order_by(PnlSnapshotModel.snapshot_at)
            )
            rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "snapshot_at": r.snapshot_at,
                "open_positions": r.open_positions,
                "available_capital": r.available_capital,
                "realized_pnl": r.realized_pnl,
                "peak_capital": r.peak_capital,
                "drawdown_pct": r.drawdown_pct,
            }
            for r in rows
        ]

    async def get_aggregate_stats(self, window: Any = None) -> AggregateStats:
        """
        Compute aggregate stats over closed positions (and opportunities for avg_spread).

        window: optional timedelta; if provided, only consider records within the window.
        """
        async with self._session() as session:
            # Build base query for closed positions
            pos_query = select(GeminiPositionModel).where(
                GeminiPositionModel.status == "closed"
            )
            if window is not None:
                cutoff = datetime.now(tz=timezone.utc) - window
                pos_query = pos_query.where(GeminiPositionModel.opened_at >= cutoff)

            result = await session.execute(pos_query)
            closed_positions = result.scalars().all()

            # avg_spread from opportunities
            opp_query = select(OpportunityModel.spread_pct)
            if window is not None:
                cutoff = datetime.now(tz=timezone.utc) - window
                opp_query = opp_query.where(OpportunityModel.detected_at >= cutoff)
            opp_result = await session.execute(opp_query)
            spread_pcts = [row[0] for row in opp_result.all()]

        total_pnl = sum(
            p.realized_pnl for p in closed_positions if p.realized_pnl is not None
        )
        trade_count = len(closed_positions)
        winning = sum(
            1 for p in closed_positions if p.realized_pnl is not None and p.realized_pnl > 0
        )
        win_rate = winning / trade_count if trade_count > 0 else 0.0
        avg_spread = sum(spread_pcts) / len(spread_pcts) if spread_pcts else 0.0

        return AggregateStats(
            total_pnl=total_pnl,
            win_rate=win_rate,
            avg_spread=avg_spread,
            exit_reason_breakdown={},  # not stored yet
            trade_count=trade_count,
        )

    # ------------------------------------------------------------------
    # Orderbook snapshots
    # ------------------------------------------------------------------

    async def save_orderbook_snapshot(self, snapshot: "OrderbookSnapshot") -> None:
        model = _ob_to_model(snapshot)

        async def _write() -> None:
            async with self._session() as session:
                session.add(model)
                await session.commit()

        await _retry_write(
            _write, label=f"save_orderbook_snapshot:{snapshot.platform}:{snapshot.ticker}"
        )

    async def get_orderbook_snapshot(
        self, platform: str, ticker: str
    ) -> "Optional[OrderbookSnapshot]":
        """Return the most recent snapshot for (platform, ticker)."""
        async with self._session() as session:
            result = await session.execute(
                select(OrderbookSnapshotModel)
                .where(
                    OrderbookSnapshotModel.platform == platform,
                    OrderbookSnapshotModel.ticker == ticker,
                )
                .order_by(OrderbookSnapshotModel.fetched_at.desc())
                .limit(1)
            )
            row = result.scalars().first()
        if row is None:
            return None
        return _model_to_ob(row)

    # ------------------------------------------------------------------
    # Match cache
    # ------------------------------------------------------------------

    async def load_match_cache(self) -> list[dict]:
        """Return all non-expired match cache entries as dicts."""
        now = datetime.now(tz=timezone.utc)
        async with self._session() as session:
            result = await session.execute(
                select(MatchCacheModel).where(MatchCacheModel.expires_at > now)
            )
            rows = result.scalars().all()
        return [
            {
                "key": r.key,
                "equivalent": r.equivalent,
                "confidence": r.confidence,
                "reasoning": r.reasoning,
                "asset": r.asset,
                "price_level": r.price_level,
                "direction": r.direction,
                "resolution_date": r.resolution_date,
                "inverted": r.inverted,
                "backend": r.backend,
                "expires_at": r.expires_at,
            }
            for r in rows
        ]

    async def save_match_cache_entry(
        self, key: str, result: dict, expires_at: datetime
    ) -> None:
        """Upsert a match cache entry."""
        async def _write() -> None:
            async with self._session() as session:
                row = await session.get(MatchCacheModel, key)
                now = datetime.now(tz=timezone.utc)
                if row is None:
                    row = MatchCacheModel(
                        key=key,
                        equivalent=result.get("equivalent", False),
                        confidence=result.get("confidence", 0.0),
                        reasoning=result.get("reasoning", ""),
                        asset=result.get("asset"),
                        price_level=result.get("price_level"),
                        direction=result.get("direction"),
                        resolution_date=result.get("resolution_date"),
                        inverted=result.get("inverted", False),
                        backend=result.get("backend", ""),
                        created_at=now,
                        expires_at=expires_at,
                    )
                    session.add(row)
                else:
                    row.equivalent = result.get("equivalent", row.equivalent)
                    row.confidence = result.get("confidence", row.confidence)
                    row.reasoning = result.get("reasoning", row.reasoning)
                    row.asset = result.get("asset", row.asset)
                    row.price_level = result.get("price_level", row.price_level)
                    row.direction = result.get("direction", row.direction)
                    row.resolution_date = result.get("resolution_date", row.resolution_date)
                    row.inverted = result.get("inverted", row.inverted)
                    row.backend = result.get("backend", row.backend)
                    row.expires_at = expires_at
                await session.commit()

        await _retry_write(_write, label=f"save_match_cache_entry:{key}")

    async def prune_expired_match_cache(self) -> int:
        """Delete expired match cache entries. Returns number of rows deleted."""
        from sqlalchemy import delete

        now = datetime.now(tz=timezone.utc)
        deleted_count = 0

        async def _write() -> None:
            nonlocal deleted_count
            async with self._session() as session:
                result = await session.execute(
                    delete(MatchCacheModel).where(MatchCacheModel.expires_at <= now)
                )
                deleted_count = result.rowcount
                await session.commit()

        await _retry_write(_write, label="prune_expired_match_cache")
        log.info("match_cache_pruned", deleted=deleted_count)
        return deleted_count
