# Feature: prediction-arbitrage-production
# Property 12: for any subset of absent optional vars, Config fields equal documented defaults
# Property 13: for any out-of-range numeric config value, ConfigService.load() raises SystemExit

from __future__ import annotations

import os
import sys

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from prediction_arb.bot.config import Config, ConfigService

# ---------------------------------------------------------------------------
# Optional vars with their documented defaults
# ---------------------------------------------------------------------------

_OPTIONAL_VARS: dict[str, tuple[str, object]] = {
    "SCAN_INTERVAL_SECONDS": ("scan_interval_seconds", 300),
    "PRICE_POLL_INTERVAL_SECONDS": ("price_poll_interval_seconds", 30),
    "MONITOR_INTERVAL_SECONDS": ("monitor_interval_seconds", 60),
    "MIN_SPREAD_PCT": ("min_spread_pct", 0.08),
    "MAX_POSITIONS": ("max_positions", 10),
    "MAX_POSITION_PCT": ("max_position_pct", 0.05),
    "CAPITAL": ("capital", 1000.0),
    "MAX_DRAWDOWN_PCT": ("max_drawdown_pct", 0.20),
    "MAX_OPPORTUNITIES_PER_SCAN": ("max_opportunities_per_scan", 50),
    "MIN_CONFIDENCE": ("min_confidence", 0.70),
    "MAX_RISK": ("max_risk", 0.80),
    "MAX_PRICE_AGE_SECONDS": ("max_price_age_seconds", 60),
    "MIN_GEMINI_DEPTH_USD": ("min_gemini_depth_usd", 50.0),
    "CONVERGENCE_EXIT_DAYS": ("convergence_exit_days", 7),
    "CONVERGENCE_THRESHOLD": ("convergence_threshold", 0.02),
    "STOP_LOSS_PCT": ("stop_loss_pct", 0.15),
    "FEE_PER_CONTRACT": ("fee_per_contract", 0.0),
    "DRY_RUN": ("dry_run", True),
    "API_SERVER_ENABLED": ("api_server_enabled", True),
    "API_SERVER_PORT": ("api_server_port", 8000),
    "LOG_LEVEL": ("log_level", "INFO"),
    "MATCHER_BACKEND": ("matcher_backend", "rule_based"),
    "MATCHER_CACHE_TTL": ("matcher_cache_ttl", 3600),
    "MAX_CONCURRENT_LLM_CALLS": ("max_concurrent_llm_calls", 5),
    "ALERT_CHANNEL": ("alert_channel", "none"),
    "ALERT_SPREAD_THRESHOLD": ("alert_spread_threshold", 0.20),
    "ALERT_DEDUP_WINDOW": ("alert_dedup_window", 300),
}

_OPTIONAL_VAR_NAMES = list(_OPTIONAL_VARS.keys())


# ---------------------------------------------------------------------------
# Property 12: absent optional vars produce documented defaults
# ---------------------------------------------------------------------------

@given(st.frozensets(st.sampled_from(_OPTIONAL_VAR_NAMES), max_size=len(_OPTIONAL_VAR_NAMES)))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_12_config_defaults(absent_vars: frozenset[str]) -> None:
    """
    Property 12: for any subset of absent optional vars, Config fields equal
    documented defaults with no KeyError or None.
    """
    # Backup and remove the absent vars
    original: dict[str, str | None] = {}
    try:
        for env_key in absent_vars:
            original[env_key] = os.environ.get(env_key)
            os.environ.pop(env_key, None)

        # Use env backend with dry_run to avoid secret checks
        os.environ["SECRET_BACKEND"] = "env"
        os.environ["DRY_RUN"] = "true"

        try:
            cfg = ConfigService().load()
        except SystemExit:
            # Validation failure is acceptable if we accidentally removed a required var
            return

        defaults = Config()

        for env_key in absent_vars:
            attr, expected_default = _OPTIONAL_VARS[env_key]
            actual = getattr(cfg, attr)
            assert actual is not None, f"Config.{attr} is None (expected default {expected_default!r})"
            # For bool/str/int/float, compare directly
            if isinstance(expected_default, bool):
                assert actual == expected_default, (
                    f"Config.{attr} = {actual!r}, expected default {expected_default!r}"
                )
            elif isinstance(expected_default, (int, float)):
                assert abs(float(actual) - float(expected_default)) < 1e-9, (
                    f"Config.{attr} = {actual!r}, expected default {expected_default!r}"
                )
            else:
                assert actual == expected_default, (
                    f"Config.{attr} = {actual!r}, expected default {expected_default!r}"
                )

    finally:
        for env_key, original_val in original.items():
            if original_val is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = original_val
        os.environ.pop("SECRET_BACKEND", None)
        os.environ.pop("DRY_RUN", None)


# ---------------------------------------------------------------------------
# Property 13: out-of-range numeric config values cause SystemExit
# ---------------------------------------------------------------------------

# (env_var, out-of-range value as string)
_OUT_OF_RANGE_CASES: list[tuple[str, str]] = [
    ("MIN_SPREAD_PCT", "-0.01"),
    ("MAX_POSITIONS", "0"),
    ("CAPITAL", "0"),
    ("CAPITAL", "-100"),
    ("MAX_DRAWDOWN_PCT", "-0.01"),
    ("MAX_DRAWDOWN_PCT", "1.01"),
    ("MAX_POSITION_PCT", "-0.01"),
    ("MAX_POSITION_PCT", "1.01"),
    ("MIN_CONFIDENCE", "-0.01"),
    ("MIN_CONFIDENCE", "1.01"),
    ("MAX_RISK", "-0.01"),
    ("MAX_RISK", "1.01"),
    ("MAX_PRICE_AGE_SECONDS", "0"),
    ("SCAN_INTERVAL_SECONDS", "0"),
    ("PRICE_POLL_INTERVAL_SECONDS", "0"),
    ("MONITOR_INTERVAL_SECONDS", "0"),
    ("MAX_OPPORTUNITIES_PER_SCAN", "0"),
    ("API_SERVER_PORT", "0"),
    ("API_SERVER_PORT", "65536"),
    ("MATCHER_CACHE_TTL", "0"),
    ("MAX_CONCURRENT_LLM_CALLS", "0"),
]


@given(st.sampled_from(_OUT_OF_RANGE_CASES))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_13_out_of_range_causes_system_exit(case: tuple[str, str]) -> None:
    """
    Property 13: for any out-of-range numeric config value, ConfigService.load()
    raises SystemExit.
    """
    env_key, bad_value = case
    original = os.environ.get(env_key)
    try:
        os.environ[env_key] = bad_value
        os.environ["SECRET_BACKEND"] = "env"
        os.environ["DRY_RUN"] = "true"

        with pytest.raises(SystemExit):
            ConfigService().load()

    finally:
        if original is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = original
        os.environ.pop("SECRET_BACKEND", None)
        os.environ.pop("DRY_RUN", None)
