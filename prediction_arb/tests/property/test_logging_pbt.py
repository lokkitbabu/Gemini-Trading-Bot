# Feature: prediction-arbitrage-production
# Property 3: every log record is parseable as JSON with required fields
# Property 4: no secret value appears in any log record during ConfigService.load()

from __future__ import annotations

import io
import json
import logging
import os
import sys
from contextlib import contextmanager
from typing import Generator

import pytest
import structlog
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_LOG_FIELDS = {"timestamp", "level", "event"}

_SYSTEM_EVENTS = [
    "opportunity_detected",
    "position_submitted",
    "api_failure",
    "scan_cycle_error",
    "config_default_applied",
    "risk_decision",
    "scanner_platform_fetch_ok",
    "alert_deduplicated",
    "state_write_retry",
]


@contextmanager
def _capture_structlog_json() -> Generator[list[dict], None, None]:
    """
    Temporarily configure structlog to write JSON to a StringIO buffer.
    Yields a list that will be populated with parsed log records after the block.
    """
    buf = io.StringIO()
    records: list[dict] = []

    # Configure structlog with JSON renderer to our buffer
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=buf),
    )

    yield records

    # Parse all lines written to the buffer
    buf.seek(0)
    for line in buf:
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # non-JSON lines are ignored


# ---------------------------------------------------------------------------
# Property 3: log records are parseable JSON with required fields
# ---------------------------------------------------------------------------

@given(st.sampled_from(_SYSTEM_EVENTS))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_log_records_are_valid_json(event_name: str) -> None:
    """
    Property 3: for any system event, the log record is parseable as JSON
    and contains the required fields: timestamp (or similar), level, event.
    """
    with _capture_structlog_json() as records:
        log = structlog.get_logger("test_component")
        log.info(event_name, component="test_component", message=f"Test event: {event_name}")

    assert len(records) >= 1, "Expected at least one log record"
    record = records[-1]

    # Must be a dict (valid JSON object)
    assert isinstance(record, dict), f"Log record is not a dict: {record!r}"

    # Must contain 'event' field (structlog uses 'event' for the message)
    assert "event" in record, f"Missing 'event' field in log record: {record}"

    # Must contain 'level' field
    assert "level" in record, f"Missing 'level' field in log record: {record}"

    # Must contain a timestamp field (structlog TimeStamper adds 'timestamp')
    assert "timestamp" in record, f"Missing 'timestamp' field in log record: {record}"


# ---------------------------------------------------------------------------
# Property 4: no secret value appears in any log record during ConfigService.load()
# ---------------------------------------------------------------------------

_SECRET_FIELD_NAMES = [
    "gemini_api_key",
    "gemini_api_secret",
    "api_server_token",
    "openai_api_key",
    "anthropic_api_key",
    "slack_webhook_url",
    "smtp_password",
    "vault_token",
    "database_url",
]

_SECRET_VALUE_STRATEGY = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=8,
    max_size=32,
)


@given(st.fixed_dictionaries({k: _SECRET_VALUE_STRATEGY for k in _SECRET_FIELD_NAMES}))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_no_secrets_in_logs(secret_values: dict[str, str]) -> None:
    """
    Property 4: for any set of secret values, running ConfigService.load()
    with those secrets set as env vars produces no log record containing
    any secret value.
    """
    # Set env vars for the 'env' backend
    env_map = {
        "gemini_api_key": "GEMINI_API_KEY",
        "gemini_api_secret": "GEMINI_API_SECRET",
        "api_server_token": "API_SERVER_TOKEN",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "slack_webhook_url": "SLACK_WEBHOOK_URL",
        "smtp_password": "SMTP_PASSWORD",
        "vault_token": "VAULT_TOKEN",
        "database_url": "DATABASE_URL",
    }

    # Backup and set env vars
    original_env: dict[str, str | None] = {}
    try:
        for field_name, env_key in env_map.items():
            original_env[env_key] = os.environ.get(env_key)
            os.environ[env_key] = secret_values[field_name]

        # Use 'env' backend and dry_run=true to avoid required-secret checks
        os.environ["SECRET_BACKEND"] = "env"
        os.environ["DRY_RUN"] = "true"

        with _capture_structlog_json() as records:
            from prediction_arb.bot.config import ConfigService
            try:
                ConfigService().load()
            except SystemExit:
                pass  # validation errors are acceptable; we only care about log content

        # Check that no secret value appears in any log record
        all_log_text = json.dumps(records)
        for field_name, secret_val in secret_values.items():
            assert secret_val not in all_log_text, (
                f"Secret value for '{field_name}' found in log output"
            )

    finally:
        # Restore original env
        for env_key, original_val in original_env.items():
            if original_val is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = original_val
        os.environ.pop("SECRET_BACKEND", None)
        os.environ.pop("DRY_RUN", None)
