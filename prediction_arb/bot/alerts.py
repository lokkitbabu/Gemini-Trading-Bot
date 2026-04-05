"""
AlertManager — sends notifications when important system events occur.

Supported channels:
  slack   — HTTP POST to a Slack incoming webhook URL
  email   — SMTP (sync, run in asyncio executor)
  webhook — generic HTTP POST (JSON body)
  none    — no-op (default)

Deduplication:
  Alerts of the same ``alert_type`` are suppressed within
  ``dedup_window_seconds`` to prevent alert storms.

Delivery failures:
  On first failure the delivery is retried once.  If the retry also fails
  the failure is logged at WARNING level and the system continues normally.

No secret values are ever passed to any logger.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class AlertManager:
    """
    Sends alerts through the configured notification channel.

    All public ``send_*`` methods are async-safe and handle their own
    deduplication, retry, and error logging.
    """

    def __init__(
        self,
        channel: str = "none",
        slack_webhook_url: str = "",
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        smtp_from: str = "",
        smtp_to: str = "",
        webhook_url: str = "",
        dedup_window_seconds: int = 300,
        alert_spread_threshold: float = 0.20,
    ) -> None:
        self._channel = channel.lower()
        self._slack_webhook_url = slack_webhook_url
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._smtp_from = smtp_from
        self._smtp_to = smtp_to
        self._webhook_url = webhook_url
        self._dedup_window = dedup_window_seconds
        self._alert_spread_threshold = alert_spread_threshold

        # Deduplication: alert_type → last sent timestamp (monotonic)
        self._last_sent: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public alert methods
    # ------------------------------------------------------------------

    async def send_drawdown_alert(
        self,
        drawdown_pct: float,
        available_capital: float,
    ) -> None:
        """Send an alert when the drawdown kill-switch is triggered."""
        message = (
            f"[DRAWDOWN ALERT] Trading suspended. "
            f"Drawdown: {drawdown_pct:.1%}, "
            f"Available capital: ${available_capital:,.2f}"
        )
        await self.send_alert(
            message=message,
            level="critical",
            alert_type="drawdown_suspension",
        )

    async def send_platform_down_alert(
        self,
        platform: str,
        consecutive_failures: int,
    ) -> None:
        """Send an alert when a platform has been unavailable for > 3 consecutive cycles."""
        message = (
            f"[PLATFORM DOWN] {platform} has been unavailable for "
            f"{consecutive_failures} consecutive scan cycles."
        )
        await self.send_alert(
            message=message,
            level="warning",
            alert_type=f"platform_down_{platform.lower()}",
        )

    async def send_order_failure_alert(
        self,
        event_id: str,
        side: str,
        amount: float,
        error: str,
    ) -> None:
        """Send an alert immediately when a Gemini position execution fails."""
        message = (
            f"[ORDER FAILURE] Gemini position execution failed. "
            f"Event: {event_id}, Side: {side}, Amount: ${amount:,.2f}, "
            f"Error: {error}"
        )
        # Order failures are never deduplicated — each failure is unique
        await self._deliver(
            message=message,
            level="error",
            alert_type="order_failure",
            skip_dedup=True,
        )

    async def send_high_spread_alert(
        self,
        opportunity_id: str,
        spread_pct: float,
    ) -> None:
        """Send an alert when spread_pct exceeds ALERT_SPREAD_THRESHOLD."""
        if spread_pct <= self._alert_spread_threshold:
            return
        message = (
            f"[HIGH SPREAD] Opportunity {opportunity_id} has spread "
            f"{spread_pct:.1%} (threshold: {self._alert_spread_threshold:.1%})"
        )
        await self.send_alert(
            message=message,
            level="info",
            alert_type="high_spread",
        )

    async def send_alert(
        self,
        message: str,
        level: str = "info",
        alert_type: str = "generic",
    ) -> None:
        """
        Send a generic alert through the configured channel.

        Applies deduplication: if an alert of the same ``alert_type`` was
        sent within ``dedup_window_seconds``, the alert is suppressed.
        """
        await self._deliver(message=message, level=level, alert_type=alert_type)

    # ------------------------------------------------------------------
    # Internal delivery
    # ------------------------------------------------------------------

    async def _deliver(
        self,
        message: str,
        level: str,
        alert_type: str,
        skip_dedup: bool = False,
    ) -> None:
        """Apply deduplication then dispatch to the configured channel."""
        if self._channel == "none":
            return

        if not skip_dedup and self._is_duplicate(alert_type):
            log.debug(
                "alert_deduplicated",
                alert_type=alert_type,
                dedup_window=self._dedup_window,
            )
            return

        # Attempt delivery with one retry on failure
        success = await self._attempt_delivery(message, level, alert_type)
        if not success:
            log.warning(
                "alert_delivery_retry",
                alert_type=alert_type,
                channel=self._channel,
            )
            success = await self._attempt_delivery(message, level, alert_type)
            if not success:
                log.warning(
                    "alert_delivery_failed",
                    alert_type=alert_type,
                    channel=self._channel,
                    message="Alert delivery failed after retry; continuing normal operation",
                )
                return

        # Record successful delivery for deduplication
        self._last_sent[alert_type] = time.monotonic()

    async def _attempt_delivery(
        self,
        message: str,
        level: str,
        alert_type: str,
    ) -> bool:
        """
        Attempt a single delivery to the configured channel.

        Returns True on success, False on any exception.
        """
        try:
            if self._channel == "slack":
                await self._send_slack(message)
            elif self._channel == "email":
                await self._send_email(message, level)
            elif self._channel == "webhook":
                await self._send_webhook(message, level, alert_type)
            else:
                # Unknown channel — treat as no-op
                return True
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "alert_delivery_error",
                channel=self._channel,
                alert_type=alert_type,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Channel implementations
    # ------------------------------------------------------------------

    async def _send_slack(self, message: str) -> None:
        """POST a message to the Slack incoming webhook URL."""
        payload: dict[str, Any] = {"text": message}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(self._slack_webhook_url, json=payload)
            response.raise_for_status()

    async def _send_webhook(
        self,
        message: str,
        level: str,
        alert_type: str,
    ) -> None:
        """POST a JSON payload to the generic webhook URL."""
        payload: dict[str, Any] = {
            "message": message,
            "level": level,
            "alert_type": alert_type,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(self._webhook_url, json=payload)
            response.raise_for_status()

    async def _send_email(self, message: str, level: str) -> None:
        """Send an email via SMTP (sync, run in asyncio executor)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_email_sync, message, level)

    def _send_email_sync(self, message: str, level: str) -> None:
        """Synchronous SMTP delivery (called from executor)."""
        subject = f"[{level.upper()}] Prediction Arb Alert"
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = self._smtp_from
        msg["To"] = self._smtp_to

        with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
            server.ehlo()
            server.starttls()
            if self._smtp_user and self._smtp_password:
                server.login(self._smtp_user, self._smtp_password)
            server.sendmail(self._smtp_from, [self._smtp_to], msg.as_string())

    # ------------------------------------------------------------------
    # Deduplication helpers
    # ------------------------------------------------------------------

    def _is_duplicate(self, alert_type: str) -> bool:
        """Return True if an alert of this type was sent within the dedup window."""
        last = self._last_sent.get(alert_type)
        if last is None:
            return False
        return (time.monotonic() - last) < self._dedup_window
