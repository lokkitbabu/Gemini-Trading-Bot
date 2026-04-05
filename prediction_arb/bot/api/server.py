"""
FastAPI application factory for the prediction arbitrage API server.

Creates the FastAPI app, configures CORS, mounts the metrics and health
endpoints (no auth), and registers all authenticated REST + SSE routes.

Authentication:
  All /api/v1/* endpoints require `Authorization: Bearer <API_SERVER_TOKEN>`.
  The `require_auth` dependency validates the token with secrets.compare_digest
  to prevent timing attacks.

CORS:
  Configured from Config.dashboard_origin with allow_credentials=True and
  allow_headers=["Authorization"].

HTTP 405:
  Non-GET methods on all REST endpoints return 405 Method Not Allowed.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from prediction_arb.bot.api.routes import create_router
from prediction_arb.bot.metrics import MetricsExporter

if TYPE_CHECKING:
    from prediction_arb.bot.api.sse import SSEBroadcaster
    from prediction_arb.bot.config import Config
    from prediction_arb.bot.engine import ArbitrageEngine
    from prediction_arb.bot.orderbook_cache import OrderbookCache
    from prediction_arb.bot.risk import RiskManager
    from prediction_arb.bot.state import StateStore

log = structlog.get_logger(__name__)

_metrics_exporter = MetricsExporter()


def create_app(
    config: "Config",
    state_store: "StateStore",
    engine: "ArbitrageEngine",
    risk_manager: "RiskManager",
    scanner: Any,
    sse_broadcaster: "SSEBroadcaster",
    orderbook_cache: "OrderbookCache",
) -> FastAPI:
    """
    FastAPI application factory.

    All dependencies are stored in app.state so route handlers can access them
    via `request.app.state`.

    Parameters
    ----------
    config:
        Loaded Config instance (provides api_server_token, dashboard_origin, etc.)
    state_store:
        StateStore for DB queries.
    engine:
        ArbitrageEngine for current opportunities.
    risk_manager:
        RiskManager for portfolio/suspension state.
    scanner:
        Scanner instance for feed health data.
    sse_broadcaster:
        SSEBroadcaster for the /api/v1/events endpoint.
    orderbook_cache:
        OrderbookCache for current orderbook snapshots.
    """
    app = FastAPI(
        title="Prediction Arbitrage API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # ------------------------------------------------------------------
    # Store dependencies in app.state
    # ------------------------------------------------------------------
    app.state.config = config
    app.state.state_store = state_store
    app.state.engine = engine
    app.state.risk_manager = risk_manager
    app.state.scanner = scanner
    app.state.sse_broadcaster = sse_broadcaster
    app.state.orderbook_cache = orderbook_cache
    app.state.start_time = None  # set by main loop after startup

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config.dashboard_origin],
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization"],
    )

    # ------------------------------------------------------------------
    # Unauthenticated routes: /healthz and /metrics
    # ------------------------------------------------------------------

    @app.get("/healthz", tags=["health"])
    async def healthz(request: Request) -> Response:
        from prediction_arb.bot.api.routes import _build_health_response
        status_code, body = await _build_health_response(request.app.state)
        return Response(
            content=body,
            status_code=status_code,
            media_type="application/json",
        )

    @app.get("/metrics", tags=["observability"])
    async def metrics() -> Response:
        content_type, body = _metrics_exporter.get_metrics_response()
        return Response(content=body, media_type=content_type)

    # ------------------------------------------------------------------
    # Authenticated API routes
    # ------------------------------------------------------------------
    router = create_router(app.state, config.api_server_token)
    app.include_router(router)

    # ------------------------------------------------------------------
    # HTTP 405 catch-all for non-GET methods on /api/* paths
    # ------------------------------------------------------------------
    @app.api_route(
        "/api/{path:path}",
        methods=["POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def method_not_allowed(path: str) -> Response:
        return Response(status_code=405)

    log.info(
        "api_server_created",
        dashboard_origin=config.dashboard_origin,
        port=config.api_server_port,
    )
    return app


def make_require_auth(api_token: str):
    """
    Return a FastAPI dependency that validates Bearer token auth.

    Uses secrets.compare_digest to prevent timing attacks.
    Returns HTTP 401 on missing or invalid token.
    """

    async def require_auth(request: Request) -> None:
        auth_header = request.headers.get("Authorization", "")
        # Also accept token as query param for EventSource compatibility
        token_param = request.query_params.get("token", "")

        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer "):]
        elif token_param:
            token = token_param

        if not token or not secrets.compare_digest(token, api_token):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_auth
