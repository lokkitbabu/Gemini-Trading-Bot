"""
Scanner — fetches market lists from all platforms in parallel.

Tracks per-platform health and triggers AlertManager after 3 consecutive failures.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from prediction_arb.bot.alerts import AlertManager
    from prediction_arb.bot.clients.gemini import GeminiClient
    from prediction_arb.bot.clients.kalshi import KalshiClient
    from prediction_arb.bot.clients.polymarket import PolymarketClient

log = structlog.get_logger(__name__)

_ALERT_THRESHOLD = 3  # consecutive failures before alerting


@dataclass
class FeedHealth:
    platform: str
    status: str = "up"          # "up" | "down"
    last_success_at: datetime | None = None
    consecutive_failures: int = 0


@dataclass
class ScanResult:
    kalshi: list = field(default_factory=list)
    polymarket: list = field(default_factory=list)
    gemini: list = field(default_factory=list)
    feed_health: dict[str, FeedHealth] = field(default_factory=dict)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class Scanner:
    """
    Fetches market lists from Kalshi, Polymarket, and Gemini in parallel.

    On per-platform failure: continues with remaining platforms, marks that
    platform as "down", and triggers AlertManager after 3 consecutive failures.
    """

    def __init__(
        self,
        kalshi_client: "KalshiClient",
        polymarket_client: "PolymarketClient",
        gemini_client: "GeminiClient",
        alert_manager: "AlertManager | None" = None,
    ) -> None:
        self._kalshi = kalshi_client
        self._polymarket = polymarket_client
        self._gemini = gemini_client
        self._alert_manager = alert_manager

        self._consecutive_failures: dict[str, int] = {
            "kalshi": 0,
            "polymarket": 0,
            "gemini": 0,
        }
        self._last_success: dict[str, datetime | None] = {
            "kalshi": None,
            "polymarket": None,
            "gemini": None,
        }

    async def fetch_all(self) -> ScanResult:
        """
        Fetch market lists from all platforms concurrently.

        Returns ScanResult with per-platform event lists and FeedHealth.
        Failures are isolated — one platform failing does not block others.
        """
        kalshi_task = asyncio.create_task(self._fetch_kalshi())
        poly_task = asyncio.create_task(self._fetch_polymarket())
        gemini_task = asyncio.create_task(self._fetch_gemini())

        results = await asyncio.gather(
            kalshi_task, poly_task, gemini_task, return_exceptions=True
        )

        kalshi_result, poly_result, gemini_result = results

        kalshi_events = await self._handle_result("kalshi", kalshi_result)
        poly_events = await self._handle_result("polymarket", poly_result)
        gemini_events = await self._handle_result("gemini", gemini_result)

        # Increment scan cycle counter
        try:
            from prediction_arb.bot.metrics import SCAN_CYCLES_TOTAL
            SCAN_CYCLES_TOTAL.inc()
        except Exception:  # noqa: BLE001
            pass

        feed_health = {
            platform: FeedHealth(
                platform=platform,
                status="up" if self._consecutive_failures[platform] == 0 else "down",
                last_success_at=self._last_success[platform],
                consecutive_failures=self._consecutive_failures[platform],
            )
            for platform in ("kalshi", "polymarket", "gemini")
        }

        return ScanResult(
            kalshi=kalshi_events,
            polymarket=poly_events,
            gemini=gemini_events,
            feed_health=feed_health,
        )

    async def _fetch_kalshi(self) -> list:
        return await self._kalshi.get_series()

    async def _fetch_polymarket(self) -> list:
        return await self._polymarket.get_markets()

    async def _fetch_gemini(self) -> list:
        return await self._gemini.get_events()

    async def _handle_result(self, platform: str, result: Any) -> list:
        """Process a platform fetch result, updating health tracking."""
        if isinstance(result, BaseException):
            self._consecutive_failures[platform] += 1
            failures = self._consecutive_failures[platform]
            log.warning(
                "scanner_platform_fetch_failed",
                platform=platform,
                consecutive_failures=failures,
                error=str(result),
            )
            if failures >= _ALERT_THRESHOLD and self._alert_manager is not None:
                try:
                    await self._alert_manager.send_platform_down_alert(
                        platform=platform,
                        consecutive_failures=failures,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("scanner_alert_failed", platform=platform, error=str(exc))
            return []

        # Success
        self._consecutive_failures[platform] = 0
        self._last_success[platform] = datetime.now(tz=timezone.utc)
        events = result if isinstance(result, list) else []
        log.debug(
            "scanner_platform_fetch_ok",
            platform=platform,
            event_count=len(events),
        )
        return events
