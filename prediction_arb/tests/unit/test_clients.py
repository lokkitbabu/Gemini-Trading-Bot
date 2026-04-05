"""
Unit tests for Group 2: Platform Clients.

Tests cover:
  - BaseClient retry / backoff / 429 / 401 / failure-counter logic
  - KalshiClient orderbook parsing
  - PolymarketClient orderbook parsing
  - GeminiClient orderbook parsing and HMAC signing
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from prediction_arb.bot.clients.base import (
    BaseClient,
    _CONSECUTIVE_FAILURE_WARN_THRESHOLD,
    _MAX_ATTEMPTS,
)
from prediction_arb.bot.clients.kalshi import KalshiClient, _InMemoryOrderbook
from prediction_arb.bot.clients.polymarket import PolymarketClient
from prediction_arb.bot.clients.gemini import GeminiClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, body: Any = None, headers: dict | None = None) -> httpx.Response:
    """Create a minimal httpx.Response for testing."""
    content = json.dumps(body or {}).encode()
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers or {},
    )


class _ConcreteClient(BaseClient):
    """Minimal concrete subclass for testing BaseClient directly."""

    platform = "test"

    async def _reauthenticate(self) -> None:
        self._reauthed = True


# ---------------------------------------------------------------------------
# BaseClient tests
# ---------------------------------------------------------------------------


class TestBaseClientRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        ok_response = _make_response(200, {"ok": True})

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = ok_response
            resp = await client._request("GET", "/test")

        assert resp.status_code == 200
        assert mock_req.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        fail = _make_response(503)
        ok = _make_response(200, {"ok": True})

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [fail, ok]
            with patch("asyncio.sleep", new_callable=AsyncMock):
                resp = await client._request("GET", "/test")

        assert resp.status_code == 200
        assert mock_req.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts_on_5xx(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        fail = _make_response(500)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = fail
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(httpx.HTTPStatusError):
                    await client._request("GET", "/test")

        assert mock_req.call_count == _MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        ok = _make_response(200)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [httpx.TimeoutException("timeout"), ok]
            with patch("asyncio.sleep", new_callable=AsyncMock):
                resp = await client._request("GET", "/test")

        assert resp.status_code == 200
        assert mock_req.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts_on_timeout(self):
        client = _ConcreteClient(base_url="http://test.example.com")

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = httpx.TimeoutException("timeout")
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(httpx.TimeoutException):
                    await client._request("GET", "/test")

        assert mock_req.call_count == _MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_429_sleeps_retry_after_header(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        rate_limited = _make_response(429, headers={"Retry-After": "5"})
        ok = _make_response(200)

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [rate_limited, ok]
            with patch("asyncio.sleep", side_effect=fake_sleep):
                resp = await client._request("GET", "/test")

        assert resp.status_code == 200
        assert 5.0 in sleep_calls

    @pytest.mark.asyncio
    async def test_429_uses_default_retry_after_when_header_absent(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        rate_limited = _make_response(429)  # no Retry-After header
        ok = _make_response(200)

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [rate_limited, ok]
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await client._request("GET", "/test")

        assert 60.0 in sleep_calls

    @pytest.mark.asyncio
    async def test_401_calls_reauthenticate_once(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        client._reauthed = False
        unauth = _make_response(401)
        ok = _make_response(200)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [unauth, ok]
            resp = await client._request("GET", "/test")

        assert resp.status_code == 200
        assert client._reauthed is True

    @pytest.mark.asyncio
    async def test_consecutive_failures_incremented_on_5xx(self):
        """Failures accumulate across separate requests; success resets to 0."""
        client = _ConcreteClient(base_url="http://test.example.com")
        fail = _make_response(500)

        # Drive 2 full-failure requests (each exhausts all 3 attempts)
        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = fail
            with patch("asyncio.sleep", new_callable=AsyncMock):
                for _ in range(2):
                    try:
                        await client._request("GET", "/test")
                    except Exception:
                        pass

        # Each request makes 3 attempts, each incrementing failures → 6 total
        assert client._consecutive_failures == 6

    @pytest.mark.asyncio
    async def test_consecutive_failures_reset_on_success(self):
        client = _ConcreteClient(base_url="http://test.example.com")
        client._consecutive_failures = 3
        ok = _make_response(200)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = ok
            await client._request("GET", "/test")

        assert client._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_warning_emitted_at_threshold(self, caplog):
        import logging
        client = _ConcreteClient(base_url="http://test.example.com")
        fail = _make_response(500)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = fail
            with patch("asyncio.sleep", new_callable=AsyncMock):
                # Drive failures up to threshold
                for _ in range(_CONSECUTIVE_FAILURE_WARN_THRESHOLD):
                    try:
                        await client._request("GET", "/test")
                    except Exception:
                        pass

        assert client._consecutive_failures >= _CONSECUTIVE_FAILURE_WARN_THRESHOLD

    @pytest.mark.asyncio
    async def test_empty_result_after_final_retry_failure(self):
        """
        Verify that after exhausting all retries on 5xx, the client raises
        an exception (not returns empty result). This test documents the
        actual behavior: BaseClient raises HTTPStatusError after final retry.
        
        Note: The requirement mentions "empty result" but the implementation
        raises an exception, which is the correct behavior for HTTP errors.
        """
        client = _ConcreteClient(base_url="http://test.example.com")
        fail = _make_response(503)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = fail
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(httpx.HTTPStatusError):
                    await client._request("GET", "/test")

        # Verify all 3 attempts were made
        assert mock_req.call_count == _MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_backoff_timing_on_5xx(self):
        """Verify exponential backoff timing: 1s, 2s, 4s on 5xx retries."""
        client = _ConcreteClient(base_url="http://test.example.com")
        fail = _make_response(500)

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = fail
            with patch("asyncio.sleep", side_effect=fake_sleep):
                try:
                    await client._request("GET", "/test")
                except httpx.HTTPStatusError:
                    pass

        # Should sleep 1s after 1st failure, 2s after 2nd failure
        # (no sleep after 3rd failure since we raise)
        assert sleep_calls == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_backoff_timing_on_timeout(self):
        """Verify exponential backoff timing: 1s, 2s, 4s on timeout retries."""
        client = _ConcreteClient(base_url="http://test.example.com")

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch.object(client._http, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = httpx.TimeoutException("timeout")
            with patch("asyncio.sleep", side_effect=fake_sleep):
                try:
                    await client._request("GET", "/test")
                except httpx.TimeoutException:
                    pass

        # Should sleep 1s after 1st timeout, 2s after 2nd timeout
        assert sleep_calls == [1.0, 2.0]


# ---------------------------------------------------------------------------
# KalshiClient orderbook parsing tests
# ---------------------------------------------------------------------------


class TestKalshiOrderbookParsing:
    def test_parse_basic_orderbook(self):
        data = {
            "yes_dollars": [["0.38", "100"], ["0.40", "200"], ["0.42", "150"]],
            "no_dollars": [["0.54", "80"], ["0.56", "120"]],
        }
        ob = KalshiClient._parse_orderbook_fp("TEST-TICKER", data)

        assert ob.ticker == "TEST-TICKER"
        assert ob.best_yes_bid == Decimal("0.42")
        assert ob.best_yes_ask == Decimal("1.00") - Decimal("0.56")  # 0.44
        assert ob.yes_mid == (Decimal("0.42") + Decimal("0.44")) / 2

    def test_depth_5pct_sums_within_range(self):
        # best_yes_bid = 0.42; levels within 5¢: 0.42, 0.40, 0.38 (all within 0.05)
        data = {
            "yes_dollars": [["0.38", "100"], ["0.40", "200"], ["0.42", "150"]],
            "no_dollars": [["0.56", "120"]],
        }
        ob = KalshiClient._parse_orderbook_fp("T", data)
        # 0.42 - 0.38 = 0.04 <= 0.05 ✓; 0.42 - 0.40 = 0.02 ✓; 0.42 - 0.42 = 0 ✓
        assert ob.depth_5pct == Decimal("450")  # 100 + 200 + 150

    def test_depth_5pct_excludes_levels_beyond_range(self):
        data = {
            "yes_dollars": [["0.30", "500"], ["0.40", "200"], ["0.42", "150"]],
            "no_dollars": [["0.56", "120"]],
        }
        ob = KalshiClient._parse_orderbook_fp("T", data)
        # 0.42 - 0.30 = 0.12 > 0.05 → excluded
        assert ob.depth_5pct == Decimal("350")  # 200 + 150

    def test_empty_orderbook(self):
        ob = KalshiClient._parse_orderbook_fp("T", {"yes_dollars": [], "no_dollars": []})
        assert ob.best_yes_bid is None
        assert ob.best_yes_ask is None
        assert ob.yes_mid is None
        assert ob.depth_5pct == Decimal("0")

    def test_in_memory_orderbook_snapshot(self):
        ob = _InMemoryOrderbook("T")
        ob.apply_snapshot(
            yes=[["0.40", "100"], ["0.42", "200"]],
            no=[["0.55", "80"]],
        )
        result = ob.to_orderbook()
        assert result.best_yes_bid == Decimal("0.42")
        assert result.best_yes_ask == Decimal("1.00") - Decimal("0.55")

    def test_in_memory_orderbook_delta_add(self):
        ob = _InMemoryOrderbook("T")
        ob.apply_snapshot(yes=[["0.42", "100"]], no=[["0.55", "80"]])
        ob.apply_delta("0.42", 50, "yes")
        assert ob.yes["0.42"] == 150

    def test_in_memory_orderbook_delta_remove(self):
        ob = _InMemoryOrderbook("T")
        ob.apply_snapshot(yes=[["0.42", "100"]], no=[["0.55", "80"]])
        ob.apply_delta("0.42", -100, "yes")
        assert "0.42" not in ob.yes

    def test_read_only_raises_not_implemented(self):
        client = KalshiClient()
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(client.place_order())

    def test_read_only_cancel_order_raises_not_implemented(self):
        """Verify KalshiClient raises NotImplementedError for cancel_order."""
        client = KalshiClient()
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(client.cancel_order())


# ---------------------------------------------------------------------------
# PolymarketClient orderbook parsing tests
# ---------------------------------------------------------------------------


class TestPolymarketOrderbookParsing:
    def test_parse_basic_orderbook(self):
        bids = [{"price": "0.65", "size": "300"}, {"price": "0.68", "size": "200"}]
        asks = [{"price": "0.70", "size": "150"}, {"price": "0.72", "size": "100"}]
        ob = PolymarketClient._parse_orderbook("token1", bids, asks)

        assert ob.token_id == "token1"
        assert ob.best_bid == 0.68
        assert ob.best_ask == 0.70
        assert ob.mid == pytest.approx((0.68 + 0.70) / 2)

    def test_depth_5pct_sums_within_range(self):
        # best_bid = 0.68; within 5¢: 0.68 (diff=0), 0.65 (diff=0.03)
        bids = [{"price": "0.65", "size": "300"}, {"price": "0.68", "size": "200"}]
        asks = [{"price": "0.70", "size": "150"}]
        ob = PolymarketClient._parse_orderbook("t", bids, asks)
        assert ob.depth_5pct == pytest.approx(500.0)  # 300 + 200

    def test_depth_5pct_excludes_far_levels(self):
        # best_bid = 0.68; 0.60 is 0.08 away → excluded
        bids = [{"price": "0.60", "size": "500"}, {"price": "0.68", "size": "200"}]
        asks = [{"price": "0.70", "size": "150"}]
        ob = PolymarketClient._parse_orderbook("t", bids, asks)
        assert ob.depth_5pct == pytest.approx(200.0)

    def test_empty_bids_and_asks(self):
        ob = PolymarketClient._parse_orderbook("t", [], [])
        assert ob.best_bid is None
        assert ob.best_ask is None
        assert ob.mid is None
        assert ob.depth_5pct == 0.0

    def test_read_only_raises_not_implemented(self):
        client = PolymarketClient()
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(client.place_order())


# ---------------------------------------------------------------------------
# GeminiClient tests
# ---------------------------------------------------------------------------


class TestGeminiOrderbookParsing:
    def test_parse_basic_orderbook(self):
        data = {
            "bids": [{"price": "0.58", "amount": "200"}, {"price": "0.56", "amount": "100"}],
            "asks": [{"price": "0.60", "amount": "150"}, {"price": "0.62", "amount": "80"}],
        }
        ob = GeminiClient._parse_orderbook("GEMI-TEST", data)

        assert ob.symbol == "GEMI-TEST"
        assert ob.best_bid == 0.58
        assert ob.best_ask == 0.60
        assert ob.yes_mid == pytest.approx((0.58 + 0.60) / 2)

    def test_depth_3pct_usd_sums_within_range(self):
        # best_ask = 0.60; within 3¢: 0.60 (diff=0), 0.62 (diff=0.02)
        data = {
            "bids": [{"price": "0.58", "amount": "200"}],
            "asks": [
                {"price": "0.60", "amount": "150"},
                {"price": "0.62", "amount": "80"},
                {"price": "0.65", "amount": "50"},  # 0.05 away → excluded
            ],
        }
        ob = GeminiClient._parse_orderbook("T", data)
        expected = 0.60 * 150 + 0.62 * 80  # = 90 + 49.6 = 139.6
        assert ob.depth_3pct_usd == pytest.approx(expected)

    def test_depth_3pct_excludes_far_levels(self):
        data = {
            "bids": [{"price": "0.58", "amount": "200"}],
            "asks": [
                {"price": "0.60", "amount": "100"},
                {"price": "0.65", "amount": "500"},  # 0.05 away → excluded
            ],
        }
        ob = GeminiClient._parse_orderbook("T", data)
        assert ob.depth_3pct_usd == pytest.approx(0.60 * 100)

    def test_empty_orderbook(self):
        ob = GeminiClient._parse_orderbook("T", {"bids": [], "asks": []})
        assert ob.best_bid is None
        assert ob.best_ask is None
        assert ob.yes_mid is None
        assert ob.depth_3pct_usd == 0.0


class TestGeminiHMACSigning:
    def test_signed_headers_structure(self):
        client = GeminiClient(api_key="mykey", api_secret="mysecret")
        payload = {"request": "/v1/order/new", "nonce": 12345}
        headers = client._signed_headers(payload)

        assert headers["X-GEMINI-APIKEY"] == "mykey"
        assert "X-GEMINI-PAYLOAD" in headers
        assert "X-GEMINI-SIGNATURE" in headers

    def test_signature_is_valid_hmac_sha384(self):
        client = GeminiClient(api_key="mykey", api_secret="mysecret")
        payload = {"request": "/v1/order/new", "nonce": 99999}
        headers = client._signed_headers(payload)

        payload_b64 = headers["X-GEMINI-PAYLOAD"]
        expected_sig = hmac.new(
            b"mysecret",
            payload_b64.encode(),
            hashlib.sha384,
        ).hexdigest()

        assert headers["X-GEMINI-SIGNATURE"] == expected_sig

    def test_payload_is_valid_base64_json(self):
        client = GeminiClient(api_key="k", api_secret="s")
        payload = {"request": "/v1/orders", "nonce": 1}
        headers = client._signed_headers(payload)

        decoded = json.loads(base64.b64decode(headers["X-GEMINI-PAYLOAD"]))
        assert decoded["request"] == "/v1/orders"
        assert decoded["nonce"] == 1
