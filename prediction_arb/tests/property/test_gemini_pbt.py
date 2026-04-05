# Feature: prediction-arbitrage-production
# Property 11: GeminiClient-produced X-GEMINI-SIGNATURE equals independently computed HMAC-SHA384

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from prediction_arb.bot.clients.gemini import GeminiClient


def _make_gemini_client(api_key: str, api_secret: str) -> GeminiClient:
    """Construct a GeminiClient with the given credentials (no real HTTP)."""
    client = GeminiClient.__new__(GeminiClient)
    client._api_key = api_key
    client._api_secret = api_secret
    client._base_url = "https://api.gemini.com"
    client._price_poll_interval = 30
    client._subscribed_symbols = []
    client._ws = None
    client._consecutive_failures = 0
    client._session = None
    return client


# ---------------------------------------------------------------------------
# Property 11: HMAC-SHA384 signature correctness
# ---------------------------------------------------------------------------

@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"))),
        st.text(min_size=0, max_size=50),
        min_size=0,
        max_size=10,
    ),
    st.text(min_size=8, max_size=64, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"))),
    st.text(min_size=8, max_size=64, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"))),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_11_gemini_hmac_signature(
    payload: dict,
    api_key: str,
    api_secret: str,
) -> None:
    """
    Property 11: for any payload dict, the X-GEMINI-SIGNATURE produced by
    GeminiClient._signed_headers() equals independently computed HMAC-SHA384
    of the base64-encoded payload using the configured secret.
    """
    client = _make_gemini_client(api_key, api_secret)

    # Get the headers produced by the client
    headers = client._signed_headers(payload)

    # Independently compute the expected signature
    payload_json = json.dumps(payload)
    payload_b64 = base64.b64encode(payload_json.encode()).decode()
    expected_signature = hmac.new(
        api_secret.encode(),
        payload_b64.encode(),
        hashlib.sha384,
    ).hexdigest()

    # The payload_b64 in the header must match what we computed
    assert headers["X-GEMINI-PAYLOAD"] == payload_b64, (
        "X-GEMINI-PAYLOAD does not match independently computed base64 payload"
    )

    # The signature must match
    assert headers["X-GEMINI-SIGNATURE"] == expected_signature, (
        f"X-GEMINI-SIGNATURE mismatch: "
        f"got {headers['X-GEMINI-SIGNATURE']!r}, "
        f"expected {expected_signature!r}"
    )

    # API key must be present
    assert headers["X-GEMINI-APIKEY"] == api_key, (
        "X-GEMINI-APIKEY does not match configured api_key"
    )
