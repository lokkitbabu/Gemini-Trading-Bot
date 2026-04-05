"""Initial schema: all tables, indexes, and portfolio_summary view.

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

PORTFOLIO_SUMMARY_VIEW_SQL = """\
CREATE OR REPLACE VIEW portfolio_summary AS
SELECT
  COUNT(*) FILTER (WHERE status IN ('open','filled')) AS open_positions,
  SUM(size_usd) FILTER (WHERE status IN ('open','filled')) AS deployed_capital,
  SUM(realized_pnl) FILTER (WHERE status = 'closed') AS realized_pnl,
  COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl > 0) AS winning_trades,
  COUNT(*) FILTER (WHERE status = 'closed') AS total_closed_trades
FROM gemini_positions;"""

DROP_PORTFOLIO_SUMMARY_VIEW_SQL = "DROP VIEW IF EXISTS portfolio_summary;"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # opportunities
    # ------------------------------------------------------------------
    op.create_table(
        "opportunities",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_title", sa.Text(), nullable=False),
        sa.Column("asset", sa.String(), nullable=True),
        sa.Column("price_level", sa.Float(), nullable=True),
        sa.Column("resolution_date", sa.String(), nullable=True),
        sa.Column("signal_platform", sa.String(), nullable=False),
        sa.Column("signal_event_id", sa.String(), nullable=False),
        sa.Column("signal_yes_price", sa.Float(), nullable=False),
        sa.Column("signal_volume", sa.Float(), nullable=False),
        sa.Column("gemini_event_id", sa.String(), nullable=False),
        sa.Column("gemini_yes_price", sa.Float(), nullable=False),
        sa.Column("gemini_volume", sa.Float(), nullable=False),
        sa.Column("gemini_bid", sa.Float(), nullable=True),
        sa.Column("gemini_ask", sa.Float(), nullable=True),
        sa.Column("gemini_depth", sa.Float(), nullable=False),
        sa.Column("spread", sa.Float(), nullable=False),
        sa.Column("spread_pct", sa.Float(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("kelly_fraction", sa.Float(), nullable=False),
        sa.Column("match_confidence", sa.Float(), nullable=False),
        sa.Column("days_to_resolution", sa.Integer(), nullable=True),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("signal_disagreement", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("inverted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("price_age_seconds", sa.Float(), nullable=False, server_default="0.0"),
    )

    # ------------------------------------------------------------------
    # gemini_positions
    # ------------------------------------------------------------------
    op.create_table(
        "gemini_positions",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("opportunity_id", sa.String(), nullable=True),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("size_usd", sa.Float(), nullable=False),
        sa.Column("exit_strategy", sa.String(), nullable=False),
        sa.Column("target_exit_price", sa.Float(), nullable=False),
        sa.Column("stop_loss_price", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("ref_price", sa.Float(), nullable=False),
        sa.Column("days_to_resolution", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunities.id"],
            name="fk_gemini_positions_opportunity_id",
            ondelete="SET NULL",
        ),
    )

    # ------------------------------------------------------------------
    # pnl_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "pnl_snapshots",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("available_capital", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.Column("peak_capital", sa.Float(), nullable=False),
        sa.Column("drawdown_pct", sa.Float(), nullable=False),
    )

    # ------------------------------------------------------------------
    # match_cache
    # ------------------------------------------------------------------
    op.create_table(
        "match_cache",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("equivalent", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("asset", sa.String(), nullable=True),
        sa.Column("price_level", sa.Float(), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("resolution_date", sa.String(), nullable=True),
        sa.Column("inverted", sa.Boolean(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_match_cache_expires_at", "match_cache", ["expires_at"])

    # ------------------------------------------------------------------
    # orderbook_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "orderbook_snapshots",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("best_bid", sa.Float(), nullable=True),
        sa.Column("best_ask", sa.Float(), nullable=True),
        sa.Column("yes_mid", sa.Float(), nullable=True),
        sa.Column("depth_5pct", sa.Float(), nullable=False),
        sa.Column("depth_3pct_usd", sa.Float(), nullable=False),
        sa.Column("volume_24h", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_orderbook_snapshots_platform_ticker_fetched",
        "orderbook_snapshots",
        ["platform", "ticker", "fetched_at"],
    )

    # ------------------------------------------------------------------
    # portfolio_summary view
    # ------------------------------------------------------------------
    op.execute(PORTFOLIO_SUMMARY_VIEW_SQL)


def downgrade() -> None:
    op.execute(DROP_PORTFOLIO_SUMMARY_VIEW_SQL)
    op.drop_index("ix_orderbook_snapshots_platform_ticker_fetched", table_name="orderbook_snapshots")
    op.drop_table("orderbook_snapshots")
    op.drop_index("ix_match_cache_expires_at", table_name="match_cache")
    op.drop_table("match_cache")
    op.drop_table("pnl_snapshots")
    op.drop_table("gemini_positions")
    op.drop_table("opportunities")
