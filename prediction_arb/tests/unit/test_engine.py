"""
Unit tests for ArbitrageEngine (engine.py) — task 5.1.

Tests cover:
- determine_direction()
- kelly_fraction()
- compute_reference_price()
- ArbitrageEngine.score() — stale orderbook rejection, spread-inside-noise rejection,
  inverted pair handling
- ArbitrageEngine.rank()
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from prediction_arb.bot.engine import (
    MAX_POSITION_PCT,
    Opportunity,
    ArbitrageEngine,
    compute_reference_price,
    determine_direction,
    kelly_fraction,
)
from prediction_arb.bot.orderbook_cache import OrderbookSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_snapshot(
    platform: str = "kalshi",
    ticker: str = "KXBTC-25MAR",
    best_bid: float = 0.60,
    best_ask: float = 0.64,
    yes_mid: float = 0.62,
    depth_5pct: float = 100.0,
    depth_3pct_usd: float = 200.0,
    volume_24h: float = 1000.0,
    age_seconds: float = 0.0,
) -> OrderbookSnapshot:
    fetched_at = datetime.now(tz=timezone.utc)
    if age_seconds > 0:
        from datetime import timedelta
        fetched_at = fetched_at - timedelta(seconds=age_seconds)
    return OrderbookSnapshot(
        platform=platform,
        ticker=ticker,
        best_bid=best_bid,
        best_ask=best_ask,
        yes_mid=yes_mid,
        depth_5pct=depth_5pct,
        depth_3pct_usd=depth_3pct_usd,
        volume_24h=volume_24h,
        fetched_at=fetched_at,
    )


def make_matched_pair(
    ref_platform: str = "kalshi",
    ref_yes_price: float | None = 0.72,
    gemini_yes_price: float | None = 0.58,
    inverted: bool = False,
    confidence: float = 0.85,
    resolution_date: str | None = None,
):
    """Build a minimal MatchedPair-like object for testing."""
    from prediction_arb.bot.matcher import MarketEvent, MatchResult, MatchedPair

    ref = MarketEvent(
        id="ref-1",
        title="Will BTC exceed $95k by March 31?",
        platform=ref_platform,
        yes_price=ref_yes_price,
    )
    target = MarketEvent(
        id="gem-1",
        title="BTC above $95k",
        platform="gemini",
        yes_price=gemini_yes_price,
    )
    result = MatchResult(
        equivalent=True,
        confidence=confidence,
        reasoning="same event",
        asset="BTC",
        price_level=95000.0,
        direction="above",
        resolution_date=resolution_date,
        inverted=inverted,
    )
    return MatchedPair(ref=ref, target=target, result=result)


# ---------------------------------------------------------------------------
# determine_direction tests
# ---------------------------------------------------------------------------


class TestDetermineDirection:
    def test_yes_when_ref_above_mid(self):
        side, entry = determine_direction(0.72, 0.58, gemini_bid=0.57, gemini_ask=0.59)
        assert side == "yes"
        assert entry == pytest.approx(0.59)  # gemini_ask

    def test_no_when_ref_below_mid(self):
        side, entry = determine_direction(0.40, 0.58, gemini_bid=0.57, gemini_ask=0.59)
        assert side == "no"
        assert entry == pytest.approx(1.0 - 0.57)  # 1 - gemini_bid

    def test_yes_fallback_to_mid_when_no_ask(self):
        side, entry = determine_direction(0.72, 0.58)
        assert side == "yes"
        assert entry == pytest.approx(0.58)  # falls back to gemini_mid

    def test_no_fallback_to_mid_when_no_bid(self):
        side, entry = determine_direction(0.40, 0.58)
        assert side == "no"
        assert entry == pytest.approx(1.0 - 0.58)

    def test_equal_prices_goes_no(self):
        # ref == gemini_mid → ref is NOT > gemini_mid → buy NO
        side, _ = determine_direction(0.58, 0.58)
        assert side == "no"


# ---------------------------------------------------------------------------
# kelly_fraction tests
# ---------------------------------------------------------------------------


class TestKellyFraction:
    def test_positive_edge_yes(self):
        # ref=0.72, entry=0.59 (YES ask), side=yes
        # p=0.72, b=(1-0.59)/0.59 ≈ 0.6949
        # f = (0.72*0.6949 - 0.28) / 0.6949 ≈ 0.3172
        # quarter-kelly ≈ 0.0793, capped at MAX_POSITION_PCT=0.05
        kf = kelly_fraction(0.72, 0.59, "yes")
        assert kf == pytest.approx(MAX_POSITION_PCT)

    def test_positive_edge_no(self):
        # ref=0.40, entry=0.43 (NO ask = 1-0.57), side=no
        # p=1-0.40=0.60, b=(1-0.43)/0.43 ≈ 1.3256
        # f = (0.60*1.3256 - 0.40) / 1.3256 ≈ 0.2985
        # quarter-kelly ≈ 0.0746, capped at 0.05
        kf = kelly_fraction(0.40, 0.43, "no")
        assert kf == pytest.approx(MAX_POSITION_PCT)

    def test_zero_when_no_edge(self):
        # ref=0.50, entry=0.50 → f = (0.5*1.0 - 0.5)/1.0 = 0.0
        kf = kelly_fraction(0.50, 0.50, "yes")
        assert kf == pytest.approx(0.0)

    def test_zero_when_negative_edge(self):
        # ref=0.30, entry=0.70 → p=0.30, b=(0.30)/0.70≈0.4286
        # f = (0.30*0.4286 - 0.70)/0.4286 < 0 → clamped to 0
        kf = kelly_fraction(0.30, 0.70, "yes")
        assert kf == 0.0

    def test_zero_when_entry_price_zero(self):
        kf = kelly_fraction(0.72, 0.0, "yes")
        assert kf == 0.0

    def test_zero_when_ref_price_zero(self):
        kf = kelly_fraction(0.0, 0.59, "yes")
        assert kf == 0.0

    def test_capped_at_max_position_pct(self):
        # Very high edge — should be capped
        kf = kelly_fraction(0.99, 0.01, "yes")
        assert kf == pytest.approx(MAX_POSITION_PCT)

    def test_custom_max_position_pct(self):
        kf = kelly_fraction(0.72, 0.59, "yes", max_position_pct=0.10)
        assert kf <= 0.10

    def test_quarter_kelly_applied(self):
        # Manually compute full Kelly and verify 0.25x is applied
        p = 0.72
        entry = 0.59
        b = (1.0 - entry) / entry
        full_f = (p * b - (1.0 - p)) / b
        expected = max(0.0, min(full_f * 0.25, MAX_POSITION_PCT))
        assert kelly_fraction(p, entry, "yes") == pytest.approx(expected)


# ---------------------------------------------------------------------------
# compute_reference_price tests
# ---------------------------------------------------------------------------


class TestComputeReferencePrice:
    def test_both_liquid_volume_weighted(self):
        k = make_snapshot("kalshi", yes_mid=0.70, depth_5pct=50.0, volume_24h=1000.0)
        p = make_snapshot("polymarket", yes_mid=0.80, depth_5pct=50.0, volume_24h=3000.0)
        ref, platform, disagreement = compute_reference_price(k, p)
        expected = (0.70 * 1000 + 0.80 * 3000) / 4000
        assert ref == pytest.approx(expected)
        assert platform == "both"
        assert disagreement is True  # |0.70 - 0.80| = 0.10 > 0.05

    def test_no_disagreement_when_close(self):
        k = make_snapshot("kalshi", yes_mid=0.70, depth_5pct=50.0, volume_24h=1000.0)
        p = make_snapshot("polymarket", yes_mid=0.72, depth_5pct=50.0, volume_24h=1000.0)
        _, _, disagreement = compute_reference_price(k, p)
        assert disagreement is False

    def test_kalshi_only_when_poly_illiquid(self):
        k = make_snapshot("kalshi", yes_mid=0.70, depth_5pct=50.0)
        p = make_snapshot("polymarket", yes_mid=0.80, depth_5pct=5.0)  # < 10
        ref, platform, _ = compute_reference_price(k, p)
        assert ref == pytest.approx(0.70)
        assert platform == "kalshi"

    def test_poly_only_when_kalshi_illiquid(self):
        k = make_snapshot("kalshi", yes_mid=0.70, depth_5pct=5.0)  # < 10
        p = make_snapshot("polymarket", yes_mid=0.80, depth_5pct=50.0)
        ref, platform, _ = compute_reference_price(k, p)
        assert ref == pytest.approx(0.80)
        assert platform == "polymarket"

    def test_raises_when_both_illiquid(self):
        k = make_snapshot("kalshi", depth_5pct=5.0)
        p = make_snapshot("polymarket", depth_5pct=5.0)
        with pytest.raises(ValueError, match="No liquid reference price"):
            compute_reference_price(k, p)

    def test_raises_when_both_none(self):
        with pytest.raises(ValueError):
            compute_reference_price(None, None)

    def test_kalshi_only_when_poly_none(self):
        k = make_snapshot("kalshi", yes_mid=0.65, depth_5pct=20.0)
        ref, platform, _ = compute_reference_price(k, None)
        assert ref == pytest.approx(0.65)
        assert platform == "kalshi"

    def test_volume_fallback_to_one_when_none(self):
        k = make_snapshot("kalshi", yes_mid=0.60, depth_5pct=20.0, volume_24h=None)
        p = make_snapshot("polymarket", yes_mid=0.80, depth_5pct=20.0, volume_24h=None)
        # Both volumes default to 1.0 → simple average
        ref, _, _ = compute_reference_price(k, p)
        assert ref == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# ArbitrageEngine.score() tests
# ---------------------------------------------------------------------------


class TestArbitrageEngineScore:
    def test_basic_opportunity_created(self):
        engine = ArbitrageEngine()
        pair = make_matched_pair(ref_yes_price=0.72, gemini_yes_price=0.58)
        opps = engine.score([pair])
        assert len(opps) == 1
        opp = opps[0]
        assert opp.direction == "buy_yes"
        assert opp.spread > 0
        assert opp.match_confidence == pytest.approx(0.85)

    def test_inverted_pair_flips_ref_price(self):
        engine = ArbitrageEngine()
        # ref=0.72 inverted → effective_ref = 1 - 0.72 = 0.28
        # gemini=0.35 → ref(0.28) < gemini(0.35) → buy NO
        pair = make_matched_pair(ref_yes_price=0.72, gemini_yes_price=0.35, inverted=True)
        opps = engine.score([pair])
        assert len(opps) == 1
        assert opps[0].direction == "buy_no"
        assert opps[0].inverted is True

    def test_skips_when_ref_price_missing(self):
        engine = ArbitrageEngine()
        pair = make_matched_pair(ref_yes_price=None, gemini_yes_price=0.58)
        opps = engine.score([pair])
        assert len(opps) == 0

    def test_skips_when_gemini_price_missing(self):
        engine = ArbitrageEngine()
        pair = make_matched_pair(ref_yes_price=0.72, gemini_yes_price=None)
        opps = engine.score([pair])
        assert len(opps) == 0

    def test_stale_orderbook_rejected(self):
        """Gemini snapshot older than max_price_age_seconds is rejected."""
        cache = MagicMock()
        stale_gemini = make_snapshot(
            "gemini", age_seconds=120  # older than default 60s
        )
        cache.get_all_for_pair.return_value = {
            "gemini": stale_gemini,
            "kalshi": make_snapshot("kalshi"),
        }
        engine = ArbitrageEngine(orderbook_cache=cache, max_price_age_seconds=60)
        pair = make_matched_pair()
        opps = engine.score([pair])
        assert len(opps) == 0

    def test_fresh_orderbook_accepted(self):
        """Gemini snapshot within max_price_age_seconds is accepted."""
        cache = MagicMock()
        fresh_gemini = make_snapshot(
            "gemini", best_bid=0.56, best_ask=0.60, yes_mid=0.58,
            depth_3pct_usd=200.0, age_seconds=10
        )
        kalshi_ob = make_snapshot("kalshi", yes_mid=0.72, depth_5pct=50.0)
        cache.get_all_for_pair.return_value = {
            "gemini": fresh_gemini,
            "kalshi": kalshi_ob,
        }
        engine = ArbitrageEngine(orderbook_cache=cache, max_price_age_seconds=60)
        pair = make_matched_pair()
        opps = engine.score([pair])
        assert len(opps) == 1

    def test_spread_inside_noise_rejected(self):
        """Spread inside Gemini bid-ask is rejected."""
        cache = MagicMock()
        # Gemini bid=0.69, ask=0.71 → spread=0.02, mid=0.70
        # ref=0.71 → diff=0.01 ≤ spread/2=0.01 → inside noise
        gemini_ob = make_snapshot(
            "gemini", best_bid=0.69, best_ask=0.71, yes_mid=0.70,
            depth_3pct_usd=200.0, age_seconds=5
        )
        kalshi_ob = make_snapshot("kalshi", yes_mid=0.71, depth_5pct=50.0)
        cache.get_all_for_pair.return_value = {
            "gemini": gemini_ob,
            "kalshi": kalshi_ob,
        }
        engine = ArbitrageEngine(orderbook_cache=cache)
        pair = make_matched_pair(ref_yes_price=0.71, gemini_yes_price=0.70)
        opps = engine.score([pair])
        assert len(opps) == 0

    def test_spread_outside_noise_accepted(self):
        """Spread clearly outside Gemini bid-ask is accepted."""
        cache = MagicMock()
        # Gemini bid=0.56, ask=0.60 → spread=0.04, mid=0.58
        # ref=0.72 → diff=0.14 >> spread/2=0.02 → outside noise
        gemini_ob = make_snapshot(
            "gemini", best_bid=0.56, best_ask=0.60, yes_mid=0.58,
            depth_3pct_usd=200.0, age_seconds=5
        )
        kalshi_ob = make_snapshot("kalshi", yes_mid=0.72, depth_5pct=50.0)
        cache.get_all_for_pair.return_value = {
            "gemini": gemini_ob,
            "kalshi": kalshi_ob,
        }
        engine = ArbitrageEngine(orderbook_cache=cache)
        pair = make_matched_pair(ref_yes_price=0.72, gemini_yes_price=0.58)
        opps = engine.score([pair])
        assert len(opps) == 1

    def test_days_to_resolution_computed(self):
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=5)).isoformat()
        engine = ArbitrageEngine()
        pair = make_matched_pair(resolution_date=future_date)
        opps = engine.score([pair])
        assert len(opps) == 1
        assert opps[0].days_to_resolution == 5

    def test_kelly_fraction_populated(self):
        engine = ArbitrageEngine()
        pair = make_matched_pair(ref_yes_price=0.72, gemini_yes_price=0.58)
        opps = engine.score([pair])
        assert len(opps) == 1
        assert opps[0].kelly_fraction >= 0.0
        assert opps[0].kelly_fraction <= MAX_POSITION_PCT

    def test_risk_score_in_range(self):
        engine = ArbitrageEngine()
        pair = make_matched_pair()
        opps = engine.score([pair])
        assert len(opps) == 1
        assert 0.0 <= opps[0].risk_score <= 1.0


# ---------------------------------------------------------------------------
# ArbitrageEngine.rank() tests
# ---------------------------------------------------------------------------


class TestArbitrageEngineRank:
    def _make_opp(self, spread_pct: float, risk_score: float) -> Opportunity:
        opp = Opportunity()
        opp.spread_pct = spread_pct
        opp.risk_score = risk_score
        return opp

    def test_sorted_by_spread_pct_descending(self):
        engine = ArbitrageEngine()
        opps = [
            self._make_opp(0.10, 0.5),
            self._make_opp(0.20, 0.5),
            self._make_opp(0.15, 0.5),
        ]
        ranked = engine.rank(opps)
        assert [o.spread_pct for o in ranked] == [0.20, 0.15, 0.10]

    def test_secondary_sort_by_risk_score_ascending(self):
        engine = ArbitrageEngine()
        opps = [
            self._make_opp(0.15, 0.8),
            self._make_opp(0.15, 0.3),
            self._make_opp(0.15, 0.5),
        ]
        ranked = engine.rank(opps)
        assert [o.risk_score for o in ranked] == [0.3, 0.5, 0.8]

    def test_empty_list(self):
        engine = ArbitrageEngine()
        assert engine.rank([]) == []

    def test_single_item(self):
        engine = ArbitrageEngine()
        opp = self._make_opp(0.12, 0.4)
        assert engine.rank([opp]) == [opp]
