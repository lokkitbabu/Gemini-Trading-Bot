"""Ensure match_cache expires_at index exists (idempotent).

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-01 00:01:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_match_cache_expires_at"


def upgrade() -> None:
    # Idempotent: only create the index if it doesn't already exist.
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = [idx["name"] for idx in inspector.get_indexes("match_cache")]
    if INDEX_NAME not in existing:
        op.create_index(INDEX_NAME, "match_cache", ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = [idx["name"] for idx in inspector.get_indexes("match_cache")]
    if INDEX_NAME in existing:
        op.drop_index(INDEX_NAME, table_name="match_cache")
