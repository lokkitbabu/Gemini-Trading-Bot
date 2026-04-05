"""
Main entrypoint for the prediction arbitrage bot.

Initialises all components, runs Alembic migrations, warms caches,
and drives three concurrent asyncio loops:
  - Slow loop  (Scanner + EventMatcher)       every SCAN_INTERVAL_SECONDS
  - Fast loop  (PricePoller + Engine + Risk + Executor) every PRICE_POLL_INTERVAL_SECONDS
  - Monitor    (PositionMonitor)              every MONITOR_INTERVAL_SECONDS
  - Secrets    (ConfigService.refresh_secrets) every 3600s

Pass --backtest to run the backtesting mode instead.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

import structlog
import uvicorn

log = structlog.get_logger(__name__)

# Shared state between slow and fast loops
_matched_pairs: list = []
_matched_pairs_lock = asyncio.Lock()

# Shutdown flag
_shutdown = False


def _handle_sigterm(signum: int, frame: Any) -> None:
    global _shutdown
    log.info("sigterm_received", message="Graceful shutdown initiated")
    _shutdown = True


async def _run_alembic_upgrade(database_url: str) -> None:
    """Run alembic upgrade head. Exit with code 1 on failure."""
    import os
    env = {**os.environ, "DATABASE_URL": database_url}
    proc = await asyncio.create_subprocess_exec(
        "alembic", "upgrade", "head",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.critical(
            "alembic_migration_failed",
            returncode=proc.returncode,
            stderr=stderr.decode(),
        )
        sys.exit(1)
    log.info("alembic_migration_complete", stdout=stdout.decode().strip())


async def _slow_loop(
    scanner: Any,
    matcher: Any,
    config: Any,
) -> None:
    """Slow loop: fetch markets and match events every SCAN_INTERVAL_SECONDS."""
    global _matched_pairs, _shutdown
    while not _shutdown:
        try:
            scan_result = await scanner.fetch_all()
            all_refs = scan_result.kalshi + scan_result.polymarket
            gemini_events = scan_result.gemini

            if all_refs and gemini_events:
                pairs = await matcher.batch_match(
                    refs=all_refs,
                    targets=gemini_events,
                    min_confidence=config.min_confidence,
                )
                async with _matched_pairs_lock:
                    _matched_pairs = pairs
                log.info(
                    "slow_loop_complete",
                    matched_pairs=len(pairs),
                    kalshi_events=len(scan_result.kalshi),
                    poly_events=len(scan_result.polymarket),
                    gemini_events=len(gemini_events),
                )
        except Exception as exc:  # noqa: BLE001
            log.error("slow_loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(60)
            continue

        await asyncio.sleep(config.scan_interval_seconds)


async def _fast_loop(
    price_poller: Any,
    engine: Any,
    risk_manager: Any,
    executor: Any,
    state_store: Any,
    config: Any,
) -> None:
    """Fast loop: poll prices, score opportunities, evaluate risk, execute."""
    global _matched_pairs, _shutdown
    while not _shutdown:
        try:
            async with _matched_pairs_lock:
                pairs = list(_matched_pairs)

            if pairs:
                price_poller._matched_pairs = pairs
                await price_poller.poll_once()
                opportunities = engine.score(pairs)
                ranked = engine.rank(opportunities)

                # Build portfolio state from StateStore
                open_positions = await state_store.get_open_positions()
                from prediction_arb.bot.risk import Portfolio
                portfolio = Portfolio(
                    open_positions=len(open_positions),
                    available_capital=config.capital - sum(
                        p.size_usd for p in open_positions
                        if p.status in ("open", "filled")
                    ),
                    peak_capital=config.capital,
                )

                risk_manager.reset_scan_counter()
                for opp in ranked:
                    decision = risk_manager.evaluate(opp, portfolio)
                    if decision.allowed:
                        size_usd = decision.clamped_size or (
                            opp.kelly_fraction * portfolio.available_capital
                        )
                        try:
                            await executor.execute(opp, size_usd)
                            portfolio.open_positions += 1
                            portfolio.available_capital -= size_usd
                        except Exception as exc:  # noqa: BLE001
                            log.error(
                                "fast_loop_execute_error",
                                opportunity_id=opp.id,
                                error=str(exc),
                            )
        except Exception as exc:  # noqa: BLE001
            log.error("fast_loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(60)
            continue

        await asyncio.sleep(config.price_poll_interval_seconds)


async def _monitor_loop(monitor: Any, config: Any) -> None:
    """Monitor loop: check open positions for stop-loss and convergence exits."""
    global _shutdown
    while not _shutdown:
        try:
            await monitor.run_once()
        except Exception as exc:  # noqa: BLE001
            log.error("monitor_loop_error", error=str(exc), exc_info=True)
        await asyncio.sleep(config.monitor_interval_seconds)


async def _secrets_refresh_loop(config_service: Any) -> None:
    """Refresh secrets every 3600 seconds."""
    global _shutdown
    while not _shutdown:
        await asyncio.sleep(3600)
        try:
            config_service.refresh_secrets()
        except Exception as exc:  # noqa: BLE001
            log.error("secrets_refresh_error", error=str(exc))


async def main() -> None:
    global _shutdown

    # ------------------------------------------------------------------
    # Handle --backtest flag
    # ------------------------------------------------------------------
    if "--backtest" in sys.argv:
        from prediction_arb.bot.backtest import backtest_main
        await backtest_main()
        return

    # ------------------------------------------------------------------
    # Load config
    # ------------------------------------------------------------------
    from prediction_arb.bot.config import ConfigService
    config_service = ConfigService()
    config = config_service.load()

    # ------------------------------------------------------------------
    # Setup logging
    # ------------------------------------------------------------------
    from prediction_arb.bot.logging_setup import setup_logging
    setup_logging(config.log_level)

    log.info("bot_starting", dry_run=config.dry_run, log_level=config.log_level)

    # ------------------------------------------------------------------
    # Register SIGTERM handler
    # ------------------------------------------------------------------
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # ------------------------------------------------------------------
    # Initialise StateStore and run migrations
    # ------------------------------------------------------------------
    from prediction_arb.bot.state import StateStore
    state_store = StateStore(config.database_url)
    await state_store.init()
    await _run_alembic_upgrade(config.database_url)

    # ------------------------------------------------------------------
    # Initialise all components
    # ------------------------------------------------------------------
    from prediction_arb.bot.orderbook_cache import OrderbookCache
    from prediction_arb.bot.clients.kalshi import KalshiClient
    from prediction_arb.bot.clients.polymarket import PolymarketClient
    from prediction_arb.bot.clients.gemini import GeminiClient
    from prediction_arb.bot.matcher import EventMatcher
    from prediction_arb.bot.engine import ArbitrageEngine
    from prediction_arb.bot.risk import RiskManager
    from prediction_arb.bot.executor import Executor
    from prediction_arb.bot.monitor import PositionMonitor
    from prediction_arb.bot.scanner import Scanner
    from prediction_arb.bot.price_poller import PricePoller
    from prediction_arb.bot.metrics import MetricsExporter
    from prediction_arb.bot.alerts import AlertManager
    from prediction_arb.bot.api.sse import SSEBroadcaster
    from prediction_arb.bot.api.server import create_app

    orderbook_cache = OrderbookCache()

    kalshi_client = KalshiClient(
        api_key=config.kalshi_api_key,
        private_key_pem=config.kalshi_private_key,
        ws_enabled=config.kalshi_ws_enabled,
    )
    polymarket_client = PolymarketClient(
        ws_enabled=config.polymarket_ws_enabled,
    )
    gemini_client = GeminiClient(
        api_key=config.gemini_api_key,
        api_secret=config.gemini_api_secret,
    )

    alert_manager = AlertManager(
        channel=config.alert_channel,
        slack_webhook_url=config.slack_webhook_url,
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_user=config.smtp_user,
        smtp_password=config.smtp_password,
        smtp_from=config.smtp_from,
        smtp_to=config.smtp_to,
        dedup_window_seconds=config.alert_dedup_window,
        alert_spread_threshold=config.alert_spread_threshold,
    )

    matcher = EventMatcher(
        backend=config.matcher_backend,
        cache_ttl_seconds=config.matcher_cache_ttl,
        max_concurrent_llm_calls=config.max_concurrent_llm_calls,
        openai_api_key=config.openai_api_key,
        anthropic_api_key=config.anthropic_api_key,
        state_store=state_store,
    )

    engine = ArbitrageEngine(
        orderbook_cache=orderbook_cache,
        max_price_age_seconds=config.max_price_age_seconds,
        max_position_pct=config.max_position_pct,
    )

    risk_manager = RiskManager(
        max_positions=config.max_positions,
        max_position_pct=config.max_position_pct,
        max_drawdown_pct=config.max_drawdown_pct,
        min_spread_pct=config.min_spread_pct,
        min_confidence=config.min_confidence,
        max_risk=config.max_risk,
        max_price_age_seconds=config.max_price_age_seconds,
        min_gemini_depth_usd=config.min_gemini_depth_usd,
        max_opportunities_per_scan=config.max_opportunities_per_scan,
        alert_manager=alert_manager,
    )

    sse_broadcaster = SSEBroadcaster()

    executor = Executor(
        gemini_client=gemini_client,
        state_store=state_store,
        sse_broadcaster=sse_broadcaster,
        orderbook_cache=orderbook_cache,
        dry_run=config.dry_run,
        max_price_age_seconds=config.max_price_age_seconds,
        convergence_exit_days=config.convergence_exit_days,
        stop_loss_pct=config.stop_loss_pct,
        alert_manager=alert_manager,
    )

    monitor = PositionMonitor(
        executor=executor,
        gemini_client=gemini_client,
        state_store=state_store,
        stop_loss_pct=config.stop_loss_pct,
        convergence_threshold=config.convergence_threshold,
        monitor_interval_seconds=config.monitor_interval_seconds,
    )

    scanner = Scanner(
        kalshi_client=kalshi_client,
        polymarket_client=polymarket_client,
        gemini_client=gemini_client,
        alert_manager=alert_manager,
    )

    price_poller = PricePoller(
        kalshi_client=kalshi_client,
        poly_client=polymarket_client,
        gemini_client=gemini_client,
        orderbook_cache=orderbook_cache,
        state_store=state_store,
    )

    metrics_exporter = MetricsExporter()

    # ------------------------------------------------------------------
    # Warm caches and restore state
    # ------------------------------------------------------------------
    log.info("warming_matcher_cache")
    await matcher.warm_cache_from_db()

    log.info("restoring_open_positions")
    open_positions = await state_store.get_open_positions()
    log.info("open_positions_restored", count=len(open_positions))

    # ------------------------------------------------------------------
    # Start API server (if enabled)
    # ------------------------------------------------------------------
    api_server_task: asyncio.Task | None = None
    if config.api_server_enabled:
        app = create_app(
            config=config,
            state_store=state_store,
            engine=engine,
            risk_manager=risk_manager,
            scanner=scanner,
            sse_broadcaster=sse_broadcaster,
            orderbook_cache=orderbook_cache,
        )
        app.state.start_time = datetime.now(tz=timezone.utc)

        uv_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=config.api_server_port,
            log_level="warning",
        )
        server = uvicorn.Server(uv_config)
        api_server_task = asyncio.create_task(server.serve())
        log.info("api_server_started", port=config.api_server_port)

    # ------------------------------------------------------------------
    # Run all loops concurrently
    # ------------------------------------------------------------------
    log.info("bot_running", dry_run=config.dry_run)

    tasks = [
        asyncio.create_task(_slow_loop(scanner, matcher, config)),
        asyncio.create_task(_fast_loop(price_poller, engine, risk_manager, executor, state_store, config)),
        asyncio.create_task(_monitor_loop(monitor, config)),
        asyncio.create_task(_secrets_refresh_loop(config_service)),
    ]
    if api_server_task:
        tasks.append(api_server_task)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("bot_shutting_down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state_store.close()
        log.info("bot_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
