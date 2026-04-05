"""
Prometheus metrics registry for the prediction arbitrage bot.

All metric objects are module-level singletons so they can be imported
anywhere without creating duplicate registrations.

Also provides ``MetricsExporter``, a lightweight wrapper that exposes a
``/metrics`` endpoint in Prometheus text format.  If the endpoint is
unavailable the system continues normally and logs a WARNING.
"""

from __future__ import annotations

import logging

import structlog
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Scan / engine metrics
# ---------------------------------------------------------------------------

SCAN_CYCLES_TOTAL = Counter(
    "arb_scan_cycles_total",
    "Total number of completed scan cycles",
)

OPPORTUNITIES_DETECTED_TOTAL = Counter(
    "arb_opportunities_detected_total",
    "Total opportunities detected that pass minimum spread and confidence filters",
    labelnames=["platform_pair"],
)

TRADES_EXECUTED_TOTAL = Counter(
    "arb_trades_executed_total",
    "Total orders submitted to Gemini",
    labelnames=["platform", "side"],
)

OPEN_POSITIONS = Gauge(
    "arb_open_positions",
    "Current number of open Gemini positions",
)

AVAILABLE_CAPITAL_USD = Gauge(
    "arb_available_capital_usd",
    "Current available capital in USD",
)

SCAN_DURATION_SECONDS = Histogram(
    "arb_scan_duration_seconds",
    "Wall-clock time of each scan cycle in seconds",
)

# ---------------------------------------------------------------------------
# Platform API latency
# ---------------------------------------------------------------------------

API_LATENCY_HISTOGRAM = Histogram(
    "arb_platform_api_latency_seconds",
    "Latency of each platform client API call in seconds",
    labelnames=["platform", "endpoint"],
)

# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------

REALIZED_PNL_USD = Gauge(
    "arb_realized_pnl_usd",
    "Cumulative realized P&L since system start in USD",
)

# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

MATCHER_CACHE_HIT_RATE = Gauge(
    "arb_matcher_cache_hit_rate",
    "Fraction of matcher lookups served from cache (0.0–1.0)",
)

MATCHER_LLM_CALLS_TOTAL = Counter(
    "arb_matcher_llm_calls_total",
    "Total LLM calls made by the event matcher",
    labelnames=["backend", "outcome"],
)

# ---------------------------------------------------------------------------
# Orderbook fetch
# ---------------------------------------------------------------------------

ORDERBOOK_FETCH_DURATION_SECONDS = Histogram(
    "arb_orderbook_fetch_duration_seconds",
    "Time to fetch an orderbook snapshot per platform",
    labelnames=["platform"],
)


# ---------------------------------------------------------------------------
# MetricsExporter
# ---------------------------------------------------------------------------


class MetricsExporter:
    """
    Exposes a ``/metrics`` endpoint in Prometheus text exposition format.

    Usage::

        exporter = MetricsExporter()
        content_type, body = exporter.get_metrics_response()

    If generating the metrics output fails for any reason the exporter logs a
    WARNING and returns an empty body so the system continues normally.
    """

    def get_metrics_response(self) -> tuple[str, bytes]:
        """
        Generate the Prometheus text output for all registered metrics.

        Returns:
            A ``(content_type, body)`` tuple where ``content_type`` is the
            Prometheus text format MIME type and ``body`` is the UTF-8 encoded
            metrics payload.

        On failure, logs a WARNING and returns ``(content_type, b"")``.
        """
        try:
            body: bytes = generate_latest()
            return CONTENT_TYPE_LATEST, body
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "metrics_endpoint_unavailable",
                error=str(exc),
                message="/metrics endpoint unavailable; continuing normal operation",
            )
            return CONTENT_TYPE_LATEST, b""
