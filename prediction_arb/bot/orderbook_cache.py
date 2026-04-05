"""
OrderbookCache — in-memory cache of most recent OrderbookSnapshot per (platform, ticker).

Thread-safe (asyncio-safe) via asyncio.Lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prediction_arb.bot.matcher import MatchedPair


# ---------------------------------------------------------------------------
# OrderbookSnapshot dataclass
# ---------------------------------------------------------------------------


@dataclass
class OrderbookSnapshot:
    platform: str           # "kalshi" | "polymarket" | "gemini"
    ticker: str             # market ticker / token_id / symbol
    best_bid: float | None
    best_ask: float | None
    yes_mid: float | None
    depth_5pct: float       # for Kalshi/Polymarket (contracts within 5¢ of best bid)
    depth_3pct_usd: float   # for Gemini (USD value within 3¢ of best ask)
    volume_24h: float | None
    fetched_at: datetime    # UTC timestamp when snapshot was fetched


# ---------------------------------------------------------------------------
# OrderbookCache
# ---------------------------------------------------------------------------


class OrderbookCache:
    """
    In-memory cache of most recent OrderbookSnapshot per (platform, ticker).

    All mutations are protected by an asyncio.Lock for safe concurrent access
    from multiple coroutines.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], OrderbookSnapshot] = {}
        self._lock = asyncio.Lock()

    async def update(self, snapshot: OrderbookSnapshot) -> None:
        """Store snapshot keyed by (platform, ticker)."""
        async with self._lock:
            self._store[(snapshot.platform, snapshot.ticker)] = snapshot

    def get(self, platform: str, ticker: str) -> OrderbookSnapshot | None:
        """Return most recent OrderbookSnapshot for (platform, ticker), or None."""
        return self._store.get((platform, ticker))

    def get_all_for_pair(self, pair: "MatchedPair") -> dict[str, OrderbookSnapshot | None]:
        """
        Return a dict of snapshots for all platforms in a MatchedPair.

        Keys are platform names: "kalshi", "polymarket", "gemini".
        Values are the most recent snapshot or None if not cached.
        """
        ref = pair.ref
        target = pair.target

        result: dict[str, OrderbookSnapshot | None] = {
            target.platform: self.get(target.platform, target.id),
        }

        # ref can be kalshi or polymarket
        result[ref.platform] = self.get(ref.platform, ref.id)

        return result

    def is_fresh(self, platform: str, ticker: str, max_age_seconds: int) -> bool:
        """
        Return True iff the snapshot for (platform, ticker) exists and
        (now - fetched_at).total_seconds() <= max_age_seconds.
        """
        snapshot = self.get(platform, ticker)
        if snapshot is None:
            return False
        now = datetime.now(tz=timezone.utc)
        age = (now - snapshot.fetched_at).total_seconds()
        return age <= max_age_seconds
