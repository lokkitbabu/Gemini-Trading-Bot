# Feature: prediction-arbitrage-production
# Property 18: POST/PUT/DELETE/PATCH on REST endpoints returns 405
# Property 19: missing or incorrect token returns 401

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# App factory helper
# ---------------------------------------------------------------------------

_API_TOKEN = "test-secret-token-abc123"

_REST_ENDPOINTS = [
    "/api/v1/status",
    "/api/v1/opportunities",
    "/api/v1/trades",
    "/api/v1/portfolio",
    "/api/v1/pnl/history",
    "/api/v1/feeds/health",
]

_ALL_ENDPOINTS = _REST_ENDPOINTS + ["/api/v1/orderbooks"]


def _make_app():
    """Build a minimal FastAPI test app with mocked dependencies."""
    from prediction_arb.bot.api.server import create_app
    from prediction_arb.bot.config import Config

    cfg = Config()
    cfg.api_server_token = _API_TOKEN
    cfg.dashboard_origin = "http://localhost:3000"

    # Minimal mocks for all dependencies
    state_store = MagicMock()
    state_store.get_open_positions = AsyncMock(return_value=[])
    state_store.get_pnl_history = AsyncMock(return_value=[])
    state_store.get_aggregate_stats = AsyncMock(return_value=MagicMock(
        total_pnl=0.0, win_rate=0.0, avg_spread=0.0,
        exit_reason_breakdown={}, trade_count=0,
    ))

    engine = MagicMock()
    engine._current_opportunities = []

    risk_manager = MagicMock()
    risk_manager.is_suspended = MagicMock(return_value=False)

    scanner = MagicMock()
    scanner._consecutive_failures = {"kalshi": 0, "polymarket": 0, "gemini": 0}
    scanner._last_success = {"kalshi": None, "polymarket": None, "gemini": None}

    sse_broadcaster = MagicMock()
    sse_broadcaster.subscribe = MagicMock(return_value=iter([]))

    orderbook_cache = MagicMock()
    orderbook_cache._store = {}

    app = create_app(
        config=cfg,
        state_store=state_store,
        engine=engine,
        risk_manager=risk_manager,
        scanner=scanner,
        sse_broadcaster=sse_broadcaster,
        orderbook_cache=orderbook_cache,
    )
    return app


# ---------------------------------------------------------------------------
# Property 18: non-GET methods return 405
# ---------------------------------------------------------------------------

@given(
    st.sampled_from(_REST_ENDPOINTS),
    st.sampled_from(["POST", "PUT", "DELETE", "PATCH"]),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_18_non_get_returns_405(endpoint: str, method: str) -> None:
    """
    Property 18: POST/PUT/DELETE/PATCH on any REST endpoint returns 405.
    """
    from httpx import AsyncClient, ASGITransport

    app = _make_app()

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.request(
                method,
                endpoint,
                headers={"Authorization": f"Bearer {_API_TOKEN}"},
            )
            return response.status_code

    status = asyncio.get_event_loop().run_until_complete(_run())
    assert status == 405, (
        f"Expected 405 for {method} {endpoint}, got {status}"
    )


# ---------------------------------------------------------------------------
# Property 19: missing or incorrect token returns 401
# ---------------------------------------------------------------------------

@given(
    st.sampled_from(_REST_ENDPOINTS),
    st.one_of(
        st.none(),  # no token
        st.text(min_size=1, max_size=50).filter(lambda t: t != _API_TOKEN),  # wrong token
    ),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_19_invalid_token_returns_401(
    endpoint: str, bad_token: str | None
) -> None:
    """
    Property 19: for missing or incorrect token, any REST endpoint returns 401.
    """
    from httpx import AsyncClient, ASGITransport

    app = _make_app()

    async def _run():
        headers = {}
        if bad_token is not None:
            headers["Authorization"] = f"Bearer {bad_token}"

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(endpoint, headers=headers)
            return response.status_code

    status = asyncio.get_event_loop().run_until_complete(_run())
    assert status == 401, (
        f"Expected 401 for GET {endpoint} with token={bad_token!r}, got {status}"
    )
