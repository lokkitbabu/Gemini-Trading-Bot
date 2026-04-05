"""
SQLAlchemy 2 ORM models for the prediction arbitrage bot.

Tables:
  - opportunities        — maps to Opportunity dataclass (engine.py)
  - gemini_positions     — maps to GeminiPosition dataclass (executor.py)
  - pnl_snapshots        — periodic P&L snapshots
  - match_cache          — LLM match result cache
  - orderbook_snapshots  — per-(platform, ticker) orderbook snapshots

Also defines PORTFOLIO_SUMMARY_VIEW_SQL used by /api/v1/portfolio.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Portfolio summary view SQL (used by /api/v1/portfolio)
# ---------------------------------------------------------------------------

PORTFOLIO_SUMMARY_VIEW_SQL = """\
CREATE OR REPLACE VIEW portfolio_summary AS
SELECT
  COUNT(*) FILTER (WHERE status IN ('open','filled')) AS open_positions,
  SUM(size_usd) FILTER (WHERE status IN ('open','filled')) AS deployed_capital,
  SUM(realized_pnl) FILTER (WHERE status = 'closed') AS realized_pnl,
  COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl > 0) AS winning_trades,
  COUNT(*) FILTER (WHERE status = 'closed') AS total_closed_trades
FROM gemini_positions;"""


# ---------------------------------------------------------------------------
# opportunities
# ---------------------------------------------------------------------------


class OpportunityModel(Base):
    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_title: Mapped[str] = mapped_column(Text, nullable=False)
    asset: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    price_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    resolution_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signal_platform: Mapped[str] = mapped_column(String, nullable=False)
    signal_event_id: Mapped[str] = mapped_column(String, nullable=False)
    signal_yes_price: Mapped[float] = mapped_column(Float, nullable=False)
    signal_volume: Mapped[float] = mapped_column(Float, nullable=False)
    gemini_event_id: Mapped[str] = mapped_column(String, nullable=False)
    gemini_yes_price: Mapped[float] = mapped_column(Float, nullable=False)
    gemini_volume: Mapped[float] = mapped_column(Float, nullable=False)
    gemini_bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gemini_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gemini_depth: Mapped[float] = mapped_column(Float, nullable=False)
    spread: Mapped[float] = mapped_column(Float, nullable=False)
    spread_pct: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    kelly_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    match_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    days_to_resolution: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    signal_disagreement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inverted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    price_age_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


# ---------------------------------------------------------------------------
# gemini_positions
# ---------------------------------------------------------------------------


class GeminiPositionModel(Base):
    __tablename__ = "gemini_positions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    opportunity_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usd: Mapped[float] = mapped_column(Float, nullable=False)
    exit_strategy: Mapped[str] = mapped_column(String, nullable=False)
    target_exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ref_price: Mapped[float] = mapped_column(Float, nullable=False)
    days_to_resolution: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Auto-update updated_at on INSERT and UPDATE via SQLAlchemy ORM event
@event.listens_for(GeminiPositionModel, "before_insert")
def _set_updated_at_on_insert(mapper, connection, target: GeminiPositionModel) -> None:  # noqa: ARG001
    from datetime import timezone

    target.updated_at = datetime.now(tz=timezone.utc)


@event.listens_for(GeminiPositionModel, "before_update")
def _set_updated_at_on_update(mapper, connection, target: GeminiPositionModel) -> None:  # noqa: ARG001
    from datetime import timezone

    target.updated_at = datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# pnl_snapshots
# ---------------------------------------------------------------------------


class PnlSnapshotModel(Base):
    __tablename__ = "pnl_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    available_capital: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    peak_capital: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)


# ---------------------------------------------------------------------------
# match_cache
# ---------------------------------------------------------------------------


class MatchCacheModel(Base):
    __tablename__ = "match_cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)  # SHA-256 hex
    equivalent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    asset: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    price_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolution_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    inverted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    backend: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_match_cache_expires_at", "expires_at"),)


# ---------------------------------------------------------------------------
# orderbook_snapshots
# ---------------------------------------------------------------------------


class OrderbookSnapshotModel(Base):
    __tablename__ = "orderbook_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    platform: Mapped[str] = mapped_column(String, nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    best_bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yes_mid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    depth_5pct: Mapped[float] = mapped_column(Float, nullable=False)
    depth_3pct_usd: Mapped[float] = mapped_column(Float, nullable=False)
    volume_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_orderbook_snapshots_platform_ticker_fetched", "platform", "ticker", "fetched_at"),
    )
