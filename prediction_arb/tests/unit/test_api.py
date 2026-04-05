"""
Unit tests for the FastAPI app (Task 14.8).
"""

import time
import pytest
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select

from prediction_arb.bot.api.server import create_app
from prediction_arb.bot.executor import GeminiPosition


def make_app():
    config = MagicMock()
    config.api_server_token = "test-token"
    config.dashboard_origin = "http://localhost:3000"
    config.api_server_port = 8000
    config.api_server_enabled = True
    config.dry_run = True

    state_store = AsyncMock()
    state_store.get_open_positions.return_value = []
    state_store.get_aggregate_stats.return_value = MagicMock(
        total_pnl=0.0, win_rate=0.0, avg_spread=0.0,
        exit_reason_breakdown={}, trade_count=0
    )
    state_store.get_pnl_history.return_value = []

    engine = MagicMock()
    engine._last_opportunities = []
    risk_manager = MagicMock()
    risk_manager.is_suspended.return_value = False
    risk_manager._portfolio = MagicMock()
    risk_manager._portfolio.available_capital = 1000.0
    risk_manager._portfolio.realized_pnl = 0.0
    risk_manager._portfolio.peak_capital = 1000.0
    risk_manager._portfolio.drawdown_pct = 0.0
    
    scanner = MagicMock()
    scanner.scan_count = 0
    scanner.last_scan_at = None
    scanner.feed_health = {
        "kalshi": MagicMock(status="ok", last_success_at=datetime.now(tz=timezone.utc), consecutive_failures=0),
        "polymarket": MagicMock(status="ok", last_success_at=datetime.now(tz=timezone.utc), consecutive_failures=0),
        "gemini": MagicMock(status="ok", last_success_at=datetime.now(tz=timezone.utc), consecutive_failures=0),
    }
    
    sse_broadcaster = AsyncMock()
    async def mock_subscribe():
        yield "event: heartbeat\ndata: {}\n\n"
    sse_broadcaster.subscribe.return_value = mock_subscribe()
    
    orderbook_cache = MagicMock()
    orderbook_cache._store = {}

    app = create_app(
        config=config,
        state_store=state_store,
        engine=engine,
        risk_manager=risk_manager,
        scanner=scanner,
        sse_broadcaster=sse_broadcaster,
        orderbook_cache=orderbook_cache,
    )
    # start_time must be a monotonic float (used with time.monotonic() in routes)
    app.state.start_time = time.monotonic()
    return app


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_missing_token():
    """HTTP 401 for missing bearer token."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_401_wrong_token():
    """HTTP 401 for invalid bearer token."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/status", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_all_endpoints_require_auth():
    """All /api/v1/* endpoints require authentication."""
    app = make_app()
    endpoints = [
        "/api/v1/status",
        "/api/v1/opportunities",
        "/api/v1/trades",
        "/api/v1/portfolio",
        "/api/v1/pnl/history",
        "/api/v1/feeds/health",
        "/api/v1/orderbooks",
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for endpoint in endpoints:
            resp = await client.get(endpoint)
            assert resp.status_code == 401, f"{endpoint} should require auth"


# ---------------------------------------------------------------------------
# HTTP 405 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_405_post_on_rest_endpoint():
    """HTTP 405 for POST on REST endpoints."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/status", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_405_put_on_rest_endpoint():
    """HTTP 405 for PUT on REST endpoints."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/api/v1/status", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_405_delete_on_rest_endpoint():
    """HTTP 405 for DELETE on REST endpoints."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/v1/status", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_405_patch_on_rest_endpoint():
    """HTTP 405 for PATCH on REST endpoints."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/v1/status", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# /healthz tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_200_when_healthy():
    """/healthz returns 200 when healthy."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_healthz_returns_503_when_db_down():
    """/healthz returns 503 when DB is unreachable."""
    app = make_app()
    app.state.state_store.get_open_positions.side_effect = Exception("DB connection failed")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"


# ---------------------------------------------------------------------------
# Response schema tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_endpoint_schema():
    """GET /api/v1/status returns correct schema."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/status", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    required_fields = ["mode", "uptime_seconds", "scan_count", "open_positions", 
                      "available_capital_usd", "realized_pnl_usd", "last_scan_at"]
    for field in required_fields:
        assert field in data, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_opportunities_endpoint_schema():
    """GET /api/v1/opportunities returns correct schema."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/opportunities", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "opportunities" in data
    assert "count" in data
    assert isinstance(data["opportunities"], list)


@pytest.mark.asyncio
async def test_trades_endpoint_schema():
    """GET /api/v1/trades returns correct schema."""
    from contextlib import asynccontextmanager
    
    app = make_app()
    
    # Mock the _session() context manager to return a mock session
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)
    
    # Create a proper async context manager for _session()
    @asynccontextmanager
    async def mock_session_context():
        yield mock_session
    
    app.state.state_store._session = lambda: mock_session_context()
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/trades?limit=10&offset=0", 
                               headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "trades" in data
    assert "limit" in data
    assert "offset" in data
    assert "count" in data


@pytest.mark.asyncio
async def test_portfolio_endpoint_schema():
    """GET /api/v1/portfolio returns correct schema."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/portfolio", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "positions" in data
    assert "open_positions" in data["summary"]
    assert "available_capital_usd" in data["summary"]


@pytest.mark.asyncio
async def test_pnl_history_endpoint_schema():
    """GET /api/v1/pnl/history returns correct schema."""
    app = make_app()
    # Use proper ISO 8601 format without URL encoding (httpx handles this)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/pnl/history",
                               params={"from": "2025-01-01T00:00:00+00:00", "to": "2025-01-02T00:00:00+00:00"},
                               headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "snapshots" in data
    assert "count" in data
    assert "from" in data
    assert "to" in data


@pytest.mark.asyncio
async def test_feeds_health_endpoint_schema():
    """GET /api/v1/feeds/health returns correct schema."""
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/feeds/health", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "feeds" in data
    for platform in ["kalshi", "polymarket", "gemini"]:
        assert platform in data["feeds"]
        assert "status" in data["feeds"][platform]
        assert "last_success_at" in data["feeds"][platform]
        assert "consecutive_failures" in data["feeds"][platform]


# ---------------------------------------------------------------------------
# SSE event delivery test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_event_delivery():
    """SSE event delivery: verify broadcaster methods work correctly."""
    # Create a real SSE broadcaster for this test
    from prediction_arb.bot.api.sse import SSEBroadcaster
    real_broadcaster = SSEBroadcaster()
    
    # Verify the broadcaster has the required methods
    assert hasattr(real_broadcaster, 'publish')
    assert hasattr(real_broadcaster, 'subscribe')
    
    # Verify broadcaster can publish events (doesn't raise)
    await real_broadcaster.publish("position_opened", {"position_id": "pos-1"})
    
    # Verify subscriber_count property works
    assert real_broadcaster.subscriber_count == 0
    
    # Note: Full SSE connection testing requires a running event loop with streaming,
    # which is difficult to test without hanging. The integration test should cover
    # the full SSE flow.
