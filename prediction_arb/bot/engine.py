"""
ArbitrageEngine — scores, filters, and ranks arbitrage opportunities.

This module also defines the Opportunity dataclass used throughout the system.
Inverted-pair handling (task 3.6): when MatchedPair.result.inverted=True, the
effective reference price is computed as 1.0 - ref_event.yes_price before
spread direction is evaluated.

Also provides compute_reference_price() for combining Kalshi and Polymarket
orderbook snapshots into a single volume-weighted reference price signal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from prediction_arb.bot.matcher import MatchedPair
    from prediction_arb.bot.orderbook_cache import OrderbookCache, OrderbookSnapshot

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config defaults (used when no Config object is injected)
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.05
MAX_PRICE_AGE_SECONDS = 60


# ---------------------------------------------------------------------------
# Opportunity dataclass
# ---------------------------------------------------------------------------


@dataclass
class Opportunity:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    detected_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    event_title: str = ""
    asset: str | None = None
    price_level: float | None = None
    resolution_date: str | None = None
    signal_platform: str = ""  # "kalshi" | "polymarket" | "both"
    signal_event_id: str = ""
    signal_yes_price: float = 0.0
    signal_volume: float = 0.0
    gemini_event_id: str = ""
    gemini_yes_price: float = 0.0
    gemini_volume: float = 0.0
    gemini_bid: float | None = None
    gemini_ask: float | None = None
    gemini_depth: float = 0.0  # depth_3pct_usd
    spread: float = 0.0
    spread_pct: float = 0.0
    direction: str = ""  # "buy_yes" | "buy_no"
    entry_price: float = 0.0
    kelly_fraction: float = 0.0
    match_confidence: float = 0.0
    days_to_resolution: int | None = None
    risk_score: float = 0.0
    status: str = "pending"  # "pending" | "executed" | "expired" | "skipped"
    signal_disagreement: bool = False
    inverted: bool = False  # True when matched pair has inverted YES/NO framing
    price_age_seconds: float = 0.0  # age of the most recent orderbook snapshot


# ---------------------------------------------------------------------------
# Reference price computation
# ---------------------------------------------------------------------------


def compute_reference_price(
    kalshi_ob: "OrderbookSnapshot | None",
    poly_ob: "OrderbookSnapshot | None",
) -> tuple[float, str, bool]:
    """
    Compute a consensus reference price from Kalshi and Polymarket orderbook snapshots.

    Returns (ref_price, signal_platform, disagreement_flag).

    Logic:
    - Volume-weighted average when both platforms have depth_5pct >= 10
    - Single-source fallback when only one platform is liquid
    - Sets disagreement=True when |kalshi_mid - poly_mid| > 0.05
    - Raises ValueError when no liquid reference price is available

    Reference price is always derived from OrderbookSnapshot.yes_mid,
    never from market list yes_price.
    """
    k_mid = kalshi_ob.yes_mid if kalshi_ob and kalshi_ob.depth_5pct >= 10 else None
    p_mid = poly_ob.yes_mid if poly_ob and poly_ob.depth_5pct >= 10 else None

    if k_mid is not None and p_mid is not None:
        disagreement = abs(k_mid - p_mid) > 0.05
        k_vol = kalshi_ob.volume_24h or 1.0  # type: ignore[union-attr]
        p_vol = poly_ob.volume_24h or 1.0    # type: ignore[union-attr]
        ref = (k_mid * k_vol + p_mid * p_vol) / (k_vol + p_vol)
        return ref, "both", disagreement
    elif k_mid is not None:
        return k_mid, "kalshi", False
    elif p_mid is not None:
        return p_mid, "polymarket", False
    else:
        raise ValueError("No liquid reference price available")


# ---------------------------------------------------------------------------
# Direction and Kelly helpers
# ---------------------------------------------------------------------------


def determine_direction(
    ref_price: float,
    gemini_mid: float,
    gemini_bid: float | None = None,
    gemini_ask: float | None = None,
) -> tuple[str, float]:
    """
    Determine trade direction and entry price.

    Returns (side, entry_price) where side is 'yes' or 'no'.

    If ref_price > gemini_mid: Gemini YES is underpriced → buy YES on Gemini
      entry_price = gemini_ask (pay the ask to get filled)
    If ref_price <= gemini_mid: Gemini YES is overpriced → buy NO on Gemini
      entry_price = 1.0 - gemini_bid (NO ask = 1 - YES bid)
    """
    if ref_price > gemini_mid:
        # YES is cheap on Gemini relative to reference
        entry = gemini_ask if gemini_ask is not None else gemini_mid
        return "yes", entry
    else:
        # NO is cheap on Gemini (YES is expensive)
        bid = gemini_bid if gemini_bid is not None else gemini_mid
        return "no", 1.0 - bid  # NO ask = 1 - YES bid


def kelly_fraction(
    ref_price: float,
    entry_price: float,
    side: str,
    max_position_pct: float = MAX_POSITION_PCT,
) -> float:
    """
    Compute quarter-Kelly position size fraction.

    Uses the corrected Kelly formula:
        f* = (p * b - (1 - p)) / b
    where:
        p = estimated win probability (from reference price)
        b = net odds (payout per dollar risked)

    Applies 0.25x fractional Kelly and caps at max_position_pct.
    Returns 0.0 if the bet has no positive expected value.
    """
    if entry_price <= 0:
        return 0.0

    if side == "yes":
        p = ref_price                          # prob of YES resolving
        b = (1.0 - entry_price) / entry_price  # net odds on YES
    else:  # "no"
        p = 1.0 - ref_price                    # prob of NO resolving
        b = (1.0 - entry_price) / entry_price  # net odds on NO (entry_price = NO ask)

    if b <= 0 or p <= 0:
        return 0.0

    f = (p * b - (1.0 - p)) / b

    # Apply fractional Kelly (0.25x) to account for model uncertainty
    # and cap at max_position_pct
    return max(0.0, min(f * 0.25, max_position_pct))


def _compute_risk_score(
    match_confidence: float,
    days_to_resolution: int | None,
    gemini_depth: float,
    min_gemini_depth_usd: float = 50.0,
) -> float:
    """
    Compute a composite risk score in [0.0, 1.0].

    Higher score = higher risk. Components:
    - Confidence risk: 1.0 - match_confidence
    - Time risk: longer time to resolution = higher risk (capped at 30 days)
    - Liquidity risk: low depth relative to minimum
    """
    confidence_risk = 1.0 - max(0.0, min(1.0, match_confidence))

    if days_to_resolution is not None and days_to_resolution > 0:
        time_risk = min(1.0, days_to_resolution / 30.0)
    else:
        time_risk = 0.5  # unknown resolution date = moderate risk

    if min_gemini_depth_usd > 0:
        liquidity_risk = max(0.0, 1.0 - gemini_depth / (min_gemini_depth_usd * 4))
    else:
        liquidity_risk = 0.0

    # Weighted composite
    return min(1.0, 0.4 * confidence_risk + 0.35 * time_risk + 0.25 * liquidity_risk)


# ---------------------------------------------------------------------------
# ArbitrageEngine
# ---------------------------------------------------------------------------


class ArbitrageEngine:
    """
    Scores and ranks arbitrage opportunities from matched event pairs.

    Inverted-pair handling (task 3.6):
    When MatchedPair.result.inverted=True, the effective reference price is
    1.0 - ref_event.yes_price (the complement), because one platform's YES
    corresponds to the other platform's NO.

    Stale orderbook rejection:
    Opportunities where the Gemini orderbook snapshot is older than
    max_price_age_seconds are rejected and logged as stale_orderbook.

    Spread-inside-noise rejection:
    Opportunities where the spread falls entirely within Gemini's own
    bid-ask spread are rejected (the signal is noise, not edge).
    """

    def __init__(
        self,
        orderbook_cache: "OrderbookCache | None" = None,
        max_price_age_seconds: int = MAX_PRICE_AGE_SECONDS,
        max_position_pct: float = MAX_POSITION_PCT,
    ) -> None:
        self._cache = orderbook_cache
        self._max_price_age_seconds = max_price_age_seconds
        self._max_position_pct = max_position_pct

    def score(self, pairs: list["MatchedPair"]) -> list[Opportunity]:
        """
        Score each MatchedPair and return a list of Opportunity objects.

        For each pair:
        1. Fetch orderbook snapshots from cache (if available)
        2. Compute reference price from Kalshi/Polymarket snapshots
        3. Reject stale orderbooks
        4. Compute gemini_mid, spread, spread_pct, direction, entry_price
        5. Reject spread-inside-noise
        6. Compute kelly_fraction and risk_score
        7. Handle inverted pairs by flipping reference price
        """
        opportunities: list[Opportunity] = []

        for pair in pairs:
            ref = pair.ref
            target = pair.target  # Gemini event
            result = pair.result

            # ------------------------------------------------------------------
            # Fetch orderbook snapshots from cache
            # ------------------------------------------------------------------
            kalshi_ob: OrderbookSnapshot | None = None
            poly_ob: OrderbookSnapshot | None = None
            gemini_ob: OrderbookSnapshot | None = None

            if self._cache is not None:
                snapshots = self._cache.get_all_for_pair(pair)
                gemini_ob = snapshots.get(target.platform)
                ref_ob = snapshots.get(ref.platform)
                if ref.platform == "kalshi":
                    kalshi_ob = ref_ob
                elif ref.platform == "polymarket":
                    poly_ob = ref_ob

            # ------------------------------------------------------------------
            # Stale orderbook check
            # ------------------------------------------------------------------
            now = datetime.now(tz=timezone.utc)
            if gemini_ob is not None:
                gemini_age = (now - gemini_ob.fetched_at).total_seconds()
                if gemini_age > self._max_price_age_seconds:
                    log.info(
                        "stale_orderbook",
                        ref_id=ref.id,
                        target_id=target.id,
                        platform="gemini",
                        age_seconds=gemini_age,
                        max_age=self._max_price_age_seconds,
                    )
                    continue

            # ------------------------------------------------------------------
            # Compute reference price
            # ------------------------------------------------------------------
            ref_yes_price = ref.yes_price
            gemini_yes_price = target.yes_price

            # Try to use orderbook snapshots for reference price
            ref_price: float | None = None
            signal_platform = ref.platform
            signal_disagreement = False

            if kalshi_ob is not None or poly_ob is not None:
                try:
                    ref_price, signal_platform, signal_disagreement = compute_reference_price(
                        kalshi_ob, poly_ob
                    )
                except ValueError:
                    # No liquid reference — fall back to market list price
                    ref_price = None

            # Fall back to market list yes_price if no orderbook data
            if ref_price is None:
                if ref_yes_price is None:
                    log.debug(
                        "score_skipped_missing_price",
                        ref_id=ref.id,
                        target_id=target.id,
                    )
                    continue
                ref_price = ref_yes_price

            if gemini_yes_price is None:
                # Try to get gemini mid from orderbook
                if gemini_ob is not None and gemini_ob.yes_mid is not None:
                    gemini_yes_price = gemini_ob.yes_mid
                else:
                    log.debug(
                        "score_skipped_missing_gemini_price",
                        ref_id=ref.id,
                        target_id=target.id,
                    )
                    continue

            # ------------------------------------------------------------------
            # Inverted-pair handling: flip reference price when framing is opposite
            # ------------------------------------------------------------------
            effective_ref_price = ref_price
            if result.inverted:
                effective_ref_price = 1.0 - ref_price
                log.debug(
                    "inverted_pair_price_flip",
                    ref_id=ref.id,
                    target_id=target.id,
                    original_ref_price=ref_price,
                    effective_ref_price=effective_ref_price,
                )

            # ------------------------------------------------------------------
            # Gemini bid/ask from orderbook snapshot
            # ------------------------------------------------------------------
            gemini_bid: float | None = None
            gemini_ask: float | None = None
            gemini_depth = 0.0
            price_age_seconds = 0.0

            if gemini_ob is not None:
                gemini_bid = gemini_ob.best_bid
                gemini_ask = gemini_ob.best_ask
                gemini_depth = gemini_ob.depth_3pct_usd
                price_age_seconds = (now - gemini_ob.fetched_at).total_seconds()

            # Compute gemini_mid
            if gemini_bid is not None and gemini_ask is not None:
                gemini_mid = (gemini_bid + gemini_ask) / 2.0
            else:
                gemini_mid = gemini_yes_price

            # ------------------------------------------------------------------
            # Spread-inside-noise rejection
            # ------------------------------------------------------------------
            if gemini_bid is not None and gemini_ask is not None:
                gemini_spread = gemini_ask - gemini_bid
                price_diff = abs(effective_ref_price - gemini_mid)
                if price_diff <= gemini_spread / 2.0:
                    log.info(
                        "spread_inside_noise",
                        ref_id=ref.id,
                        target_id=target.id,
                        ref_price=effective_ref_price,
                        gemini_mid=gemini_mid,
                        gemini_spread=gemini_spread,
                    )
                    continue

            # ------------------------------------------------------------------
            # Direction and entry price
            # ------------------------------------------------------------------
            side, entry_price = determine_direction(
                effective_ref_price, gemini_mid, gemini_bid, gemini_ask
            )
            direction = "buy_yes" if side == "yes" else "buy_no"

            # ------------------------------------------------------------------
            # Spread computation
            # ------------------------------------------------------------------
            spread = abs(effective_ref_price - gemini_mid)
            denom = min(effective_ref_price, gemini_mid)
            spread_pct = spread / denom if denom > 0 else 0.0

            # ------------------------------------------------------------------
            # Days to resolution
            # ------------------------------------------------------------------
            days_to_resolution: int | None = None
            if result.resolution_date:
                try:
                    from datetime import date
                    res_date = date.fromisoformat(result.resolution_date)
                    today = datetime.now(tz=timezone.utc).date()
                    days_to_resolution = max(0, (res_date - today).days)
                except (ValueError, TypeError):
                    pass

            # ------------------------------------------------------------------
            # Kelly fraction
            # ------------------------------------------------------------------
            kf = kelly_fraction(
                effective_ref_price, entry_price, side, self._max_position_pct
            )

            # ------------------------------------------------------------------
            # Risk score
            # ------------------------------------------------------------------
            rs = _compute_risk_score(
                match_confidence=result.confidence,
                days_to_resolution=days_to_resolution,
                gemini_depth=gemini_depth,
            )

            opp = Opportunity(
                event_title=ref.title,
                asset=result.asset,
                price_level=result.price_level,
                resolution_date=result.resolution_date,
                signal_platform=signal_platform,
                signal_event_id=ref.id,
                signal_yes_price=ref_yes_price if ref_yes_price is not None else ref_price,
                gemini_event_id=target.id,
                gemini_yes_price=gemini_yes_price,
                gemini_bid=gemini_bid,
                gemini_ask=gemini_ask,
                gemini_depth=gemini_depth,
                spread=spread,
                spread_pct=spread_pct,
                direction=direction,
                entry_price=entry_price,
                kelly_fraction=kf,
                match_confidence=result.confidence,
                days_to_resolution=days_to_resolution,
                risk_score=rs,
                signal_disagreement=signal_disagreement,
                inverted=result.inverted,
                price_age_seconds=price_age_seconds,
            )
            opportunities.append(opp)

        return opportunities

    def rank(self, opps: list[Opportunity]) -> list[Opportunity]:
        """
        Rank opportunities by spread_pct descending (primary) and
        risk_score ascending (secondary).
        """
        return sorted(opps, key=lambda o: (-o.spread_pct, o.risk_score))
