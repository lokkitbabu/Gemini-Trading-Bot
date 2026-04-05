"""
Unit tests for compute_reference_price in engine.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prediction_arb.bot.engine import compute_reference_price
from prediction_arb.bot.orderbook_cache import OrderbookSnapshot


def _make_ob(
    platform: str,
    ticker: str,
    yes_mid: float,
    depth_5pct: float = 20.0,
    volume_24h: float | None = 1000.0,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        platform=platform,
        ticker=ticker,
        best_bid=yes_mid - 0.01,
        best_ask=yes_mid + 0.01,
        yes_mid=yes_mid,
        depth_5pct=depth_5pct,
        depth_3pct_usd=0.0,
        volume_24h=volume_24h,
        fetched_at=datetime.now(tz=timezone.utc),
    )


class TestComputeReferencePriceBothLiquid:
    def test_equal_volumes_averages_mids(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, volume_24h=1000.0)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.64, volume_24h=1000.0)
        ref, platform, disagreement = compute_reference_price(k_ob, p_ob)
        assert platform == "both"
        assert ref == pytest.approx(0.62, abs=1e-6)
        assert disagreement is False

    def test_volume_weighted_average(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, volume_24h=3000.0)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.70, volume_24h=1000.0)
        ref, platform, disagreement = compute_reference_price(k_ob, p_ob)
        # (0.60 * 3000 + 0.70 * 1000) / 4000 = 2500/4000 = 0.625
        assert platform == "both"
        assert ref == pytest.approx(0.625, abs=1e-6)

    def test_disagreement_flag_set_when_diff_gt_5_cents(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.66)
        _, _, disagreement = compute_reference_price(k_ob, p_ob)
        assert disagreement is True

    def test_no_disagreement_when_diff_exactly_5_cents(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.64)
        _, _, disagreement = compute_reference_price(k_ob, p_ob)
        # |0.60 - 0.64| = 0.04, not > 0.05
        assert disagreement is False

    def test_none_volume_uses_fallback_1(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, volume_24h=None)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.70, volume_24h=None)
        ref, platform, _ = compute_reference_price(k_ob, p_ob)
        # Both volumes default to 1.0 → equal weight
        assert platform == "both"
        assert ref == pytest.approx(0.65, abs=1e-6)


class TestComputeReferencePriceSingleSource:
    def test_kalshi_only_liquid(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, depth_5pct=20.0)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.70, depth_5pct=5.0)  # illiquid
        ref, platform, disagreement = compute_reference_price(k_ob, p_ob)
        assert platform == "kalshi"
        assert ref == pytest.approx(0.60)
        assert disagreement is False

    def test_polymarket_only_liquid(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, depth_5pct=5.0)  # illiquid
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.70, depth_5pct=20.0)
        ref, platform, disagreement = compute_reference_price(k_ob, p_ob)
        assert platform == "polymarket"
        assert ref == pytest.approx(0.70)
        assert disagreement is False

    def test_kalshi_only_no_poly(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, depth_5pct=20.0)
        ref, platform, disagreement = compute_reference_price(k_ob, None)
        assert platform == "kalshi"
        assert ref == pytest.approx(0.60)
        assert disagreement is False

    def test_polymarket_only_no_kalshi(self):
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.70, depth_5pct=20.0)
        ref, platform, disagreement = compute_reference_price(None, p_ob)
        assert platform == "polymarket"
        assert ref == pytest.approx(0.70)
        assert disagreement is False


class TestComputeReferencePriceNoLiquidity:
    def test_both_none_raises(self):
        with pytest.raises(ValueError, match="No liquid reference price available"):
            compute_reference_price(None, None)

    def test_both_illiquid_raises(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, depth_5pct=5.0)
        p_ob = _make_ob("polymarket", "P1", yes_mid=0.70, depth_5pct=3.0)
        with pytest.raises(ValueError, match="No liquid reference price available"):
            compute_reference_price(k_ob, p_ob)

    def test_exactly_10_depth_is_liquid(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, depth_5pct=10.0)
        ref, platform, _ = compute_reference_price(k_ob, None)
        assert platform == "kalshi"
        assert ref == pytest.approx(0.60)

    def test_depth_9_is_illiquid(self):
        k_ob = _make_ob("kalshi", "K1", yes_mid=0.60, depth_5pct=9.0)
        with pytest.raises(ValueError):
            compute_reference_price(k_ob, None)
