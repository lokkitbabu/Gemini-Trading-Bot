"""
EventMatcher — three-stage pipeline for determining whether two prediction market
events from different platforms resolve on the same underlying outcome.

Stage 1: Rule-based pre-filter  (fast, cheap, eliminates obvious non-matches)
Stage 2: Structured extraction  (parse asset, price level, direction, date)
Stage 3: LLM semantic judgment  (only for ambiguous pairs in [0.40, 0.75) band)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Metrics — imported lazily so module works before metrics.py is initialised
# ---------------------------------------------------------------------------
try:
    from prediction_arb.bot.metrics import (  # type: ignore[import]
        MATCHER_CACHE_HIT_RATE,
        MATCHER_LLM_CALLS_TOTAL,
    )
except ImportError:  # pragma: no cover
    MATCHER_CACHE_HIT_RATE = None  # type: ignore[assignment]
    MATCHER_LLM_CALLS_TOTAL = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSET_MAP: dict[str, str] = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "ether": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
    "bnb": "BNB",
    "binance": "BNB",
    "avalanche": "AVAX",
    "avax": "AVAX",
    "cardano": "ADA",
    "ada": "ADA",
    "dogecoin": "DOGE",
    "doge": "DOGE",
    "litecoin": "LTC",
    "ltc": "LTC",
    "polkadot": "DOT",
    "dot": "DOT",
    "chainlink": "LINK",
    "link": "LINK",
    "polygon": "MATIC",
    "matic": "MATIC",
    "shiba": "SHIB",
    "shib": "SHIB",
}

ABOVE_KEYWORDS: frozenset[str] = frozenset(
    {
        "above",
        "over",
        "exceed",
        "surpass",
        "reach",
        "hit",
        "break",
        "higher",
        "high",
        "top",
        "cross",
        "past",
        "ath",
    }
)

BELOW_KEYWORDS: frozenset[str] = frozenset(
    {
        "below",
        "under",
        "drop",
        "fall",
        "crash",
        "low",
        "dip",
        "beneath",
        "sink",
        "lose",
    }
)

# Rule scoring weights
DIMENSION_WEIGHTS: dict[str, float] = {
    "asset": 0.30,
    "price": 0.35,
    "direction": 0.15,
    "date": 0.20,
}

RULE_REJECT_THRESHOLD = 0.40
RULE_ACCEPT_THRESHOLD = 0.75

# Price plausibility filter
_PRICE_MIN = 100.0
_PRICE_MAX = 10_000_000.0

# Max concurrent LLM calls (can be overridden by config)
MAX_CONCURRENT_LLM_CALLS = 5

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MarketEvent:
    id: str
    title: str
    platform: str  # "kalshi" | "polymarket" | "gemini"
    yes_price: float | None = None
    end_date: str | None = None
    expiry_date: str | None = None
    resolution_date: str | None = None
    close_time: str | None = None
    endDateIso: str | None = None
    extra: dict = field(default_factory=dict)

    # Cached extraction results (populated lazily)
    _extracted_asset: str | None = field(default=None, repr=False, compare=False)
    _extracted_price: float | None = field(default=None, repr=False, compare=False)
    _extracted_direction: str | None = field(default=None, repr=False, compare=False)
    _extracted_date: date | None = field(default=None, repr=False, compare=False)


@dataclass
class MatchResult:
    equivalent: bool
    confidence: float  # 0.0–1.0
    reasoning: str
    asset: str | None = None
    price_level: float | None = None
    direction: str | None = None  # "above" | "below" | None
    resolution_date: str | None = None
    inverted: bool = False
    backend: str = "rule_based"  # "rule_based" | "openai" | "anthropic"


@dataclass
class MatchedPair:
    ref: MarketEvent  # Kalshi or Polymarket event
    target: MarketEvent  # Gemini event
    result: MatchResult


@dataclass
class CacheEntry:
    result: MatchResult
    expires_at: datetime


# ---------------------------------------------------------------------------
# LLM Tool Schemas
# ---------------------------------------------------------------------------

MATCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "match_event_pair",
        "description": (
            "Determine whether two prediction market events from different platforms "
            "resolve on the same underlying outcome. Focus on asset, price threshold, "
            "direction (above/below), and resolution date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "equivalent": {
                    "type": "boolean",
                    "description": "True if both events resolve on the same outcome",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score 0.0–1.0",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One-sentence explanation of the decision",
                },
                "asset": {
                    "type": "string",
                    "description": "Canonical asset symbol (BTC, ETH, SOL, etc.) or null",
                },
                "price_level": {
                    "type": ["number", "null"],
                    "description": "Extracted price threshold in USD, or null",
                },
                "direction": {
                    "type": "string",
                    "enum": ["above", "below", "null"],
                    "description": "Price direction: above, below, or null if unclear",
                },
                "resolution_date": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 date (YYYY-MM-DD) or null",
                },
                "inverted": {
                    "type": "boolean",
                    "description": (
                        "True if one platform phrases the event as YES=above "
                        "and the other as YES=below (logical complement)"
                    ),
                },
            },
            "required": ["equivalent", "confidence", "reasoning", "inverted"],
        },
    },
}

ANTHROPIC_MATCH_TOOL: dict[str, Any] = {
    "name": "match_event_pair",
    "description": MATCH_TOOL_SCHEMA["function"]["description"],
    "input_schema": MATCH_TOOL_SCHEMA["function"]["parameters"],
}

EXTRACTION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "extract_asset",
            "description": "Extract the canonical crypto asset symbol from an event title",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_price_level",
            "description": "Extract the price threshold in USD from an event title",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_direction",
            "description": "Extract the price direction (above/below) from an event title",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
    },
]

# Anthropic-format extraction tools
_ANTHROPIC_EXTRACTION_TOOLS: list[dict[str, Any]] = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in EXTRACTION_TOOLS
]


# ---------------------------------------------------------------------------
# Extraction utilities (Stage 2)
# ---------------------------------------------------------------------------


def extract_asset(title: str) -> str | None:
    """
    Extract canonical crypto asset symbol from an event title.
    Uses word-boundary enforcement to avoid false matches (e.g. "resolution" ≠ "sol").
    Returns canonical symbol (e.g. "BTC") or None.
    """
    title_lower = title.lower()
    # Sort by length descending so longer keys (e.g. "bitcoin") match before shorter ("btc")
    for keyword, symbol in sorted(ASSET_MAP.items(), key=lambda x: -len(x[0])):
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, title_lower):
            return symbol
    return None


def extract_price_level(title: str) -> float | None:
    """
    Extract price threshold in USD from an event title.
    Handles formats: $95,000  $95k  $95K  95000  95,000  95k  0.95
    Applies plausibility filter: 100 ≤ price ≤ 10,000,000.
    Returns float in USD or None.
    """
    # Pattern 1: $95,000 or $95k or $95K (with optional dollar sign)
    # Pattern 2: plain numbers like 95000 or 95,000 or 95k
    # Pattern 3: decimal like 0.95 (only if preceded by $ to avoid matching percentages)

    # (pattern, is_k_suffix)
    patterns: list[tuple[str, bool]] = [
        # $95,000 or $95,000.50
        (r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)", False),
        # $95k or $95K or $95.5k
        (r"\$\s*(\d+(?:\.\d+)?)\s*[kK]\b", True),
        # $95 or $9500 (plain dollar amount, no comma)
        (r"\$\s*(\d+(?:\.\d+)?)\b", False),
        # 95,000 or 95,000.50 (comma-separated, no dollar sign)
        (r"\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\b", False),
        # 95k or 95K (no dollar sign)
        (r"\b(\d+(?:\.\d+)?)\s*[kK]\b", True),
        # plain integer >= 4 digits (e.g. 95000) — must be word-boundary
        (r"\b(\d{4,})\b", False),
    ]

    candidates: list[float] = []

    for pattern, is_k in patterns:
        for match in re.finditer(pattern, title):
            raw = match.group(1).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            if is_k:
                value *= 1000.0
            candidates.append(value)

    # Filter by plausibility and return the first valid candidate
    for value in candidates:
        if _PRICE_MIN <= value <= _PRICE_MAX:
            return value

    return None


def extract_direction(title: str) -> str | None:
    """
    Extract price direction from an event title.
    Returns "above", "below", or None.
    Checks ±2-word context for inversions like "reach a low".
    """
    words = re.findall(r"\b\w+\b", title.lower())

    for i, word in enumerate(words):
        if word in ABOVE_KEYWORDS:
            # Check ±2-word context for inversion phrases like "reach a low"
            context_start = max(0, i - 2)
            context_end = min(len(words), i + 3)
            context = words[context_start:context_end]
            # "reach a low" or "hit a low" → below
            if word in ("reach", "hit") and any(w in BELOW_KEYWORDS for w in context):
                return "below"
            return "above"
        if word in BELOW_KEYWORDS:
            return "below"

    return None


def extract_date(event: MarketEvent) -> date | None:
    """
    Extract resolution date from a MarketEvent.
    Field priority: end_date → expiry_date → resolution_date → close_time → endDateIso
    Falls back to title-pattern parsing.
    Normalises to UTC date (time component stripped).
    """
    # Try structured fields first
    for field_val in (
        event.end_date,
        event.expiry_date,
        event.resolution_date,
        event.close_time,
        event.endDateIso,
    ):
        if field_val:
            parsed = _parse_date_string(field_val)
            if parsed is not None:
                return parsed

    # Fall back to title parsing
    return _extract_date_from_title(event.title)


def _parse_date_string(s: str) -> date | None:
    """Parse a date/datetime string to a UTC date."""
    s = s.strip()
    # Try ISO 8601 datetime first
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return dt.date()
        except ValueError:
            continue

    # Try Unix timestamp
    try:
        ts = float(s)
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except (ValueError, OSError):
        pass

    return None


_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _extract_date_from_title(title: str) -> date | None:
    """Parse date patterns from event title text."""
    import calendar

    title_lower = title.lower()
    current_year = datetime.now(tz=timezone.utc).year

    # "March 31" or "Mar 31" or "31 March"
    for month_name, month_num in _MONTH_MAP.items():
        # "March 31, 2025" or "March 31 2025"
        m = re.search(
            rf"\b{month_name}\s+(\d{{1,2}})(?:[,\s]+(\d{{4}}))?\b",
            title_lower,
        )
        if m:
            day = int(m.group(1))
            year = int(m.group(2)) if m.group(2) else current_year
            try:
                return date(year, month_num, day)
            except ValueError:
                pass

        # "31 March 2025" or "31 March"
        m = re.search(
            rf"\b(\d{{1,2}})\s+{month_name}(?:[,\s]+(\d{{4}}))?\b",
            title_lower,
        )
        if m:
            day = int(m.group(1))
            year = int(m.group(2)) if m.group(2) else current_year
            try:
                return date(year, month_num, day)
            except ValueError:
                pass

        # "end of March" → last day of that month
        m = re.search(rf"\bend\s+of\s+{month_name}\b", title_lower)
        if m:
            last_day = calendar.monthrange(current_year, month_num)[1]
            return date(current_year, month_num, last_day)

    # "3/31" or "3/31/2025"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", title)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else current_year
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # "Q1 2026" or "Q3 2025"
    m = re.search(r"\bQ([1-4])\s+(\d{4})\b", title, re.IGNORECASE)
    if m:
        quarter = int(m.group(1))
        year = int(m.group(2))
        month, day = _QUARTER_END[quarter]
        return date(year, month, day)

    return None


# ---------------------------------------------------------------------------
# Rule-based pre-filter (Stage 1)
# ---------------------------------------------------------------------------


def _rule_score(ref: MarketEvent, target: MarketEvent) -> float:
    """
    Compute weighted rule-based score for a (ref, target) event pair.
    Returns a float in [0.0, 1.0].
    """
    asset_a = ref._extracted_asset
    asset_b = target._extracted_asset
    price_a = ref._extracted_price
    price_b = target._extracted_price
    dir_a = ref._extracted_direction
    dir_b = target._extracted_direction
    date_a = ref._extracted_date
    date_b = target._extracted_date

    # Asset dimension
    if asset_a is not None and asset_b is not None:
        asset_score = 1.0 if asset_a == asset_b else 0.0
    else:
        asset_score = 0.5

    # Price dimension
    if price_a is not None and price_b is not None:
        pct_diff = abs(price_a - price_b) / max(price_a, price_b)
        price_score = 1.0 if pct_diff <= 0.01 else 0.0
    else:
        price_score = 0.5

    # Direction dimension
    if dir_a is not None and dir_b is not None:
        direction_score = 1.0 if dir_a == dir_b else 0.0
    else:
        direction_score = 0.5

    # Date dimension
    if date_a is not None and date_b is not None:
        delta_days = abs((date_a - date_b).days)
        date_score = 1.0 if delta_days <= 3 else 0.0
    else:
        date_score = 0.5

    score = (
        DIMENSION_WEIGHTS["asset"] * asset_score
        + DIMENSION_WEIGHTS["price"] * price_score
        + DIMENSION_WEIGHTS["direction"] * direction_score
        + DIMENSION_WEIGHTS["date"] * date_score
    )
    return score


def _assets_compatible(ref: MarketEvent, target: MarketEvent) -> bool:
    """
    Asset pre-filter: return False if both events have detected assets that differ.
    If either asset is None, return True (cannot rule out a match).
    """
    a = ref._extracted_asset
    b = target._extracted_asset
    if a is not None and b is not None and a != b:
        return False
    return True


def _populate_extractions(event: MarketEvent) -> None:
    """Populate cached extraction fields on a MarketEvent (idempotent)."""
    if event._extracted_asset is None:
        event._extracted_asset = extract_asset(event.title)
    if event._extracted_price is None:
        event._extracted_price = extract_price_level(event.title)
    if event._extracted_direction is None:
        event._extracted_direction = extract_direction(event.title)
    if event._extracted_date is None:
        event._extracted_date = extract_date(event)


# ---------------------------------------------------------------------------
# MatchingToolRegistry (Stage 3 helpers)
# ---------------------------------------------------------------------------


class MatchingToolRegistry:
    """Maps tool names to extraction functions for LLM tool-use dispatch."""

    _registry: dict[str, Any] = {
        "extract_asset": lambda args: extract_asset(args["title"]),
        "extract_price_level": lambda args: extract_price_level(args["title"]),
        "extract_direction": lambda args: extract_direction(args["title"]),
    }

    @classmethod
    def execute(cls, name: str, args: dict[str, Any]) -> Any:
        """Dispatch a tool call by name. Returns the tool result."""
        fn = cls._registry.get(name)
        if fn is None:
            raise ValueError(f"Unknown extraction tool: {name!r}")
        return fn(args)


def _execute_extraction_tool(name: str, args: dict[str, Any]) -> Any:
    """Dispatch an extraction tool call by name."""
    return MatchingToolRegistry.execute(name, args)


# ---------------------------------------------------------------------------
# LLM result parsing
# ---------------------------------------------------------------------------


@dataclass
class LLMMatchResult:
    equivalent: bool
    confidence: float
    reasoning: str
    asset: str | None
    price_level: float | None
    direction: str | None
    resolution_date: str | None
    inverted: bool


def _parse_match_result(args: dict[str, Any]) -> LLMMatchResult:
    """
    Parse and validate the structured output from the match_event_pair tool call.
    Clamps confidence to [0.0, 1.0] and normalises direction.
    """
    direction_raw = args.get("direction")
    direction = direction_raw if direction_raw not in (None, "null", "") else None

    return LLMMatchResult(
        equivalent=bool(args["equivalent"]),
        confidence=max(0.0, min(1.0, float(args["confidence"]))),
        reasoning=args.get("reasoning", ""),
        asset=args.get("asset") or None,
        price_level=args.get("price_level"),
        direction=direction,
        resolution_date=args.get("resolution_date") or None,
        inverted=bool(args.get("inverted", False)),
    )


# ---------------------------------------------------------------------------
# LLM backends (Stage 3)
# ---------------------------------------------------------------------------


def _build_user_message(
    event_a: MarketEvent,
    event_b: MarketEvent,
    rule_score: float,
    ob_ctx: dict[str, Any] | None,
) -> dict[str, str]:
    """Build the user message for the LLM, injecting live orderbook context when available."""
    price_context = ""
    if ob_ctx:
        ref_mid = ob_ctx.get("ref_mid")
        gemini_mid = ob_ctx.get("gemini_mid")
        spread = ob_ctx.get("spread")
        parts = ["\nLive orderbook context:"]
        if ref_mid is not None:
            parts.append(f"\n  {event_a.platform} yes_mid: {ref_mid:.3f}")
        if gemini_mid is not None:
            parts.append(f"\n  {event_b.platform} yes_mid: {gemini_mid:.3f}")
        if spread is not None:
            parts.append(f"\n  Price spread: {spread:.4f}")
        parts.append(
            "\n  (Converging prices suggest the same event; diverging prices may indicate "
            "different resolution dates or inverted framing)"
        )
        price_context = "".join(parts)

    content = (
        f"Event A ({event_a.platform}): \"{event_a.title}\"\n"
        f"Resolution date A: {event_a.end_date or 'unknown'}\n\n"
        f"Event B ({event_b.platform}): \"{event_b.title}\"\n"
        f"Resolution date B: {event_b.end_date or 'unknown'}\n\n"
        f"Rule-based pre-score: {rule_score:.2f} (ambiguous — needs semantic judgment)\n"
        f"Pre-extracted: asset_a={event_a._extracted_asset}, "
        f"price_a={event_a._extracted_price}, direction_a={event_a._extracted_direction}\n"
        f"             asset_b={event_b._extracted_asset}, "
        f"price_b={event_b._extracted_price}, direction_b={event_b._extracted_direction}"
        f"{price_context}\n\n"
        f"Call match_event_pair with your determination. "
        f"If a title is ambiguous, call extract_asset, extract_price_level, or "
        f"extract_direction first."
    )
    return {"role": "user", "content": content}


async def _call_openai_with_tools(
    client: Any,
    messages: list[dict],
    tools: list[dict],
    rule_score: float,
) -> LLMMatchResult:
    """
    Multi-turn OpenAI tool-use loop (max 3 turns) with asyncio.timeout(10.0).
    Falls back to rule-based result on any error.
    """
    import openai

    async with asyncio.timeout(10.0):
        for turn in range(3):
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                raise ValueError("LLM returned no tool call")

            tool_call = tool_calls[0]
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)

            log.debug(
                "llm_tool_invocation",
                backend="openai",
                tool_name=tool_name,
                args=tool_args,
                turn=turn,
            )

            if tool_name == "match_event_pair":
                result = _parse_match_result(tool_args)
                log.debug(
                    "llm_tool_result",
                    backend="openai",
                    tool_name=tool_name,
                    result=str(result),
                )
                return result

            # Extraction tool — execute and feed result back
            tool_result = _execute_extraction_tool(tool_name, tool_args)
            log.debug(
                "llm_tool_result",
                backend="openai",
                tool_name=tool_name,
                result=tool_result,
            )
            messages = messages + [
                {"role": "assistant", "tool_calls": [tool_call]},
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result),
                },
            ]

    raise TimeoutError("OpenAI tool loop exceeded 10s budget")


async def _call_anthropic_with_tools(
    client: Any,
    messages: list[dict],
    tools: list[dict],
    rule_score: float,
) -> LLMMatchResult:
    """
    Multi-turn Anthropic tool-use loop (max 3 turns) with asyncio.timeout(10.0).
    Falls back to rule-based result on any error.
    """
    async with asyncio.timeout(10.0):
        for turn in range(3):
            response = await client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=512,
                messages=messages,
                tools=tools,
            )

            # Find tool_use block
            tool_use_block = next(
                (b for b in response.content if b.type == "tool_use"), None
            )
            if tool_use_block is None:
                raise ValueError("Anthropic returned no tool_use block")

            tool_name = tool_use_block.name
            tool_args = tool_use_block.input

            log.debug(
                "llm_tool_invocation",
                backend="anthropic",
                tool_name=tool_name,
                args=tool_args,
                turn=turn,
            )

            if tool_name == "match_event_pair":
                result = _parse_match_result(tool_args)
                log.debug(
                    "llm_tool_result",
                    backend="anthropic",
                    tool_name=tool_name,
                    result=str(result),
                )
                return result

            # Extraction tool
            tool_result = _execute_extraction_tool(tool_name, tool_args)
            log.debug(
                "llm_tool_result",
                backend="anthropic",
                tool_name=tool_name,
                result=tool_result,
            )
            messages = messages + [
                {
                    "role": "assistant",
                    "content": response.content,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_block.id,
                            "content": json.dumps(tool_result),
                        }
                    ],
                },
            ]

    raise TimeoutError("Anthropic tool loop exceeded 10s budget")


# ---------------------------------------------------------------------------
# StateStore stub interface (wired in Group 7)
# ---------------------------------------------------------------------------


class _StateStoreStub:
    """Stub StateStore interface for cache persistence. Wired in Group 7."""

    async def load_match_cache(self) -> list[dict[str, Any]]:
        """Load non-expired match cache rows from DB. Returns list of row dicts."""
        return []

    async def save_match_cache_entry(
        self,
        key: str,
        result: MatchResult,
        expires_at: datetime,
    ) -> None:
        """Persist a match cache entry to DB asynchronously."""
        pass

    async def prune_expired_match_cache(self) -> int:
        """Remove expired rows from match_cache table. Returns count removed."""
        return 0


# ---------------------------------------------------------------------------
# EventMatcher
# ---------------------------------------------------------------------------


class EventMatcher:
    """
    Three-stage event matching pipeline.

    Stage 1: Rule-based pre-filter
    Stage 2: Structured extraction (asset, price, direction, date)
    Stage 3: LLM semantic judgment (only for ambiguous pairs)
    """

    def __init__(
        self,
        backend: str = "rule_based",
        cache_ttl_seconds: int = 3600,
        max_concurrent_llm_calls: int = MAX_CONCURRENT_LLM_CALLS,
        openai_api_key: str = "",
        anthropic_api_key: str = "",
        state_store: Any = None,
    ) -> None:
        self._backend = backend
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, CacheEntry] = {}
        self._state_store = state_store or _StateStoreStub()
        self._sem = asyncio.Semaphore(max_concurrent_llm_calls)

        # Observability counters
        self._cache_hits = 0
        self._cache_misses = 0
        self._llm_call_count = 0
        self._last_batch_duration_ms = 0.0

        # Background DB prune scheduling
        self._last_db_prune: float = 0.0
        self._db_prune_interval = 3600.0

        # LLM clients (lazy init)
        self._openai_client: Any = None
        self._anthropic_client: Any = None
        self._openai_api_key = openai_api_key
        self._anthropic_api_key = anthropic_api_key

    # ------------------------------------------------------------------
    # LLM client initialisation
    # ------------------------------------------------------------------

    def _get_openai_client(self) -> Any:
        if self._openai_client is None:
            import openai
            self._openai_client = openai.AsyncOpenAI(api_key=self._openai_api_key or None)
        return self._openai_client

    def _get_anthropic_client(self) -> Any:
        if self._anthropic_client is None:
            import anthropic
            self._anthropic_client = anthropic.AsyncAnthropic(
                api_key=self._anthropic_api_key or None
            )
        return self._anthropic_client

    # ------------------------------------------------------------------
    # Cache key
    # ------------------------------------------------------------------

    def _cache_key(self, ref: MarketEvent, target: MarketEvent) -> str:
        """
        SHA-256 of sorted [f"{title_a}|{date_a}", f"{title_b}|{date_b}"].
        Order-independent: cache_key(a, b) == cache_key(b, a).
        """
        date_a = str(ref._extracted_date or "")
        date_b = str(target._extracted_date or "")
        parts = sorted([f"{ref.title}|{date_a}", f"{target.title}|{date_b}"])
        payload = "\n".join(parts).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def prune_expired(self) -> int:
        """Remove expired in-memory entries. Returns count removed."""
        now = datetime.now(tz=timezone.utc)
        expired_keys = [k for k, v in self._cache.items() if v.expires_at <= now]
        for k in expired_keys:
            del self._cache[k]
        return len(expired_keys)

    async def warm_cache_from_db(self) -> int:
        """Load non-expired rows from match_cache table on startup. Returns entries loaded."""
        rows = await self._state_store.load_match_cache()
        now = datetime.now(tz=timezone.utc)
        loaded = 0
        for row in rows:
            expires_at = row.get("expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at is None or expires_at <= now:
                continue
            result = MatchResult(
                equivalent=row["equivalent"],
                confidence=float(row["confidence"]),
                reasoning=row.get("reasoning", ""),
                asset=row.get("asset"),
                price_level=row.get("price_level"),
                direction=row.get("direction"),
                resolution_date=row.get("resolution_date"),
                inverted=bool(row.get("inverted", False)),
                backend=row.get("backend", "rule_based"),
            )
            self._cache[row["cache_key"]] = CacheEntry(result=result, expires_at=expires_at)
            loaded += 1
        log.info("matcher_cache_warmed", entries_loaded=loaded)
        return loaded

    async def persist_result(self, key: str, result: MatchResult) -> None:
        """Write a match result to the match_cache table asynchronously."""
        expires_at = datetime.now(tz=timezone.utc).replace(
            microsecond=0
        )
        from datetime import timedelta
        expires_at = expires_at + timedelta(seconds=self._cache_ttl)
        try:
            await self._state_store.save_match_cache_entry(key, result, expires_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("matcher_persist_failed", key=key, error=str(exc))

    async def _maybe_prune_db(self) -> None:
        """Prune expired DB rows every 3600s (background)."""
        now = time.monotonic()
        if now - self._last_db_prune >= self._db_prune_interval:
            self._last_db_prune = now
            try:
                removed = await self._state_store.prune_expired_match_cache()
                if removed:
                    log.debug("matcher_db_pruned", removed=removed)
            except Exception as exc:  # noqa: BLE001
                log.warning("matcher_db_prune_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Core scoring
    # ------------------------------------------------------------------

    async def _score_pair(
        self,
        ref: MarketEvent,
        target: MarketEvent,
        ob_ctx: dict[str, Any] | None = None,
    ) -> MatchResult:
        """
        Score a single (ref, target) pair through the three-stage pipeline.
        Returns a MatchResult.
        """
        _populate_extractions(ref)
        _populate_extractions(target)

        rule_score = _rule_score(ref, target)

        # Stage 1 routing
        if rule_score < RULE_REJECT_THRESHOLD:
            return MatchResult(
                equivalent=False,
                confidence=rule_score,
                reasoning=f"Rule score {rule_score:.2f} below reject threshold",
                asset=ref._extracted_asset or target._extracted_asset,
                price_level=ref._extracted_price,
                direction=ref._extracted_direction,
                backend="rule_based",
            )

        if rule_score >= RULE_ACCEPT_THRESHOLD:
            return MatchResult(
                equivalent=True,
                confidence=rule_score,
                reasoning=f"Rule score {rule_score:.2f} above accept threshold",
                asset=ref._extracted_asset or target._extracted_asset,
                price_level=ref._extracted_price,
                direction=ref._extracted_direction,
                backend="rule_based",
            )

        # Stage 3: LLM for ambiguous band [0.40, 0.75)
        if self._backend == "rule_based":
            # No LLM configured — return mid-confidence rule result
            equivalent = rule_score >= 0.575  # midpoint of ambiguous band
            return MatchResult(
                equivalent=equivalent,
                confidence=rule_score,
                reasoning=f"Rule-based only (backend=rule_based), score={rule_score:.2f}",
                asset=ref._extracted_asset or target._extracted_asset,
                price_level=ref._extracted_price,
                direction=ref._extracted_direction,
                backend="rule_based",
            )

        return await self._call_llm(ref, target, rule_score, ob_ctx)

    async def _call_llm(
        self,
        ref: MarketEvent,
        target: MarketEvent,
        rule_score: float,
        ob_ctx: dict[str, Any] | None,
    ) -> MatchResult:
        """Call the configured LLM backend. Falls back to rule-based on any error."""
        backend = self._backend
        messages = [_build_user_message(ref, target, rule_score, ob_ctx)]

        try:
            if backend == "openai":
                all_tools = EXTRACTION_TOOLS + [MATCH_TOOL_SCHEMA]
                client = self._get_openai_client()
                llm_result = await _call_openai_with_tools(
                    client, messages, all_tools, rule_score
                )
            elif backend == "anthropic":
                all_tools = _ANTHROPIC_EXTRACTION_TOOLS + [ANTHROPIC_MATCH_TOOL]
                client = self._get_anthropic_client()
                llm_result = await _call_anthropic_with_tools(
                    client, messages, all_tools, rule_score
                )
            else:
                raise ValueError(f"Unknown backend: {backend!r}")

            self._llm_call_count += 1
            _emit_llm_counter(backend, "success")

            return MatchResult(
                equivalent=llm_result.equivalent,
                confidence=llm_result.confidence,
                reasoning=llm_result.reasoning,
                asset=llm_result.asset,
                price_level=llm_result.price_level,
                direction=llm_result.direction,
                resolution_date=llm_result.resolution_date,
                inverted=llm_result.inverted,
                backend=backend,
            )

        except (TimeoutError, asyncio.TimeoutError) as exc:
            log.warning(
                "llm_timeout",
                backend=backend,
                rule_score=rule_score,
                error=str(exc),
            )
            _emit_llm_counter(backend, "timeout")
        except json.JSONDecodeError as exc:
            log.warning(
                "llm_json_error",
                backend=backend,
                rule_score=rule_score,
                error=str(exc),
            )
            _emit_llm_counter(backend, "json_error")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "llm_error",
                backend=backend,
                rule_score=rule_score,
                error=str(exc),
            )
            _emit_llm_counter(backend, "error")

        # Fallback to rule-based result
        equivalent = rule_score >= 0.575
        return MatchResult(
            equivalent=equivalent,
            confidence=rule_score,
            reasoning=f"LLM fallback (backend={backend}), rule_score={rule_score:.2f}",
            asset=ref._extracted_asset or target._extracted_asset,
            price_level=ref._extracted_price,
            direction=ref._extracted_direction,
            backend="rule_based",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def match(
        self,
        ref: MarketEvent,
        target: MarketEvent,
        ob_ctx: dict[str, Any] | None = None,
    ) -> MatchResult:
        """Single-pair entry point."""
        _populate_extractions(ref)
        _populate_extractions(target)

        key = self._cache_key(ref, target)
        now = datetime.now(tz=timezone.utc)

        cached = self._cache.get(key)
        if cached and cached.expires_at > now:
            self._cache_hits += 1
            return cached.result

        self._cache_misses += 1
        result = await self._score_pair(ref, target, ob_ctx)

        from datetime import timedelta
        expires_at = now + timedelta(seconds=self._cache_ttl)
        self._cache[key] = CacheEntry(result=result, expires_at=expires_at)
        asyncio.ensure_future(self.persist_result(key, result))

        return result

    async def batch_match(
        self,
        refs: list[MarketEvent],
        targets: list[MarketEvent],
        min_confidence: float = 0.70,
        ob_ctx_map: dict[str, dict[str, Any]] | None = None,
    ) -> list[MatchedPair]:
        """
        Match all (ref, target) pairs with cache-first lookup and asset pre-filter.
        LLM calls are gathered concurrently, capped at MAX_CONCURRENT_LLM_CALLS.
        Returns only pairs with result.confidence >= min_confidence.
        """
        start = time.monotonic()
        now = datetime.now(tz=timezone.utc)

        # Lazy prune in-memory cache
        self.prune_expired()

        # Background DB prune
        asyncio.ensure_future(self._maybe_prune_db())

        # Populate extractions for all events upfront
        for event in refs + targets:
            _populate_extractions(event)

        matched: list[MatchedPair] = []
        pairs_to_score: list[tuple[MarketEvent, MarketEvent]] = []

        for ref in refs:
            for target in targets:
                # Asset pre-filter
                if not _assets_compatible(ref, target):
                    continue

                key = self._cache_key(ref, target)
                cached = self._cache.get(key)
                if cached and cached.expires_at > now:
                    self._cache_hits += 1
                    if cached.result.confidence >= min_confidence:
                        matched.append(MatchedPair(ref=ref, target=target, result=cached.result))
                    continue

                self._cache_misses += 1
                pairs_to_score.append((ref, target))

        # Score uncached pairs concurrently
        async def _score_with_sem(ref: MarketEvent, target: MarketEvent) -> MatchResult:
            ob_ctx = None
            if ob_ctx_map:
                ob_ctx = ob_ctx_map.get(f"{ref.id}|{target.id}")
            async with self._sem:
                return await self._score_pair(ref, target, ob_ctx)

        if pairs_to_score:
            tasks = [_score_with_sem(r, t) for r, t in pairs_to_score]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            from datetime import timedelta
            for (ref, target), result in zip(pairs_to_score, results):
                if isinstance(result, BaseException):
                    log.warning(
                        "batch_match_pair_error",
                        ref_id=ref.id,
                        target_id=target.id,
                        error=str(result),
                    )
                    continue

                key = self._cache_key(ref, target)
                expires_at = now + timedelta(seconds=self._cache_ttl)
                self._cache[key] = CacheEntry(result=result, expires_at=expires_at)
                asyncio.ensure_future(self.persist_result(key, result))

                if result.confidence >= min_confidence:
                    matched.append(MatchedPair(ref=ref, target=target, result=result))

        self._last_batch_duration_ms = (time.monotonic() - start) * 1000.0

        # Emit cache hit rate metric
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total if total > 0 else 0.0
        if MATCHER_CACHE_HIT_RATE is not None:
            try:
                MATCHER_CACHE_HIT_RATE.set(hit_rate)
            except Exception:  # noqa: BLE001
                pass

        log.debug(
            "batch_match_complete",
            pairs_scored=len(pairs_to_score),
            matched=len(matched),
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
            duration_ms=self._last_batch_duration_ms,
        )

        return matched

    # ------------------------------------------------------------------
    # Observability properties
    # ------------------------------------------------------------------

    @property
    def cache_hit_rate(self) -> float:
        total = self._cache_hits + self._cache_misses
        return self._cache_hits / total if total > 0 else 0.0

    @property
    def llm_call_count(self) -> int:
        return self._llm_call_count

    @property
    def last_batch_duration_ms(self) -> float:
        return self._last_batch_duration_ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_llm_counter(backend: str, outcome: str) -> None:
    """Emit arb_matcher_llm_calls_total counter."""
    if MATCHER_LLM_CALLS_TOTAL is not None:
        try:
            MATCHER_LLM_CALLS_TOTAL.labels(backend=backend, outcome=outcome).inc()
        except Exception:  # noqa: BLE001
            pass
