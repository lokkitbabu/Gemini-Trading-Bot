"""
ConfigService — loads, validates, and periodically refreshes all configuration.

Secret backends:
  aws   — AWS Secrets Manager via IAM instance profile (no hardcoded keys)
  vault — HashiCorp Vault via VAULT_TOKEN env var
  env   — plain environment variables only (dev / CI)

No secret value is ever passed to any logger.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field, fields
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Sentinel for "not provided" — distinct from None so we can detect absence
# ---------------------------------------------------------------------------
_MISSING = object()

# ---------------------------------------------------------------------------
# Secret field names — values are NEVER logged
# ---------------------------------------------------------------------------
_SECRET_FIELDS = frozenset(
    {
        "gemini_api_key",
        "gemini_api_secret",
        "kalshi_api_key",
        "kalshi_private_key",
        "api_server_token",
        "openai_api_key",
        "anthropic_api_key",
        "slack_webhook_url",
        "smtp_password",
        "vault_token",
        "database_url",
    }
)

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class Config:
    # ------------------------------------------------------------------
    # Non-secret config (env vars with defaults)
    # ------------------------------------------------------------------
    secret_backend: str = "aws"
    scan_interval_seconds: int = 300
    price_poll_interval_seconds: int = 30
    monitor_interval_seconds: int = 60
    min_spread_pct: float = 0.08
    max_positions: int = 10
    max_position_pct: float = 0.05
    capital: float = 1000.0
    max_drawdown_pct: float = 0.20
    max_opportunities_per_scan: int = 50
    min_confidence: float = 0.70
    max_risk: float = 0.80
    max_price_age_seconds: int = 60
    min_gemini_depth_usd: float = 50.0
    convergence_exit_days: int = 7
    convergence_threshold: float = 0.02
    stop_loss_pct: float = 0.15
    fee_per_contract: float = 0.0
    dry_run: bool = True
    api_server_enabled: bool = True
    api_server_port: int = 8000
    log_level: str = "INFO"
    matcher_backend: str = "rule_based"
    matcher_cache_ttl: int = 3600
    max_concurrent_llm_calls: int = 5
    alert_channel: str = "none"
    alert_spread_threshold: float = 0.20
    alert_dedup_window: int = 300
    kalshi_ws_enabled: bool = False
    polymarket_ws_enabled: bool = False
    dashboard_origin: str = "http://localhost:3000"

    # ------------------------------------------------------------------
    # Required secrets (no defaults — must be present in live mode)
    # ------------------------------------------------------------------
    gemini_api_key: str = ""
    gemini_api_secret: str = ""
    kalshi_api_key: str = ""          # optional for read-only
    kalshi_private_key: str = ""      # optional for read-only
    api_server_token: str = ""
    openai_api_key: str = ""          # required if matcher_backend=openai
    anthropic_api_key: str = ""       # required if matcher_backend=anthropic
    slack_webhook_url: str = ""       # required if alert_channel=slack
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""           # [SECRET]
    smtp_from: str = ""
    smtp_to: str = ""
    vault_token: str = ""             # required if secret_backend=vault
    vault_addr: str = "https://vault.example.com"
    database_url: str = "postgresql+asyncpg://arb:arb@localhost:5432/arbdb"


# ---------------------------------------------------------------------------
# Validation rules: (field_name, min_exclusive_or_none, max_exclusive_or_none, min_inclusive)
# ---------------------------------------------------------------------------
_RANGE_RULES: list[tuple[str, float | None, float | None]] = [
    # (field, min_value_inclusive, max_value_inclusive)
    ("min_spread_pct", 0.0, None),          # must be >= 0
    ("max_positions", 1, None),             # must be >= 1
    ("capital", None, None),                # checked separately (> 0)
    ("max_drawdown_pct", 0.0, 1.0),
    ("max_position_pct", 0.0, 1.0),
    ("min_confidence", 0.0, 1.0),
    ("max_risk", 0.0, 1.0),
    ("max_price_age_seconds", 1, None),
    ("min_gemini_depth_usd", 0.0, None),
    ("convergence_exit_days", 1, None),
    ("convergence_threshold", 0.0, 1.0),
    ("stop_loss_pct", 0.0, 1.0),
    ("fee_per_contract", 0.0, None),
    ("scan_interval_seconds", 1, None),
    ("price_poll_interval_seconds", 1, None),
    ("monitor_interval_seconds", 1, None),
    ("max_opportunities_per_scan", 1, None),
    ("api_server_port", 1, 65535),
    ("alert_dedup_window", 0, None),
    ("matcher_cache_ttl", 1, None),
    ("max_concurrent_llm_calls", 1, None),
]


# ---------------------------------------------------------------------------
# ConfigService
# ---------------------------------------------------------------------------


class ConfigService:
    """Loads, validates, and periodically refreshes configuration."""

    def __init__(self) -> None:
        self._config: Config | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> Config:
        """
        Load configuration from environment variables and the selected secret
        backend.  Exits with code 1 on any validation failure.
        """
        cfg = Config()

        # Step 1: load non-secret env vars (with defaults)
        self._load_non_secrets(cfg)

        # Step 2: load secrets from the selected backend
        backend = cfg.secret_backend.lower()
        if backend == "aws":
            self._load_aws(cfg)
        elif backend == "vault":
            self._load_vault(cfg)
        elif backend == "env":
            self._load_env_secrets(cfg)
        else:
            log.critical(
                "unknown_secret_backend",
                backend=backend,
                message=f"SECRET_BACKEND must be 'aws', 'vault', or 'env'; got '{backend}'",
            )
            sys.exit(1)

        # Step 3: validate
        self._validate(cfg)

        self._config = cfg
        return cfg

    def refresh_secrets(self) -> None:
        """
        Re-fetch secrets from the configured backend.  Called every 3600s by
        the scheduler.  Non-secret config is NOT reloaded (requires restart).
        """
        if self._config is None:
            log.warning("refresh_secrets_called_before_load")
            return

        cfg = self._config
        backend = cfg.secret_backend.lower()
        log.info("refreshing_secrets", backend=backend)

        try:
            if backend == "aws":
                self._load_aws(cfg)
            elif backend == "vault":
                self._load_vault(cfg)
            elif backend == "env":
                self._load_env_secrets(cfg)
        except Exception as exc:  # noqa: BLE001
            log.error("secret_refresh_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Non-secret loading
    # ------------------------------------------------------------------

    def _load_non_secrets(self, cfg: Config) -> None:
        """Read non-secret env vars; log INFO for each default applied."""
        env_map: dict[str, str] = {
            "secret_backend": "SECRET_BACKEND",
            "scan_interval_seconds": "SCAN_INTERVAL_SECONDS",
            "price_poll_interval_seconds": "PRICE_POLL_INTERVAL_SECONDS",
            "monitor_interval_seconds": "MONITOR_INTERVAL_SECONDS",
            "min_spread_pct": "MIN_SPREAD_PCT",
            "max_positions": "MAX_POSITIONS",
            "max_position_pct": "MAX_POSITION_PCT",
            "capital": "CAPITAL",
            "max_drawdown_pct": "MAX_DRAWDOWN_PCT",
            "max_opportunities_per_scan": "MAX_OPPORTUNITIES_PER_SCAN",
            "min_confidence": "MIN_CONFIDENCE",
            "max_risk": "MAX_RISK",
            "max_price_age_seconds": "MAX_PRICE_AGE_SECONDS",
            "min_gemini_depth_usd": "MIN_GEMINI_DEPTH_USD",
            "convergence_exit_days": "CONVERGENCE_EXIT_DAYS",
            "convergence_threshold": "CONVERGENCE_THRESHOLD",
            "stop_loss_pct": "STOP_LOSS_PCT",
            "fee_per_contract": "FEE_PER_CONTRACT",
            "dry_run": "DRY_RUN",
            "api_server_enabled": "API_SERVER_ENABLED",
            "api_server_port": "API_SERVER_PORT",
            "log_level": "LOG_LEVEL",
            "matcher_backend": "MATCHER_BACKEND",
            "matcher_cache_ttl": "MATCHER_CACHE_TTL",
            "max_concurrent_llm_calls": "MAX_CONCURRENT_LLM_CALLS",
            "alert_channel": "ALERT_CHANNEL",
            "alert_spread_threshold": "ALERT_SPREAD_THRESHOLD",
            "alert_dedup_window": "ALERT_DEDUP_WINDOW",
            "kalshi_ws_enabled": "KALSHI_WS_ENABLED",
            "polymarket_ws_enabled": "POLYMARKET_WS_ENABLED",
            "dashboard_origin": "DASHBOARD_ORIGIN",
            # Non-secret but env-sourced
            "smtp_host": "SMTP_HOST",
            "smtp_port": "SMTP_PORT",
            "smtp_user": "SMTP_USER",
            "smtp_from": "SMTP_FROM",
            "smtp_to": "SMTP_TO",
            "vault_addr": "VAULT_ADDR",
        }

        defaults = Config()  # used to detect when a default is applied

        for attr, env_key in env_map.items():
            raw = os.environ.get(env_key)
            if raw is None:
                default_val = getattr(defaults, attr)
                log.info(
                    "config_default_applied",
                    field=attr,
                    env_var=env_key,
                    default=str(default_val),
                )
                continue

            try:
                current = getattr(cfg, attr)
                coerced = self._coerce(raw, type(current))
                setattr(cfg, attr, coerced)
            except (ValueError, TypeError) as exc:
                log.critical(
                    "config_coerce_failed",
                    field=attr,
                    env_var=env_key,
                    error=str(exc),
                )
                sys.exit(1)

    # ------------------------------------------------------------------
    # Secret backends
    # ------------------------------------------------------------------

    def _load_aws(self, cfg: Config) -> None:
        """Load secrets from AWS Secrets Manager using IAM instance profile."""
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError:
            log.critical("boto3_not_installed", message="Install boto3 to use SECRET_BACKEND=aws")
            sys.exit(1)

        # boto3 automatically uses the IAM instance profile credential chain —
        # no access keys are ever hardcoded here.
        client = boto3.client("secretsmanager")

        secret_map = {
            "gemini_api_key": "arb/gemini_api_key",
            "gemini_api_secret": "arb/gemini_api_secret",
            "openai_api_key": "arb/openai_api_key",
            "anthropic_api_key": "arb/anthropic_api_key",
            "api_server_token": "arb/api_server_token",
            "slack_webhook_url": "arb/alert_webhook_url",
        }

        for attr, secret_name in secret_map.items():
            try:
                response = client.get_secret_value(SecretId=secret_name)
                value = response.get("SecretString", "")
                # SecretString may be a JSON object; try to unwrap single-key objects
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict) and len(parsed) == 1:
                        value = next(iter(parsed.values()))
                except (json.JSONDecodeError, TypeError):
                    pass
                setattr(cfg, attr, value)
            except Exception as exc:  # noqa: BLE001
                # Non-fatal at load time; required-secret check happens in _validate
                log.warning(
                    "aws_secret_fetch_failed",
                    secret_name=secret_name,
                    error=str(exc),
                    # value is intentionally NOT logged
                )

        # DATABASE_URL may also be stored as a secret
        db_secret = os.environ.get("DATABASE_URL")
        if db_secret:
            cfg.database_url = db_secret

    def _load_vault(self, cfg: Config) -> None:
        """Load secrets from HashiCorp Vault using VAULT_TOKEN env var."""
        try:
            import hvac  # type: ignore[import-untyped]
        except ImportError:
            log.critical("hvac_not_installed", message="Install hvac to use SECRET_BACKEND=vault")
            sys.exit(1)

        vault_token = os.environ.get("VAULT_TOKEN", "")
        if not vault_token:
            log.critical(
                "vault_token_missing",
                message="VAULT_TOKEN env var is required when SECRET_BACKEND=vault",
            )
            sys.exit(1)

        vault_addr = os.environ.get("VAULT_ADDR", cfg.vault_addr)
        client = hvac.Client(url=vault_addr, token=vault_token)

        secret_map = {
            "gemini_api_key": ("arb", "gemini_api_key"),
            "gemini_api_secret": ("arb", "gemini_api_secret"),
            "openai_api_key": ("arb", "openai_api_key"),
            "anthropic_api_key": ("arb", "anthropic_api_key"),
            "api_server_token": ("arb", "api_server_token"),
            "slack_webhook_url": ("arb", "alert_webhook_url"),
        }

        for attr, (path, key) in secret_map.items():
            try:
                response = client.secrets.kv.v2.read_secret_version(path=path)
                value = response["data"]["data"].get(key, "")
                setattr(cfg, attr, value)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "vault_secret_fetch_failed",
                    path=path,
                    key=key,
                    error=str(exc),
                    # value is intentionally NOT logged
                )

        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            cfg.database_url = db_url

    def _load_env_secrets(self, cfg: Config) -> None:
        """Load secrets directly from environment variables (dev / CI mode)."""
        env_secret_map = {
            "gemini_api_key": "GEMINI_API_KEY",
            "gemini_api_secret": "GEMINI_API_SECRET",
            "kalshi_api_key": "KALSHI_API_KEY",
            "kalshi_private_key": "KALSHI_PRIVATE_KEY",
            "api_server_token": "API_SERVER_TOKEN",
            "openai_api_key": "OPENAI_API_KEY",
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "slack_webhook_url": "SLACK_WEBHOOK_URL",
            "smtp_password": "SMTP_PASSWORD",
            "vault_token": "VAULT_TOKEN",
            "database_url": "DATABASE_URL",
        }

        for attr, env_key in env_secret_map.items():
            value = os.environ.get(env_key, "")
            if value:
                setattr(cfg, attr, value)
            # No logging of values — secrets must never appear in logs

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, cfg: Config) -> None:
        """Validate ranges and required secrets; exit(1) on any failure."""
        errors: list[str] = []

        # --- Range checks ---
        for field_name, min_val, max_val in _RANGE_RULES:
            value = getattr(cfg, field_name)
            if min_val is not None and value < min_val:
                errors.append(
                    f"{field_name}={value} is below minimum allowed value {min_val}"
                )
            if max_val is not None and value > max_val:
                errors.append(
                    f"{field_name}={value} exceeds maximum allowed value {max_val}"
                )

        # CAPITAL must be strictly positive
        if cfg.capital <= 0:
            errors.append(f"CAPITAL={cfg.capital} must be > 0")

        # --- Required-secret checks (only in live mode) ---
        if not cfg.dry_run:
            required_live = ["gemini_api_key", "gemini_api_secret", "api_server_token"]
            for secret in required_live:
                if not getattr(cfg, secret):
                    # Log field name only — never the value
                    log.critical(
                        "required_secret_missing",
                        field=secret,
                        message=f"Required secret '{secret.upper()}' is absent; "
                        "set it via the configured SECRET_BACKEND",
                    )
                    errors.append(f"Required secret '{secret}' is missing")

        # Conditional required secrets
        if cfg.matcher_backend == "openai" and not cfg.openai_api_key:
            log.critical(
                "required_secret_missing",
                field="openai_api_key",
                message="OPENAI_API_KEY is required when MATCHER_BACKEND=openai",
            )
            errors.append("openai_api_key required for MATCHER_BACKEND=openai")

        if cfg.matcher_backend == "anthropic" and not cfg.anthropic_api_key:
            log.critical(
                "required_secret_missing",
                field="anthropic_api_key",
                message="ANTHROPIC_API_KEY is required when MATCHER_BACKEND=anthropic",
            )
            errors.append("anthropic_api_key required for MATCHER_BACKEND=anthropic")

        if cfg.alert_channel == "slack" and not cfg.slack_webhook_url:
            log.critical(
                "required_secret_missing",
                field="slack_webhook_url",
                message="SLACK_WEBHOOK_URL is required when ALERT_CHANNEL=slack",
            )
            errors.append("slack_webhook_url required for ALERT_CHANNEL=slack")

        if cfg.secret_backend == "vault" and not cfg.vault_token:
            log.critical(
                "required_secret_missing",
                field="vault_token",
                message="VAULT_TOKEN is required when SECRET_BACKEND=vault",
            )
            errors.append("vault_token required for SECRET_BACKEND=vault")

        if errors:
            for err in errors:
                log.critical("config_validation_error", error=err)
            sys.exit(1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce(raw: str, target_type: type) -> Any:
        """Coerce a raw string env var to the target Python type."""
        if target_type is bool:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if target_type is int:
            return int(raw.strip())
        if target_type is float:
            return float(raw.strip())
        return raw  # str
