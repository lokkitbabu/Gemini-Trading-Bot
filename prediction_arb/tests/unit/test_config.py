"""
Unit tests for ConfigService (Task 14.1).
"""

import os
import pytest
from unittest.mock import patch

from prediction_arb.bot.config import ConfigService, Config


def test_missing_required_secret_exits(monkeypatch):
    """SystemExit on missing required secret in live mode."""
    monkeypatch.setenv("SECRET_BACKEND", "env")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_SECRET", raising=False)
    monkeypatch.delenv("API_SERVER_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        ConfigService().load()


def test_defaults_applied():
    """All optional fields get documented defaults."""
    env = {"SECRET_BACKEND": "env", "DRY_RUN": "true"}
    with patch.dict(os.environ, env, clear=True):
        cfg = ConfigService().load()
    defaults = Config()
    assert cfg.scan_interval_seconds == defaults.scan_interval_seconds
    assert cfg.min_spread_pct == defaults.min_spread_pct
    assert cfg.max_positions == defaults.max_positions


def test_out_of_range_exits(monkeypatch):
    """Out-of-range numeric config exits with non-zero code."""
    monkeypatch.setenv("SECRET_BACKEND", "env")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MIN_SPREAD_PCT", "-0.5")
    with pytest.raises(SystemExit):
        ConfigService().load()


def test_no_secret_in_logs(monkeypatch, caplog):
    """No secret value appears in any log output during load()."""
    import logging
    monkeypatch.setenv("SECRET_BACKEND", "env")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key-12345")
    with caplog.at_level(logging.DEBUG):
        ConfigService().load()
    for record in caplog.records:
        assert "super-secret-key-12345" not in record.getMessage()
