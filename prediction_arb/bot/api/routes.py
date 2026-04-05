"""
REST and SSE route handlers for the prediction arbitrage API.

All /api/v1/* routes require Bearer token authentication (via require_auth
dependency). /healthz and /metrics are mounted directly in server.py without auth.

Routes:
  GET /api/v1/status          — system status summary
  GET /api/v1/opportunities   — current actionable opportunities
  GET /api/v1/trades          — paginated trade history
  GET /api/v1/portfolio       — portfolio summary + open positions
  GET /api/v1/pnl/history     — time-series P&L snapshots
  GET /api/v1/feeds/health    — per-platform connectivity status
  GET /api/v1/events          — SSE stream
  GET /api/v1/orderbooks      — current in-memory orderbook snapshots
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sse_starlette.sse import EventSourceResponse

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Health check helper (also used by /healthz in server.py)
# ---------------------------------------------------------------------------


async def _build_health_response(app_state: Any) -> tuple[int, str]:
    """
    Build the /healthz response body and status code.

    Returns 200 when healthy, 503 when DB is unreachable or all platforms failing.
    """
    db_status = "ok"
    feeds: dict[str, str] = {}
    is_degraded = False

    # Check DB connectivity
    try:
        state_store = app_state.state_store
        if state_store is not None:
            # Lightweight probe: try to get open positions
            await state_store.get_open_positions()
    except Exception as exc:  # noqa: BLE001
        db_status = f"error: {exc}"
        is_degraded = True

    # Check feed health from scanner if available
    scanner = getattr(app_state, "scanner", None)
    if scanner is not None:
        try:
            feed_health = getattr(scanner, "feed_health", {})
            for platform, health in feed_health.items():
                status = getattr(health, "status", "unknown")
                feeds[platform] = status
                if status == "down":
                    is_degraded = True
        except Exception:  # noqa: BLE001
            pass

    if not feeds:
        feeds = {"kalshi": "unknown", "polymarket": "unknown", "gemini": "unknown"}

    # All platforms failing?
    if feeds and all(v == "down" for v in feeds.values()):
        is_degraded = True

    body = {
        "status": "degraded" if is_degraded else "ok",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "db": db_status,
        "feeds": feeds,
    }
    status_code = 503 if is_degraded else 200
    return status_code, json.dumps(body)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_router(app_state: Any, api_token: str) -> APIRouter:
    """
    Create and return an APIRouter with all authenticated /api/v1/* routes.

    Parameters
    ----------
    app_state:
        The FastAPI app.state object containing all injected dependencies.
    api_token:
        The bearer token string used to build the require_auth dependency.
    """
    from prediction_arb.bot.api.server import make_require_auth

    require_auth = make_require_auth(api_token)
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])

    # ------------------------------------------------------------------
    # GET /api/v1/status
    # ------------------------------------------------------------------

    @router.get("/status")
    async def get_status(request: Request) -> dict:
        state = request.app.state
        risk_manager = state.risk_manager
        state_store = state.state_store

        # Uptime
        start_time = getattr(state, "start_time", None)
        uptime_seconds: float = 0.0
        if start_time is not None:
            uptime_seconds = time.monotonic() - start_time

        # Open positions count
        open_positions = 0
        try:
            positions = await state_store.get_open_positions()
            open_positions = len(positions)
        except Exception:  # noqa: BLE001
            pass

        # Portfolio state from risk manager
        portfolio = getattr(risk_manager, "_portfolio", None)
        available_capital: float = 0.0
        realized_pnl: float = 0.0
        if portfolio is not None:
            available_capital = getattr(portfolio, "available_capital", 0.0)
            realized_pnl = getattr(portfolio, "realized_pnl", 0.0)
        else:
            # Fallback: read from config
            config = getattr(state, "config", None)
            if config is not None:
                available_capital = getattr(config, "capital", 0.0)

        # Scan count from scanner
        scanner = getattr(state, "scanner", None)
        scan_count: int = getattr(scanner, "scan_count", 0) if scanner else 0
        last_scan_at: str | None = None
        if scanner is not None:
            last_scan = getattr(scanner, "last_scan_at", None)
            if last_scan is not None:
                last_scan_at = last_scan.isoformat() if hasattr(last_scan, "isoformat") else str(last_scan)

        config = getattr(state, "config", None)
        mode = "dry_run" if (config and getattr(config, "dry_run", True)) else "live"

        return {
            "mode": mode,
            "uptime_seconds": round(uptime_seconds, 1),
            "scan_count": scan_count,
            "open_positions": open_positions,
            "available_capital_usd": round(available_capital, 2),
            "realized_pnl_usd": round(realized_pnl, 4),
            "last_scan_at": last_scan_at,
        }

    # ------------------------------------------------------------------
    # GET /api/v1/opportunities
    # ------------------------------------------------------------------

    @router.get("/opportunities")
    async def get_opportunities(request: Request) -> dict:
        state = request.app.state
        engine = state.engine

        opportunities = getattr(engine, "_last_opportunities", [])
        result = []
        for opp in opportunities:
            result.append({
                "id": opp.id,
                "detected_at": opp.detected_at.isoformat(),
                "event_title": opp.event_title,
                "asset": opp.asset,
                "price_level": opp.price_level,
                "resolution_date": opp.resolution_date,
                "signal_platform": opp.signal_platform,
                "signal_event_id": opp.signal_event_id,
                "signal_yes_price": opp.signal_yes_price,
                "gemini_event_id": opp.gemini_event_id,
                "gemini_yes_price": opp.gemini_yes_price,
                "gemini_bid": opp.gemini_bid,
                "gemini_ask": opp.gemini_ask,
                "gemini_depth": opp.gemini_depth,
                "spread": round(opp.spread, 6),
                "spread_pct": round(opp.spread_pct, 6),
                "direction": opp.direction,
                "entry_price": opp.entry_price,
                "kelly_fraction": round(opp.kelly_fraction, 6),
                "match_confidence": round(opp.match_confidence, 4),
                "days_to_resolution": opp.days_to_resolution,
                "risk_score": round(opp.risk_score, 4),
                "status": opp.status,
                "signal_disagreement": opp.signal_disagreement,
                "inverted": opp.inverted,
                "price_age_seconds": round(opp.price_age_seconds, 1),
            })

        return {"opportunities": result, "count": len(result)}

    # ------------------------------------------------------------------
    # GET /api/v1/trades?limit=50&offset=0
    # ------------------------------------------------------------------

    @router.get("/trades")
    async def get_trades(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        state = request.app.state
        state_store = state.state_store

        try:
            # Get all closed positions from StateStore
            from sqlalchemy import select
            from prediction_arb.bot.models import GeminiPositionModel

            async with state_store._session() as session:
                result = await session.execute(
                    select(GeminiPositionModel)
                    .where(GeminiPositionModel.status == "closed")
                    .order_by(GeminiPositionModel.opened_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                rows = result.scalars().all()

            trades = []
            for row in rows:
                trades.append({
                    "id": row.id,
                    "opportunity_id": row.opportunity_id,
                    "event_id": row.event_id,
                    "side": row.side,
                    "quantity": row.quantity,
                    "entry_price": float(row.entry_price),
                    "size_usd": float(row.size_usd),
                    "exit_strategy": row.exit_strategy,
                    "status": row.status,
                    "opened_at": row.opened_at.isoformat() if row.opened_at else None,
                    "closed_at": row.closed_at.isoformat() if row.closed_at else None,
                    "exit_price": float(row.exit_price) if row.exit_price is not None else None,
                    "realized_pnl": float(row.realized_pnl) if row.realized_pnl is not None else None,
                })

            return {"trades": trades, "limit": limit, "offset": offset, "count": len(trades)}

        except Exception as exc:  # noqa: BLE001
            log.error("trades_query_failed", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to fetch trades")

    # ------------------------------------------------------------------
    # GET /api/v1/portfolio
    # ------------------------------------------------------------------

    @router.get("/portfolio")
    async def get_portfolio(request: Request) -> dict:
        state = request.app.state
        state_store = state.state_store
        risk_manager = state.risk_manager

        # Open positions
        open_positions_data = []
        try:
            positions = await state_store.get_open_positions()
            for pos in positions:
                # Compute unrealized P&L if possible
                unrealized_pnl: float | None = None
                ob_cache = state.orderbook_cache
                if ob_cache is not None:
                    ob = ob_cache.get("gemini", pos.event_id)
                    if ob is not None and ob.yes_mid is not None:
                        if pos.side == "yes":
                            unrealized_pnl = (ob.yes_mid - pos.entry_price) * pos.quantity
                        else:
                            unrealized_pnl = (pos.entry_price - ob.yes_mid) * pos.quantity

                open_positions_data.append({
                    "id": pos.id,
                    "event_id": pos.event_id,
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "size_usd": pos.size_usd,
                    "exit_strategy": pos.exit_strategy,
                    "status": pos.status,
                    "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                    "unrealized_pnl": round(unrealized_pnl, 4) if unrealized_pnl is not None else None,
                    "days_to_resolution": pos.days_to_resolution,
                })
        except Exception as exc:  # noqa: BLE001
            log.error("portfolio_positions_failed", error=str(exc))

        # Aggregate stats
        try:
            stats = await state_store.get_aggregate_stats()
        except Exception:  # noqa: BLE001
            stats = None

        # Portfolio summary from risk manager
        portfolio_obj = getattr(risk_manager, "_portfolio", None)
        available_capital: float = 0.0
        peak_capital: float = 0.0
        drawdown_pct: float = 0.0
        if portfolio_obj is not None:
            available_capital = getattr(portfolio_obj, "available_capital", 0.0)
            peak_capital = getattr(portfolio_obj, "peak_capital", 0.0)
            drawdown_pct = getattr(portfolio_obj, "drawdown_pct", 0.0)

        return {
            "summary": {
                "open_positions": len(open_positions_data),
                "available_capital_usd": round(available_capital, 2),
                "peak_capital_usd": round(peak_capital, 2),
                "drawdown_pct": round(drawdown_pct, 4),
                "realized_pnl_usd": round(stats.total_pnl, 4) if stats else 0.0,
                "win_rate": round(stats.win_rate, 4) if stats else 0.0,
                "trade_count": stats.trade_count if stats else 0,
                "suspended": risk_manager.is_suspended() if risk_manager else False,
            },
            "positions": open_positions_data,
        }

    # ------------------------------------------------------------------
    # GET /api/v1/pnl/history?from=&to=
    # ------------------------------------------------------------------

    @router.get("/pnl/history")
    async def get_pnl_history(
        request: Request,
        from_: str = Query(alias="from", default=None),
        to: str = Query(default=None),
    ) -> dict:
        state = request.app.state
        state_store = state.state_store

        # Parse ISO 8601 timestamps
        try:
            from_ts = (
                datetime.fromisoformat(from_)
                if from_
                else datetime(2000, 1, 1, tzinfo=timezone.utc)
            )
            to_ts = (
                datetime.fromisoformat(to)
                if to
                else datetime.now(tz=timezone.utc)
            )
            # Ensure timezone-aware
            if from_ts.tzinfo is None:
                from_ts = from_ts.replace(tzinfo=timezone.utc)
            if to_ts.tzinfo is None:
                to_ts = to_ts.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid datetime format. Use ISO 8601. Error: {exc}",
            )

        try:
            snapshots = await state_store.get_pnl_history(from_ts, to_ts)
        except Exception as exc:  # noqa: BLE001
            log.error("pnl_history_query_failed", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to fetch P&L history")

        # Serialize datetime fields
        result = []
        for snap in snapshots:
            row = dict(snap)
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
            result.append(row)

        return {"snapshots": result, "count": len(result), "from": from_ts.isoformat(), "to": to_ts.isoformat()}

    # ------------------------------------------------------------------
    # GET /api/v1/feeds/health
    # ------------------------------------------------------------------

    @router.get("/feeds/health")
    async def get_feeds_health(request: Request) -> dict:
        state = request.app.state
        scanner = getattr(state, "scanner", None)

        platforms: dict[str, dict] = {}

        if scanner is not None:
            feed_health = getattr(scanner, "feed_health", {})
            for platform, health in feed_health.items():
                last_success = getattr(health, "last_success_at", None)
                platforms[platform] = {
                    "status": getattr(health, "status", "unknown"),
                    "last_success_at": last_success.isoformat() if last_success else None,
                    "consecutive_failures": getattr(health, "consecutive_failures", 0),
                }

        if not platforms:
            # No scanner data available yet
            for p in ("kalshi", "polymarket", "gemini"):
                platforms[p] = {
                    "status": "unknown",
                    "last_success_at": None,
                    "consecutive_failures": 0,
                }

        return {"feeds": platforms}

    # ------------------------------------------------------------------
    # GET /api/v1/events  — SSE stream
    # ------------------------------------------------------------------

    @router.get("/events")
    async def get_events(request: Request) -> EventSourceResponse:
        state = request.app.state
        sse_broadcaster = state.sse_broadcaster

        async def event_generator():
            async for chunk in sse_broadcaster.subscribe():
                # Parse the pre-formatted SSE string back into event/data parts
                # sse-starlette expects dicts with 'event' and 'data' keys
                lines = chunk.strip().split("\n")
                event_type = "message"
                data = ""
                for line in lines:
                    if line.startswith("event: "):
                        event_type = line[len("event: "):]
                    elif line.startswith("data: "):
                        data = line[len("data: "):]
                yield {"event": event_type, "data": data}

        return EventSourceResponse(event_generator())

    # ------------------------------------------------------------------
    # GET /api/v1/orderbooks
    # ------------------------------------------------------------------

    @router.get("/orderbooks")
    async def get_orderbooks(request: Request) -> dict:
        state = request.app.state
        ob_cache = state.orderbook_cache

        snapshots: list[dict] = []
        if ob_cache is not None:
            for (platform, ticker), snapshot in ob_cache._store.items():
                snapshots.append({
                    "platform": snapshot.platform,
                    "ticker": snapshot.ticker,
                    "best_bid": snapshot.best_bid,
                    "best_ask": snapshot.best_ask,
                    "yes_mid": snapshot.yes_mid,
                    "depth_5pct": snapshot.depth_5pct,
                    "depth_3pct_usd": snapshot.depth_3pct_usd,
                    "volume_24h": snapshot.volume_24h,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                })

        return {"orderbooks": snapshots, "count": len(snapshots)}

    return router
