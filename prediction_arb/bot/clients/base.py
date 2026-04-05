"""
BaseClient — abstract async HTTP client with retry, backoff, rate-limit
handling, auth refresh, consecutive-failure tracking, and latency metrics.

All platform clients (Kalshi, Polymarket, Gemini) inherit from this class.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

# ---------------------------------------------------------------------------
# Metrics — import lazily so the module works even before metrics.py exists
# ---------------------------------------------------------------------------
try:
    from prediction_arb.bot.metrics import API_LATENCY_HISTOGRAM  # type: ignore[import]
except ImportError:  # pragma: no cover
    API_LATENCY_HISTOGRAM = None  # type: ignore[assignment]

log = structlog.get_logger(__name__)

# Retry / backoff constants
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1.0, 2.0, 4.0]  # indexed by attempt number (0-based)
_DEFAULT_RETRY_AFTER = 60.0
_CONSECUTIVE_FAILURE_WARN_THRESHOLD = 5


class BaseClient(ABC):
    """
    Abstract async HTTP client.

    Subclasses must supply:
      - ``platform`` class attribute (str) — used for metric labels and logs
      - ``_reauthenticate()`` — called once on HTTP 401 before retrying
    """

    platform: str = "unknown"

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._consecutive_failures: int = 0
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
        )

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def _reauthenticate(self) -> None:
        """Refresh credentials / tokens without restarting the process."""

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        endpoint_label: str | None = None,
    ) -> httpx.Response:
        """
        Execute an HTTP request with retry / backoff logic.

        Retry policy:
          - Up to 3 attempts total.
          - Exponential backoff (1s, 2s, 4s) on timeout or 5xx response.
          - HTTP 429: sleep Retry-After header value (default 60s) then retry.
          - HTTP 401: call _reauthenticate() once, then retry (no extra sleep).
          - Any 2xx/3xx/4xx (except 401/429): return immediately.

        Raises ``httpx.HTTPStatusError`` after all retries are exhausted on 5xx.
        Raises ``httpx.TimeoutException`` after all retries are exhausted on timeout.
        """
        label = endpoint_label or path
        _reauthed = False

        for attempt in range(_MAX_ATTEMPTS):
            start = time.monotonic()
            try:
                response = await self._http.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                )
                elapsed = time.monotonic() - start
                self._record_latency(label, elapsed)

                # ---- HTTP 429 rate-limit --------------------------------
                if response.status_code == 429:
                    retry_after = float(
                        response.headers.get("Retry-After", _DEFAULT_RETRY_AFTER)
                    )
                    log.warning(
                        "rate_limited",
                        platform=self.platform,
                        endpoint=label,
                        retry_after=retry_after,
                        attempt=attempt,
                    )
                    await asyncio.sleep(retry_after)
                    continue  # retry without counting as a failure

                # ---- HTTP 401 unauthorised ------------------------------
                if response.status_code == 401 and not _reauthed:
                    log.warning(
                        "auth_expired",
                        platform=self.platform,
                        endpoint=label,
                        attempt=attempt,
                    )
                    await self._reauthenticate()
                    _reauthed = True
                    continue  # retry with fresh credentials

                # ---- 5xx server error ----------------------------------
                if response.status_code >= 500:
                    log.warning(
                        "server_error",
                        platform=self.platform,
                        endpoint=label,
                        status=response.status_code,
                        attempt=attempt,
                    )
                    self._increment_failures()
                    if attempt < _MAX_ATTEMPTS - 1:
                        await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    else:
                        raise httpx.HTTPStatusError(
                            f"Server error {response.status_code}",
                            request=httpx.Request(method, path),
                            response=response,
                        )
                    continue

                # ---- Success -------------------------------------------
                self._reset_failures()
                return response

            except httpx.TimeoutException:
                elapsed = time.monotonic() - start
                self._record_latency(label, elapsed)
                log.warning(
                    "request_timeout",
                    platform=self.platform,
                    endpoint=label,
                    attempt=attempt,
                )
                self._increment_failures()
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                else:
                    raise

        # Should not be reached, but satisfy type checker
        raise RuntimeError(f"Exhausted {_MAX_ATTEMPTS} attempts for {method} {path}")

    # ------------------------------------------------------------------
    # Failure tracking
    # ------------------------------------------------------------------

    def _increment_failures(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures == _CONSECUTIVE_FAILURE_WARN_THRESHOLD:
            log.warning(
                "consecutive_failures_threshold",
                platform=self.platform,
                count=self._consecutive_failures,
                message=f"{self.platform} has reached {self._consecutive_failures} consecutive failures",
            )

    def _reset_failures(self) -> None:
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _record_latency(self, endpoint: str, elapsed: float) -> None:
        """Record a latency observation on the shared histogram (if available)."""
        if API_LATENCY_HISTOGRAM is not None:
            try:
                API_LATENCY_HISTOGRAM.labels(
                    platform=self.platform, endpoint=endpoint
                ).observe(elapsed)
            except Exception:  # noqa: BLE001
                pass  # never let metrics crash the client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._http.aclose()

    async def __aenter__(self) -> "BaseClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
