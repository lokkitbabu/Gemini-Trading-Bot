test ens# Tasks: Prediction Arbitrage Production System

## Task Groups

- [Group 1: Project Scaffold & Configuration](#group-1-project-scaffold--configuration)
- [Group 2: Platform Clients](#group-2-platform-clients)
- [Group 3: Event Matcher](#group-3-event-matcher)
- [Group 4: Data Collection (PricePoller + OrderbookCache)](#group-4-data-collection-pricepoller--orderbookcache)
- [Group 5: Arbitrage Engine & Risk Manager](#group-5-arbitrage-engine--risk-manager)
- [Group 6: Executor & Position Monitor](#group-6-executor--position-monitor)
- [Group 7: State Store & Database](#group-7-state-store--database)
- [Group 8: Observability (Metrics, Logging, Alerts)](#group-8-observability-metrics-logging-alerts)
- [Group 9: API Server & SSE](#group-9-api-server--sse)
- [Group 10: Scanner & Main Loop](#group-10-scanner--main-loop)
- [Group 11: Backtesting Mode](#group-11-backtesting-mode)
- [Group 12: Dashboard (Next.js 14)](#group-12-dashboard-nextjs-14)
- [Group 13: Infrastructure & Deployment](#group-13-infrastructure--deployment)
- [Group 14: Unit Tests](#group-14-unit-tests)
- [Group 15: Property-Based Tests](#group-15-property-based-tests)

---

## Group 1: Project Scaffold & Configuration

### 1.1 Initialise Python package layout
- [x] Create `prediction_arb/bot/` package with `__init__.py` files for all sub-packages (`api/`, `clients/`)
- [x] Create `prediction_arb/migrations/`, `prediction_arb/tests/unit/`, `prediction_arb/tests/integration/`, `prediction_arb/tests/property/` directories with `__init__.py` files
- [x] Create `alembic.ini` pointing at `prediction_arb/migrations/`
- [x] Create `prediction_arb/.env.template` documenting every env variable with its default and whether it is a secret
- [x] Create `pyproject.toml` (or `requirements.txt`) listing all runtime dependencies: `fastapi`, `uvicorn[standard]`, `httpx`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `structlog`, `prometheus-client`, `sse-starlette`, `boto3`, `hvac`, `openai`, `anthropic`, `hypothesis`, `pytest`, `pytest-asyncio`

**References:** Req 6, Req 8, Design §Directory Layout

---

### 1.2 Implement ConfigService
- [x] Create `prediction_arb/bot/config.py` with `Config` dataclass covering all variables listed in Design §Non-Secret Config and §Required Secrets
- [x] Implement `ConfigService.load()`: read env vars, dispatch to `_load_aws()`, `_load_vault()`, or `_load_env()` based on `SECRET_BACKEND`
- [x] Implement `_load_aws()` using `boto3` with IAM instance profile credential chain (no hardcoded keys)
- [x] Implement `_load_vault()` using `hvac` with `VAULT_TOKEN` env var
- [x] Implement range validation: exit with code 1 if `MIN_SPREAD_PCT < 0`, `MAX_POSITIONS < 1`, `CAPITAL <= 0`, or any other out-of-range numeric value
- [x] Implement missing-required-secret detection: log CRITICAL and `sys.exit(1)` for any absent required secret
- [x] Apply documented defaults for all optional fields; log INFO for each default applied
- [x] Implement `ConfigService.refresh_secrets()` for periodic re-fetch every 3600s
- [x] Ensure no secret value is ever passed to any logger

**References:** Req 6.1–6.8, Design §Config/Secrets Loading Flow, Property 12, Property 13

---

## Group 2: Platform Clients

### 2.1 Implement BaseClient
- [x] Create `prediction_arb/bot/clients/base.py` with `BaseClient` abstract class
- [x] Implement `_request()` with retry loop: up to 3 attempts, exponential backoff (1s, 2s, 4s) on timeout or 5xx
- [x] Handle HTTP 429: sleep `Retry-After` header value (default 60s) before retrying
- [x] Handle HTTP 401: call `_reauthenticate()` once before retrying
- [x] Track `_consecutive_failures` counter; emit WARNING log when count reaches 5
- [x] Record `arb_platform_api_latency_seconds` histogram observation on every request
- [x] Reset `_consecutive_failures` to 0 on any successful response

**References:** Req 5.1–5.3, Req 5.9, Design §BaseClient

---

### 2.2 Implement KalshiClient (read-only)
- [x] Create `prediction_arb/bot/clients/kalshi.py` extending `BaseClient`
- [x] Implement `get_markets(series_ticker)` → `GET /trade-api/v2/markets`
- [x] Implement `get_market(ticker)` → `GET /trade-api/v2/markets/{ticker}`
- [x] Implement `get_orderbook(ticker)` → `GET /trade-api/v2/markets/{ticker}/orderbook`; parse `orderbook_fp` to extract `best_yes_bid`, `best_yes_ask`, `yes_mid`, `depth_5pct`
- [x] Implement `get_series()` → `GET /trade-api/v2/series` for slow-loop discovery
- [x] Implement RSA-based request signing for authenticated endpoints only; no order endpoints
- [x] Implement optional WebSocket client for `orderbook_delta` channel when `KALSHI_WS_ENABLED=true`: subscribe, apply delta/snapshot messages to in-memory orderbook state
- [x] Implement `_reauthenticate()` for token refresh without restart
- [x] Enforce read-only constraint: raise `NotImplementedError` for any order-placement method

**References:** Req 5.5, Req 15.1, Req 15.5, Req 15.10, Design §Kalshi endpoints

---

### 2.3 Implement PolymarketClient (read-only)
- [x] Create `prediction_arb/bot/clients/polymarket.py` extending `BaseClient`
- [x] Implement `get_markets()` → `GET https://gamma-api.polymarket.com/markets`
- [x] Implement `get_market(condition_id)` → `GET https://gamma-api.polymarket.com/markets/{conditionId}`
- [x] Implement `get_orderbooks(token_ids)` → `POST https://clob.polymarket.com/books` batching up to 500 token IDs; parse `bids`/`asks` arrays to compute `best_bid`, `best_ask`, `mid`, `depth_5pct`
- [x] Implement `get_prices(token_ids)` → `POST https://clob.polymarket.com/prices` for lightweight fast-loop fallback
- [x] Implement optional WebSocket client for `book` channel when `POLYMARKET_WS_ENABLED=true`: subscribe, apply snapshot and `best_bid_ask` updates to in-memory state
- [x] Enforce read-only constraint: no order endpoints

**References:** Req 5.6, Req 15.2, Req 15.6, Req 15.11, Design §Polymarket endpoints

---

### 2.4 Implement GeminiClient (read + write)
- [x] Create `prediction_arb/bot/clients/gemini.py` extending `BaseClient`
- [x] Implement HMAC-SHA384 request signing for all authenticated endpoints
- [x] Implement `get_events()` → `GET /v1/prediction-markets/events?status=active`
- [x] Implement `get_event(event_id)` → `GET /v1/prediction-markets/events/{eventId}`
- [x] Implement `get_orderbook(symbol)` → `GET /v1/book/{symbol}`; compute `best_bid`, `best_ask`, `yes_mid`, `depth_3pct_usd`
- [x] Implement `place_order(event_id, side, qty, price)` → `POST /v1/order/new`
- [x] Implement `cancel_order(order_id)` → `POST /v1/order/cancel`
- [x] Implement `get_order_status(order_id)` → `POST /v1/order/status`
- [x] Implement `get_open_orders()` → `POST /v1/orders` (used on startup)
- [x] Implement persistent authenticated WebSocket: stream `{symbol}@bookTicker` for all active matched markets; stream `orders@account` for fill notifications
- [x] Fall back to REST polling at `PRICE_POLL_INTERVAL_SECONDS` on WebSocket disconnect; reconnect with exponential backoff
- [x] Implement `_reauthenticate()` for HMAC key refresh

**References:** Req 5.7, Req 5.8, Req 15.3, Req 15.7, Design §Gemini endpoints, Property 11

---

## Group 3: Event Matcher

### 3.1 Implement extraction utilities
- [x] Create `prediction_arb/bot/matcher.py` with `ASSET_MAP`, `ABOVE_KEYWORDS`, `BELOW_KEYWORDS` constants
- [x] Implement `extract_asset(title)` with word-boundary enforcement; return canonical symbol or `None`
- [x] Implement `extract_price_level(title)` handling `$95,000`, `$95k`, `95000`, `0.95` formats; apply plausibility filter (100 ≤ price ≤ 10,000,000)
- [x] Implement `extract_direction(title)` mapping to `"above"` | `"below"` | `None`; check ±2-word context for `"reach a low"` style inversions
- [x] Implement `extract_date(event)` with field priority (`end_date` → `expiry_date` → `resolution_date` → `close_time` → `endDateIso`) and title-pattern fallback; normalise to UTC date

**References:** Req 14.2, Design §Stage 2 Structured Extraction

---

### 3.2 Implement rule-based pre-filter (Stage 1)
- [x] Implement `_rule_score(ref, target)` computing weighted sum across four dimensions: asset (0.30), price level (0.35), direction (0.15), resolution date (0.20)
- [x] Asset dimension: 1.0 if same canonical symbol, 0.0 if different detected symbols, 0.5 if either is `None`
- [x] Price dimension: 1.0 if within 1% of each other, 0.0 if > 1% apart, 0.5 if either is `None`
- [x] Direction dimension: 1.0 if same direction, 0.0 if opposite, 0.5 if either is `None`
- [x] Date dimension: 1.0 if within 3 days, 0.0 if > 3 days apart, 0.5 if either is `None`
- [x] Implement routing: score < 0.40 → reject; 0.40–0.74 → LLM; ≥ 0.75 → accept
- [x] Implement asset pre-filter: skip pair immediately if both events have detected assets that differ

**References:** Req 14.1–14.4, Req 14.9, Design §Stage 1, Property 15

---

### 3.3 Implement MatchingToolRegistry and LLM tool schemas
- [x] Define `MATCH_TOOL_SCHEMA` (OpenAI function-calling format) with all required fields: `equivalent`, `confidence`, `reasoning`, `asset`, `price_level`, `direction`, `resolution_date`, `inverted`
- [x] Define `ANTHROPIC_MATCH_TOOL` with equivalent `input_schema`
- [x] Define `EXTRACTION_TOOLS` list: `extract_asset`, `extract_price_level`, `extract_direction` tool schemas
- [x] Implement `MatchingToolRegistry` mapping tool names to the extraction functions from task 3.1
- [x] Implement `_execute_extraction_tool(name, args)` dispatching to the registry

**References:** Req 14.15, Req 14.16, Req 14.18, Req 14.19, Design §LLM Tooling Architecture

---

### 3.4 Implement LLM backends (Stage 3)
- [x] Implement `_build_user_message(event_a, event_b, rule_score, ob_ctx)` injecting live orderbook context when available
- [x] Implement `_call_llm_with_tools()` multi-turn loop (max 3 turns) with `asyncio.timeout(10.0)` budget
- [x] Implement OpenAI backend: call `openai.AsyncOpenAI` with `tool_choice="auto"`, model `gpt-4o-mini`
- [x] Implement Anthropic backend: call `anthropic.AsyncAnthropic` with tool-use mode, model `claude-3-haiku-20240307`
- [x] Implement `_parse_match_result(args)` → `LLMMatchResult`; clamp `confidence` to [0.0, 1.0]; normalise `direction`
- [x] On `TimeoutError`, `json.JSONDecodeError`, or any validation failure: log WARNING, return rule-based result with `backend="rule_based"` and `confidence=rule_score`
- [x] Log every tool invocation at DEBUG level (tool name, input args, return value)
- [x] Emit `arb_matcher_llm_calls_total` counter labeled by `backend` and `outcome`

**References:** Req 14.5, Req 14.7, Req 14.15–14.20, Design §Stage 3, Property 25, Property 26

---

### 3.5 Implement EventMatcher cache and batch_match
- [x] Implement `_cache_key(ref, target)` as SHA-256 of sorted `[f"{title_a}|{date_a}", f"{title_b}|{date_b}"]` — order-independent
- [x] Implement in-memory `dict[str, CacheEntry]` with lazy expiry pruning on each `batch_match` call
- [x] Implement `warm_cache_from_db()` loading non-expired rows from `match_cache` table on startup
- [x] Implement `persist_result(key, result)` writing to `match_cache` table asynchronously
- [x] Implement `prune_expired()` removing expired in-memory entries; background DB pruning every 3600s
- [x] Implement `batch_match(refs, targets, min_confidence)` with cache-first lookup, asset pre-filter, async LLM gather capped at `MAX_CONCURRENT_LLM_CALLS` semaphore
- [x] Implement `match(ref, target)` single-pair entry point
- [x] Expose `cache_hit_rate`, `llm_call_count`, `last_batch_duration_ms` properties
- [x] Emit `arb_matcher_cache_hit_rate` Prometheus gauge after each batch

**References:** Req 7.1–7.8, Req 14.8, Req 14.10, Req 14.11, Req 14.13, Req 14.14, Design §Cache Design, Property 14, Property 22, Property 23

---

### 3.6 Implement inverted-pair handling
- [x] In `ArbitrageEngine.score()`, detect `MatchedPair.result.inverted=True` and compute effective reference price as `1.0 - ref_event.yes_price`
- [x] Ensure spread direction is computed against the flipped reference price
- [x] Add `inverted` field to `Opportunity` dataclass and persist it

**References:** Req 14.6, Design §inverted flag handling, Property 24

---

## Group 4: Data Collection (PricePoller + OrderbookCache)

### 4.1 Implement OrderbookCache
- [x] Create `prediction_arb/bot/orderbook_cache.py` with `OrderbookCache` class
- [x] Implement `update(snapshot)` storing snapshot keyed by `(platform, ticker)`
- [x] Implement `get(platform, ticker)` returning most recent `OrderbookSnapshot` or `None`
- [x] Implement `get_all_for_pair(pair)` returning dict of snapshots for all platforms in a `MatchedPair`
- [x] Implement `is_fresh(platform, ticker, max_age_seconds)` returning `True` iff `(now - fetched_at).total_seconds() <= max_age_seconds`
- [x] Thread-safe (asyncio-safe) access: use `asyncio.Lock` if mutated from multiple coroutines

**References:** Req 15.8, Design §OrderbookCache interface, Property 28

---

### 4.2 Implement PricePoller
- [x] Create `prediction_arb/bot/price_poller.py` with `PricePoller` class
- [x] Implement `poll_once()`: for each active matched pair, fetch Kalshi orderbook (per-ticker REST), Polymarket orderbooks (single batched POST up to 500 token IDs), and Gemini orderbook (per-symbol REST)
- [x] Compute `depth_5pct` for Kalshi: sum contract quantities for YES bid levels within 5¢ of best YES bid using `orderbook_fp`
- [x] Compute `depth_5pct` for Polymarket: sum contract sizes for bid levels within 5¢ of best bid from CLOB `/books`
- [x] Compute `depth_3pct_usd` for Gemini: sum `price * quantity` for ask levels within 3¢ of best ask
- [x] Persist each fetched orderbook as `OrderbookSnapshot` to DB and update `OrderbookCache`
- [x] On per-market fetch failure: log WARNING, retain previous snapshot with original `fetched_at`, continue polling other markets
- [x] Record `arb_orderbook_fetch_duration_seconds` histogram labeled by `platform`
- [x] Emit WARNING when any platform's p95 fetch latency exceeds 5s over a rolling 5-minute window

**References:** Req 15.1–15.9, Req 15.12, Design §PricePoller interface, Property 29

---

### 4.3 Implement reference price computation
- [x] Create `compute_reference_price(kalshi_ob, poly_ob)` in `prediction_arb/bot/engine.py` (or a shared `utils.py`)
- [x] Volume-weighted average when both platforms have `depth_5pct >= 10`
- [x] Single-source fallback when only one platform is liquid
- [x] Set `signal_disagreement=True` and reduce `match_confidence` by 0.10 when `|kalshi_mid - poly_mid| > 0.05`
- [x] Raise `ValueError` when no liquid reference price is available
- [x] Ensure reference price is always derived from `OrderbookSnapshot.yes_mid`, never from market list `yes_price`

**References:** Req 15.15, Design §Reference Price Construction, Property 30

---

## Group 5: Arbitrage Engine & Risk Manager

### 5.1 Implement ArbitrageEngine
- [x] Create `prediction_arb/bot/engine.py` with `ArbitrageEngine` class
- [x] Implement `score(pairs)`: for each `MatchedPair`, compute `reference_price` (task 4.3), `gemini_mid`, `spread`, `spread_pct`, `direction` (buy_yes / buy_no), `days_to_resolution`, `risk_score`
- [x] Implement `determine_direction(ref_price, gemini_mid)` returning `(side, entry_price)` per design spec
- [x] Implement `kelly_fraction(ref_price, entry_price, side)` with corrected Kelly formula and quarter-Kelly (0.25×) cap at `MAX_POSITION_PCT`
- [x] Implement `rank(opps)` sorting by `spread_pct` descending (primary) and `risk_score` ascending (secondary)
- [x] Reject opportunities where spread falls inside Gemini's own bid-ask spread
- [x] Reject opportunities where `OrderbookSnapshot.fetched_at` is older than `MAX_PRICE_AGE_SECONDS`; log as `stale_orderbook`
- [x] Handle inverted pairs: flip reference price direction before spread computation (task 3.6)

**References:** Req 4.6, Req 4.7, Req 4.8, Req 4.9, Req 15.13, Req 15.15, Design §Entry Logic, Design §Position Sizing

---

### 5.2 Implement RiskManager
- [x] Create `prediction_arb/bot/risk.py` with `RiskManager` class and `RiskDecision` dataclass
- [x] Implement `evaluate(opp, portfolio)` checking all 9 rejection conditions in order:
  - `open_positions >= MAX_POSITIONS` → deny: position cap
  - `position_size > MAX_POSITION_PCT * capital` → clamp to `MAX_POSITION_PCT`
  - `drawdown > MAX_DRAWDOWN_PCT` → deny + suspend + alert
  - `spread_pct < MIN_SPREAD_PCT` → deny: spread too small
  - `confidence < MIN_CONFIDENCE` → deny: low confidence
  - `risk_score > MAX_RISK` → deny: risk too high
  - `price_age > MAX_PRICE_AGE_SECONDS` → deny: stale_price
  - `gemini_depth < MIN_GEMINI_DEPTH_USD` → deny: insufficient_liquidity
  - spread inside Gemini bid-ask → deny: spread_inside_noise
- [x] Implement `is_suspended()` and `resume()` for drawdown kill-switch
- [x] Once suspended, `is_suspended()` returns `True` for all subsequent calls until `resume()` is explicitly called
- [x] Cap execution at `MAX_OPPORTUNITIES_PER_SCAN` per scan cycle; log WARNING for excess
- [x] Log every risk decision (allow/deny/suspend) at INFO level with reason and metrics
- [x] On Gemini order error: mark position `failed`, preserve capital, log ERROR

**References:** Req 4.1–4.11, Design §Risk Manager Decision Flow, Property 7, Property 8, Property 9

---

## Group 6: Executor & Position Monitor

### 6.1 Implement Executor
- [x] Create `prediction_arb/bot/executor.py` with `Executor` class
- [x] Implement `execute(opp, size_usd)`:
  - Re-check price freshness immediately before placement; abort if stale
  - In `DRY_RUN=true` mode: simulate fill at current ask, log at INFO, persist position with `status="filled"` (simulated), do not call Gemini API
  - In live mode: call `GeminiClient.place_order()`; on error mark position `failed`, preserve capital, log ERROR, send alert
  - Compute `quantity = floor(size_usd / entry_price)`
  - Set `exit_strategy`: `target_convergence` if `days_to_resolution > CONVERGENCE_EXIT_DAYS`, else `hold_to_resolution`
  - Compute `target_exit_price` and `stop_loss_price` at entry time
  - Persist `GeminiPosition` to `StateStore`; broadcast `position_opened` SSE event
- [x] Implement `close_position(pos, reason)`: place limit sell at current Gemini bid; persist exit fields; broadcast `position_closed` SSE event

**References:** Req 1.2, Req 1.4, Req 4.5, Req 10.2, Design §Exit Logic, Design §Executor interface

---

### 6.2 Implement PositionMonitor
- [x] Create `prediction_arb/bot/monitor.py` with `PositionMonitor` class
- [x] Implement `run_once()`: load all open positions from `StateStore`; for each, fetch current Gemini orderbook
- [x] Check stop-loss: if `gemini_mid` moves `STOP_LOSS_PCT` against entry price, call `executor.close_position(pos, reason="stop_loss")`
- [x] Check convergence exit: if `exit_strategy == "target_convergence"` and `|gemini_mid - ref_price| < CONVERGENCE_THRESHOLD` and `gemini_bid > entry_price`, call `executor.close_position(pos, reason="convergence")`
- [x] Skip position if Gemini orderbook fetch returns `None`; log WARNING
- [x] Handle near-resolution events (< 4h to expiry): set `early_exit_enabled=False`, hold to resolution only
- [x] Run as asyncio task every `MONITOR_INTERVAL_SECONDS` (default 60s)

**References:** Req 1.3, Design §PositionMonitor, Design §Exit Logic

---

## Group 7: State Store & Database

### 7.1 Define SQLAlchemy ORM models
- [x] Create `prediction_arb/bot/models.py` with SQLAlchemy 2 mapped classes for: `Opportunity`, `GeminiPosition`, `PnlSnapshot`, `MatchCache`, `OrderbookSnapshot`
- [x] Match all column types and constraints to the DB schema in Design §Database Schema
- [x] Add `portfolio_summary` view definition (used by `/api/v1/portfolio`)
- [x] Add `updated_at` trigger or ORM event for `GeminiPosition`

**References:** Req 1.1, Req 1.2, Req 1.5, Design §Database Schema

---

### 7.2 Implement Alembic migrations
- [x] Configure `prediction_arb/migrations/env.py` to use async SQLAlchemy engine and import `models.py` metadata
- [x] Create initial migration `versions/0001_initial.py` creating all tables, indexes, and the `portfolio_summary` view
- [x] Create migration `versions/0002_match_cache.py` for the `match_cache` table and its `expires_at` index
- [x] Verify `alembic upgrade head` then `alembic downgrade base` succeeds cleanly

**References:** Req 1.1, Design §Database Schema

---

### 7.3 Implement StateStore
- [x] Create `prediction_arb/bot/state.py` with `StateStore` class using `sqlalchemy.ext.asyncio`
- [x] Implement `save_opportunity(opp)`, `save_position(pos)`, `update_position(pos)`, `get_open_positions()`
- [x] Implement `get_pnl_history(from_ts, to_ts)` querying `pnl_snapshots`
- [x] Implement `get_aggregate_stats(window)` returning `AggregateStats` (total P&L, win rate, avg spread, exit reason breakdown, trade count)
- [x] Implement write-failure retry: up to 3 attempts with 0.5s/1s/2s backoff; log CRITICAL on final failure; re-raise to allow caller to continue
- [x] Implement `get_opportunity(id)` for round-trip reads
- [x] Implement `get_orderbook_snapshot(platform, ticker)` and `save_orderbook_snapshot(snapshot)`
- [x] Restore open positions on startup: `get_open_positions()` called by `PositionMonitor` and `Executor` at init

**References:** Req 1.1–1.7, Design §StateStore interface, Property 1, Property 2

---

## Group 8: Observability (Metrics, Logging, Alerts)

### 8.1 Implement structured logging
- [x] Create `prediction_arb/bot/logging_setup.py` configuring `structlog` with JSON renderer to stdout
- [x] Ensure every log record includes: `timestamp` (ISO 8601), `level`, `component`, `event`, `message`
- [x] Add log records at correct levels for: opportunity detected (INFO), position submitted (INFO), API failure (WARNING), scan cycle error (ERROR with stack trace)
- [x] Support configurable log level via `Config.LOG_LEVEL` without restart
- [x] Configure optional rotating file handler with configurable max size and retention
- [x] Ensure no secret value is passed to any logger at any level

**References:** Req 2.1–2.7, Property 3, Property 4

---

### 8.2 Implement MetricsExporter
- [x] Create `prediction_arb/bot/metrics.py` with a `prometheus_client` registry
- [x] Define all required metrics:
  - `arb_scan_cycles_total` (Counter)
  - `arb_opportunities_detected_total` (Counter, label: `platform_pair`)
  - `arb_trades_executed_total` (Counter, labels: `platform`, `side`)
  - `arb_open_positions` (Gauge)
  - `arb_available_capital_usd` (Gauge)
  - `arb_scan_duration_seconds` (Histogram)
  - `arb_platform_api_latency_seconds` (Histogram, labels: `platform`, `endpoint`)
  - `arb_realized_pnl_usd` (Gauge)
  - `arb_matcher_cache_hit_rate` (Gauge)
  - `arb_matcher_llm_calls_total` (Counter, labels: `backend`, `outcome`)
  - `arb_orderbook_fetch_duration_seconds` (Histogram, label: `platform`)
- [x] Expose `/metrics` endpoint in Prometheus text format via FastAPI route (no auth required)
- [x] Continue normal operation and log WARNING if `/metrics` endpoint is unavailable

**References:** Req 3.1–3.10, Req 14.11, Req 14.12, Req 15.12, Property 5, Property 6

---

### 8.3 Implement AlertManager
- [x] Create `prediction_arb/bot/alerts.py` with `AlertManager` class
- [x] Support `ALERT_CHANNEL` values: `slack` (POST to webhook URL), `email` (SMTP), `webhook` (generic POST), `none`
- [x] Implement deduplication: track last-sent timestamp per alert type; suppress duplicates within `ALERT_DEDUP_WINDOW` seconds
- [x] Send alert within 60s when drawdown suspension triggered (include drawdown % and available capital)
- [x] Send alert when platform unavailable for > 3 consecutive scan cycles
- [x] Send alert immediately on Gemini position execution failure (event ID, side, amount, error)
- [x] Send alert when `spread_pct > ALERT_SPREAD_THRESHOLD`
- [x] On delivery failure: log WARNING, retry once, then continue without further retries

**References:** Req 9.1–9.7, Property 20, Property 21

---

## Group 9: API Server & SSE

### 9.1 Implement SSEBroadcaster
- [x] Create `prediction_arb/bot/api/sse.py` with `SSEBroadcaster` class
- [x] Implement `publish(event_type, data)` pushing to all active subscriber queues
- [x] Implement `subscribe()` as an `AsyncGenerator` yielding SSE-formatted strings
- [x] Emit heartbeat events every 15 seconds to keep connections alive through proxies
- [x] Support event types: `opportunity_detected`, `position_opened`, `position_closed`, `risk_suspended`, `heartbeat`

**References:** Req 11.7, Design §SSE Event Types

---

### 9.2 Implement FastAPI app and authentication
- [ ] Create `prediction_arb/bot/api/server.py` with `create_app()` factory
- [x] Implement `require_auth` FastAPI dependency: extract `Authorization: Bearer <token>`, validate with `secrets.compare_digest`, return HTTP 401 on failure
- [x] Configure `CORSMiddleware` with `DASHBOARD_ORIGIN`, `allow_credentials=True`, `allow_headers=["Authorization"]`
- [x] Mount `/metrics` and `/healthz` routes without auth requirement
- [x] Return HTTP 405 for POST/PUT/DELETE/PATCH on all REST endpoints

**References:** Req 11.8, Req 11.9, Req 11.10, Design §Authentication, Property 18, Property 19

---

### 9.3 Implement REST route handlers
- [x] Create `prediction_arb/bot/api/routes.py` with all route handlers
- [x] `GET /healthz`: return 200 `{"status":"ok",...}` when healthy; 503 with failing component description when DB unreachable or all platforms failing
- [x] `GET /api/v1/status`: mode, uptime_seconds, scan_count, open_positions, available_capital_usd, realized_pnl_usd, last_scan_at
- [x] `GET /api/v1/opportunities`: current actionable opportunities with all scored fields
- [x] `GET /api/v1/trades?limit=&offset=`: paginated trade history from StateStore
- [x] `GET /api/v1/portfolio`: portfolio summary + open positions + per-position P&L
- [x] `GET /api/v1/pnl/history?from=&to=`: time-series P&L snapshots (ISO 8601 params)
- [x] `GET /api/v1/feeds/health`: per-platform connectivity status, last success timestamp, consecutive failure count
- [x] `GET /api/v1/events`: SSE stream via `sse-starlette`; pass token as query param for `EventSource` compatibility
- [x] `GET /api/v1/orderbooks`: current in-memory orderbook snapshot for each active matched market
- [x] Disable API server entirely when `API_SERVER_ENABLED=false`

**References:** Req 11.1–11.11, Req 15.14, Design §Endpoints

---

## Group 10: Scanner & Main Loop

### 10.1 Implement Scanner
- [x] Create `prediction_arb/bot/scanner.py` with `Scanner` class
- [x] Implement `fetch_all()` fetching market lists from Kalshi, Polymarket, and Gemini in parallel (`asyncio.gather`)
- [x] Return `ScanResult` dataclass with `kalshi`, `polymarket`, `gemini` event lists and `feed_health` per platform
- [x] On per-platform failure: continue with remaining platforms; set that platform's `FeedHealth.status="down"`; log WARNING
- [x] Track consecutive failure counts per platform; trigger `AlertManager` after 3 consecutive failures
- [x] Update `arb_scan_cycles_total` counter on each completed scan

**References:** Req 5.4, Req 9.3, Design §Scanner interface, Property 10

---

### 10.2 Implement main asyncio event loop
- [x] Create `prediction_arb/bot/main.py` as the entrypoint
- [x] Initialise all components: `ConfigService`, `StateStore`, `OrderbookCache`, `KalshiClient`, `PolymarketClient`, `GeminiClient`, `EventMatcher`, `ArbitrageEngine`, `RiskManager`, `Executor`, `PositionMonitor`, `Scanner`, `PricePoller`, `MetricsExporter`, `AlertManager`, `SSEBroadcaster`, `APIServer`
- [x] Run Alembic `upgrade head` on startup; exit with code 1 on migration failure
- [x] Warm `EventMatcher` cache from DB on startup
- [x] Restore open positions from `StateStore` on startup
- [x] Schedule slow loop (Scanner + EventMatcher) every `SCAN_INTERVAL_SECONDS`
- [x] Schedule fast loop (PricePoller + Engine + RiskManager + Executor) every `PRICE_POLL_INTERVAL_SECONDS`
- [x] Schedule `PositionMonitor.run_once()` every `MONITOR_INTERVAL_SECONDS`
- [x] Schedule `ConfigService.refresh_secrets()` every 3600s
- [x] Handle SIGTERM: stop accepting new scan triggers, complete current cycle (max 30s), flush logs, close HTTP sessions, close DB pool, exit 0
- [x] Catch unhandled exceptions in scan cycle: log ERROR with stack trace, sleep 60s, retry

**References:** Req 1.3, Req 6.8, Req 8.6, Design §Scan Cycle Sequence

---

## Group 11: Backtesting Mode

### 11.1 Implement backtest runner
- [x] Add `--backtest` CLI flag to `main.py` (or a separate `prediction_arb/bot/backtest.py`)
- [x] In backtest mode: load historical `Opportunity` records from `StateStore` for a configurable time window; do not make any platform API calls
- [x] Replay each opportunity through `ArbitrageEngine` and `RiskManager` using the same Kelly sizing and risk rules as live mode
- [x] Simulate `GeminiPosition` fills at recorded entry prices; compute P&L using `resolved_price` from DB
- [x] Apply configurable fee model (`FEE_PER_CONTRACT`) to net P&L
- [x] Compute summary: total opportunities replayed, trades simulated, gross P&L, net P&L, win rate, max drawdown, Sharpe ratio
- [x] Emit summary as structured JSON to stdout and human-readable table to stderr
- [x] Ensure determinism: same dataset + same config → identical P&L and trade count on every run

**References:** Req 10.1–10.5, Property 16, Property 17

---

## Group 12: Dashboard (Next.js 14)

### 12.1 Scaffold Next.js 14 App Router project
- [x] Create `prediction_arb/dashboard/` with `package.json` listing: `next@14`, `react`, `react-dom`, `recharts`, `typescript`, `@types/react`
- [x] Create `app/layout.tsx` with global providers and metadata
- [x] Create `app/page.tsx` as root page wiring SSE hook and global state context
- [x] Create `dashboard/Dockerfile` for production build (`next build` + `next start`)

**References:** Req 12.1, Req 12.14, Design §Dashboard Design

---

### 12.2 Implement typed API client and SSE hook
- [x] Create `dashboard/lib/api.ts` with typed `fetch` wrappers for all API endpoints; include `Authorization: Bearer` header from `NEXT_PUBLIC_API_TOKEN`
- [x] Create `dashboard/lib/sse.ts` with `useSSE(url, token)` hook: open `EventSource` with token as query param; reconnect with exponential backoff (1s → 2s → 4s → … → 30s max); render `<ReconnectingBanner>` when disconnected
- [x] Implement `useInterval` fallback polling `GET /api/v1/status` every 10s when SSE is unavailable

**References:** Req 12.11–12.13, Req 12.16, Design §lib/sse.ts Skeleton

---

### 12.3 Implement dashboard panels
- [x] Create `dashboard/app/components/StatusPanel.tsx`: mode, uptime, scan count, last scan timestamp, available capital
- [x] Create `dashboard/app/components/RiskPanel.tsx`: drawdown %, capital utilization %, suspended flag
- [x] Create `dashboard/app/components/FeedHealthPanel.tsx`: per-platform status, last success time, consecutive failures
- [x] Create `dashboard/app/components/PnlChart.tsx`: recharts `LineChart` sourced from `/api/v1/pnl/history`; time range selector (1h, 24h, 7d, 30d)
- [x] Create `dashboard/app/components/PositionsTable.tsx`: open positions with event title, side, entry price, amount_usd, quantity, unrealized P&L
- [x] Create `dashboard/app/components/OpportunitiesPanel.tsx`: last 20 opportunities with spread_pct, match_confidence, risk_score, signal sources, status
- [x] Create `dashboard/app/components/TradesPanel.tsx`: last 20 closed trades with event title, side, amount_usd, entry price, resolved P&L
- [x] Update affected panel within 2 seconds of receiving an SSE event

**References:** Req 12.4–12.11, Design §Component Tree

---

## Group 13: Infrastructure & Deployment

### 13.1 Write Dockerfile (bot)
- [x] Create `prediction_arb/Dockerfile` with multi-stage build: `python:3.12-slim` builder stage installs deps; minimal runtime stage copies only the installed packages and source
- [x] Final image must be under 200 MB
- [x] Set `CMD` to run `python -m prediction_arb.bot.main`
- [x] Ensure all config is via env vars; no config files baked into image

**References:** Req 8.1, Req 8.7, Req 8.8

---

### 13.2 Write Docker Compose stack
- [x] Create `prediction_arb/infra/docker-compose.yml` with services: `bot`, `postgres` (16-alpine), `prometheus` (v2.51.0), `dashboard`, `nginx` (1.25-alpine)
- [x] Define `pgdata` and `promdata` named volumes for data persistence
- [x] Configure `bot` service with `awslogs` log driver pointing to `/arb/bot` CloudWatch log group
- [x] Restrict all internal ports (5432, 9090, 8000, 3000) to the internal Docker network; only nginx exposes 80 and 443 on the host
- [x] Add `depends_on` ordering: `bot` depends on `postgres`; `nginx` depends on `bot` and `dashboard`

**References:** Req 8.4, Req 8.5, Req 8.9, Req 13.3, Design §Docker Compose Service Topology

---

### 13.3 Write nginx configuration
- [x] Create `prediction_arb/infra/nginx/nginx.conf` with HTTP→HTTPS redirect, SSL termination (TLSv1.2/1.3), proxy to `bot:8000` for `/api/` and `/healthz`, proxy to `dashboard:3000` for `/`
- [x] Set `proxy_buffering off` and `proxy_read_timeout 3600s` for SSE connections
- [x] Restrict `/metrics` to VPC CIDR (`allow 10.0.0.0/8; deny all`)
- [x] Create `prediction_arb/infra/nginx/ssl/` directory with placeholder for cert and key

**References:** Req 12.2, Req 12.3, Req 13.3, Design §nginx Configuration

---

### 13.4 Write Prometheus scrape config
- [x] Create `prediction_arb/infra/prometheus/prometheus.yml` with `scrape_interval: 15s` and job `arb_bot` targeting `bot:8000` at `/metrics`

**References:** Req 3.1, Design §Prometheus Scrape Config

---

### 13.5 Write IAM policy and security group documentation
- [x] Create `prediction_arb/infra/iam/policy.json` with minimal permissions: `secretsmanager:GetSecretValue`, `secretsmanager:DescribeSecret` on `arb/*` ARNs; `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` on `/arb/*` log group
- [x] Create `prediction_arb/infra/iam/security-group.md` documenting exact port rules: inbound 80/443 from `0.0.0.0/0`, inbound 22 from operator CIDR, all other ports internal only

**References:** Req 13.1, Req 13.2, Req 13.3, Design §IAM Instance Profile Policy

---

### 13.6 Write EC2 bootstrap procedure
- [x] Add `prediction_arb/infra/bootstrap.sh` user-data script covering: Docker install, repo clone, `.env` population from AWS Secrets Manager, TLS cert setup (Let's Encrypt or self-signed), Alembic migration, `docker compose up -d`, health check verification
- [x] Document EBS encryption-at-rest as a required launch step in `README.md` or `infra/` docs

**References:** Req 8.10, Req 13.5, Req 13.6, Design §Deployment Bootstrap Procedure

---

## Group 14: Unit Tests

### 14.1 Unit tests: ConfigService
- [x] Create `tests/unit/test_config.py`
- [x] Test `SystemExit` on missing required secret
- [x] Test defaults applied for all optional fields
- [x] Test out-of-range validation exits with non-zero code
- [x] Test no secret value appears in any log output during `load()`

**References:** Req 6.4–6.7

---

### 14.2 Unit tests: Platform clients
- [ ] Create `tests/unit/test_clients.py`
- [x] Mock `httpx` responses; verify retry count (3) and backoff timing on timeout and 5xx
- [x] Verify HTTP 429 handling: sleep `Retry-After` value, retry once
- [x] Verify HTTP 401 triggers `_reauthenticate()` then retry
- [x] Verify empty result returned after final retry failure
- [x] Verify HMAC-SHA384 signature correctness for `GeminiClient`
- [x] Verify `KalshiClient` raises `NotImplementedError` for any order method
- [x] Verify `PolymarketClient` raises `NotImplementedError` for any order method

**References:** Req 5.1–5.8

---

### 14.3 Unit tests: EventMatcher extraction
- [ ] Create `tests/unit/test_matcher.py`
- [x] Test `extract_asset` for all entries in `ASSET_MAP`; verify word-boundary enforcement (e.g. "resolution" does not match "sol")
- [x] Test `extract_price_level` for all supported formats; verify plausibility filter rejects years and percentages
- [x] Test `extract_direction` for all `ABOVE_KEYWORDS` and `BELOW_KEYWORDS`; test "reach a low" inversion
- [x] Test `extract_date` field priority and title-pattern fallback
- [x] Test rule-based score for each dimension in isolation
- [x] Test routing thresholds: score < 0.40 → reject, ≥ 0.75 → accept, 0.40–0.74 → LLM
- [x] Test asset pre-filter skips pair when both events have different detected assets

**References:** Req 7.6, Req 7.7, Req 14.1–14.4, Req 14.9

---

### 14.4 Unit tests: ArbitrageEngine
- [ ] Create `tests/unit/test_engine.py`
- [x] Test `kelly_fraction` with known inputs; verify quarter-Kelly cap
- [x] Test `determine_direction` for ref > gemini_mid (buy YES) and ref < gemini_mid (buy NO)
- [x] Test `compute_reference_price` volume-weighted average, single-source fallback, disagreement flag
- [x] Test spread-inside-noise rejection
- [x] Test stale orderbook rejection
- [x] Test inverted pair flips reference price

**References:** Req 4.6–4.9, Req 15.13, Req 15.15

---

### 14.5 Unit tests: RiskManager
- [ ] Create `tests/unit/test_risk.py`
- [ ] Test each of the 9 rejection conditions individually
- [ ] Test drawdown suspension: `is_suspended()` returns `True` after threshold breach; returns `False` only after `resume()`
- [ ] Test `MAX_OPPORTUNITIES_PER_SCAN` cap with WARNING log
- [ ] Test position size clamping to `MAX_POSITION_PCT`

**References:** Req 4.1–4.11

---

### 14.6 Unit tests: Executor
- [ ] Create `tests/unit/test_executor.py`
- [ ] Test dry-run mode: no Gemini API call made; position persisted with simulated fill
- [ ] Test live mode: `GeminiClient.place_order` called with correct args
- [ ] Test freshness re-check before placement: abort if stale
- [ ] Test `exit_strategy` assignment based on `days_to_resolution`
- [ ] Test `close_position` places limit sell and persists exit fields

**References:** Req 1.2, Req 1.4, Req 4.5

---

### 14.7 Unit tests: AlertManager
- [ ] Create `tests/unit/test_alerts.py`
- [ ] Test alert sent on drawdown suspension within 60s
- [ ] Test dedup window: second identical alert within window is suppressed
- [ ] Test graceful failure when channel is unavailable (log WARNING, no exception)
- [ ] Test high-spread opportunity triggers alert

**References:** Req 9.1–9.7

---

### 14.8 Unit tests: API server
- [x] Create `tests/unit/test_api.py` using `httpx.AsyncClient` with FastAPI test app
- [x] Test HTTP 401 for missing or invalid bearer token on all endpoints
- [x] Test HTTP 405 for POST/PUT/DELETE/PATCH on all REST endpoints
- [x] Test `/healthz` returns 200 when healthy and 503 when DB is down
- [x] Test response schema for `/api/v1/status`, `/api/v1/opportunities`, `/api/v1/trades`, `/api/v1/portfolio`, `/api/v1/pnl/history`, `/api/v1/feeds/health`
- [x] Test SSE event delivery: publish event via `SSEBroadcaster`, verify it appears in SSE stream

**References:** Req 11.1–11.11

---

### 14.9 Unit tests: Backtest mode
- [ ] Create `tests/unit/test_backtest.py`
- [ ] Test no platform API calls are made in backtest mode
- [ ] Test report contains all required fields (total opps, trades, gross P&L, net P&L, win rate, max drawdown, Sharpe)
- [ ] Test JSON output to stdout and human-readable table to stderr
- [ ] Test determinism: same dataset + config → identical output on two runs

**References:** Req 10.1–10.5

---

## Group 15: Property-Based Tests

All PBT files use `hypothesis` with `@settings(max_examples=100)` unless noted. Each test is annotated with `# Feature: prediction-arbitrage-production, Property N: <text>`.

---

### 15.1 PBT: StateStore round-trip (Properties 1 & 2)
- [x] Create `tests/property/test_statestore_pbt.py`
- [ ] **Property 1** — `@given(st.builds(Opportunity, ...), st.builds(GeminiPosition, ...))`: save to StateStore, read back by ID, assert all fields equal original
- [ ] **Property 2** — `@given(st.lists(st.builds(GeminiPosition, status="resolved", pnl=st.floats(...)), ...))`: assert `get_aggregate_stats()` equals manually computed sum of P&L, win count, and trade count

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.7**

---

### 15.2 PBT: Logging correctness (Properties 3 & 4)
- [x] Create `tests/property/test_logging_pbt.py`
- [ ] **Property 3** — `@given(st.sampled_from([...system events...]))`: capture log output, assert parseable as JSON with required fields `timestamp`, `level`, `component`, `event`, `message`
- [ ] **Property 4** — `@given(st.builds(Config, ...with secret fields...))`: run `ConfigService.load()` with captured log output, assert no secret value appears in any log record

**Validates: Requirements 2.1, 2.2, 2.3, 6.7**

---

### 15.3 PBT: Metrics counters and gauges (Properties 5 & 6)
- [x] Create `tests/property/test_metrics_pbt.py`
- [x] **Property 5** — `@given(st.integers(min_value=0, max_value=50), ...)`: simulate N scan cycles, M opportunity detections, K trade executions; assert counter values equal N, M, K
- [x] **Property 6** — `@given(st.builds(Portfolio, ...))`: update portfolio state; assert gauge values equal `open_positions`, `available_capital`, `realized_pnl`

**Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.6, 3.9**

---

### 15.4 PBT: Risk evaluation thresholds (Properties 7, 8 & 9)
- [x] Create `tests/property/test_risk_pbt.py`
- [ ] **Property 7** — `@given(st.builds(Opportunity, ...), st.builds(Portfolio, ...))` with `@settings(max_examples=200)`: assert `evaluate()` returns `allowed=False` iff any rejection condition holds; when allowed, assert `clamped_size <= MAX_POSITION_PCT * available_capital`
- [ ] **Property 8** — `@given(st.builds(Portfolio, drawdown_pct=st.floats(min_value=0.21, max_value=1.0)))`: once suspended, assert `is_suspended()` returns `True` for all subsequent calls until `resume()` is called
- [ ] **Property 9** — `@given(st.lists(st.builds(Opportunity, ...), min_size=0, max_size=100))`: assert execution count equals `min(len(opps), MAX_OPPORTUNITIES_PER_SCAN)`

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.6**

---

### 15.5 PBT: Scanner partial platform data (Property 10)
- [x] Create `tests/property/test_scanner_pbt.py`
- [ ] **Property 10** — `@given(st.sets(st.sampled_from(["kalshi","polymarket","gemini"]), max_size=2))`: simulate the given set of platforms failing; assert `fetch_all()` returns data from non-failing platforms, empty list for failing platforms, and `FeedHealth.status="down"` for each failing platform

**Validates: Requirements 5.4**

---

### 15.6 PBT: Gemini HMAC-SHA384 signature (Property 11)
- [x] Create `tests/property/test_gemini_pbt.py`
- [ ] **Property 11** — `@given(st.dictionaries(st.text(), st.text()))`: for any payload dict, assert `GeminiClient`-produced `X-GEMINI-SIGNATURE` equals independently computed HMAC-SHA384 of base64-encoded payload using the configured secret

**Validates: Requirements 5.7**

---

### 15.7 PBT: Config defaults and validation (Properties 12 & 13)
- [x] Create `tests/property/test_config_pbt.py`
- [ ] **Property 12** — `@given(st.frozensets(st.sampled_from([...optional_vars...])))`: for any subset of absent optional vars, assert `Config` fields equal documented defaults with no `KeyError` or `None`
- [ ] **Property 13** — `@given(st.one_of(st.floats(max_value=-0.001), st.integers(max_value=0), ...))`: for any out-of-range numeric config value, assert `ConfigService.load()` raises `SystemExit`

**Validates: Requirements 6.5, 6.6**

---

### 15.8 PBT: Matcher determinism, dimension weights, cache key, batch threshold, inverted pairs, LLM fallback, tool schema, extraction tools (Properties 14, 15, 22, 23, 24, 25, 26, 27)
- [x] Create `tests/property/test_matcher_pbt.py`
- [ ] **Property 14** — `@given(st.builds(MarketEvent, ...), st.builds(MarketEvent, ...))`: call `match(a, b)` twice; assert same `confidence` and `equivalent`
- [ ] **Property 15** — `@given(st.builds(MarketEvent, ...), st.sampled_from(["asset","price","direction","date"]))`: change only one dimension from matching to non-matching; assert confidence decreases by exactly the documented weight for that dimension
- [ ] **Property 22** — `@given(st.builds(MarketEvent, ...), st.builds(MarketEvent, ...))`: assert `_cache_key(a, b) == _cache_key(b, a)`
- [ ] **Property 23** — `@given(st.lists(st.builds(MarketEvent, ...), ...), st.floats(min_value=0.0, max_value=1.0))`: assert every pair in `batch_match(refs, targets, min_confidence=t)` has `result.confidence >= t`
- [ ] **Property 24** — `@given(st.builds(MatchedPair, inverted=st.just(True), ref_yes_price=st.floats(0.01, 0.99)))`: assert effective reference price equals `1.0 - ref_yes_price`
- [ ] **Property 25** — `@given(st.builds(MarketEvent, ...), st.builds(MarketEvent, ...))`: simulate LLM timeout or malformed JSON; assert `match()` returns valid `MatchResult` with `backend="rule_based"` and no exception raised
- [ ] **Property 26** — `@given(st.fixed_dictionaries({"equivalent": st.booleans(), "confidence": st.floats(0.0, 1.0), "reasoning": st.text(), "inverted": st.booleans(), ...}))`: assert `_parse_match_result()` produces `LLMMatchResult` with `confidence` in [0.0, 1.0], valid `direction`, boolean `inverted`, no `KeyError`
- [ ] **Property 27** — `@given(st.text())`: assert `extract_asset(title)` equals `MatchingToolRegistry.execute("extract_asset", {"title": title})`

**Validates: Requirements 7.1, 14.2, 14.6, 14.7, 14.8, 14.13, 14.14, 14.15, 14.16, 14.18, 14.19**

---

### 15.9 PBT: Backtest determinism and P&L correctness (Properties 16 & 17)
- [x] Create `tests/property/test_backtest_pbt.py`
- [ ] **Property 16** — `@given(st.lists(st.builds(Opportunity, ...), min_size=1), st.builds(Config, ...))`: run backtest twice with same inputs; assert identical P&L, trade count, win rate, max drawdown
- [ ] **Property 17** — `@given(st.lists(st.builds(GeminiPosition, status="resolved", entry_price=st.floats(0.01,0.99), resolved_price=st.floats(0.0,1.0), quantity=st.integers(1,1000)), min_size=1))`: assert backtest gross P&L equals sum of `(resolved_price - entry_price) * quantity` for correct outcomes minus losses for incorrect

**Validates: Requirements 10.2, 10.5**

---

### 15.10 PBT: API 405 and 401 (Properties 18 & 19)
- [x] Create `tests/property/test_api_pbt.py`
- [ ] **Property 18** — `@given(st.sampled_from(["/api/v1/status", "/api/v1/opportunities", "/api/v1/trades", "/api/v1/portfolio", "/api/v1/pnl/history", "/api/v1/feeds/health"]), st.sampled_from(["POST","PUT","DELETE","PATCH"]))`: assert response status is 405
- [ ] **Property 19** — `@given(st.sampled_from([...all endpoints...]), st.one_of(st.none(), st.text()))`: for missing or incorrect token, assert response status is 401

**Validates: Requirements 11.8, 11.9**

---

### 15.11 PBT: Alert deduplication and high-spread trigger (Properties 20 & 21)
- [x] Create `tests/property/test_alerts_pbt.py`
- [ ] **Property 20** — `@given(st.integers(min_value=2, max_value=20), st.floats(min_value=0.0, max_value=299.0))`: fire identical alert N times within `ALERT_DEDUP_WINDOW` seconds; assert exactly 1 notification sent
- [ ] **Property 21** — `@given(st.builds(Opportunity, spread_pct=st.floats(min_value=0.201, max_value=1.0)))`: assert `AlertManager` enqueues exactly 1 alert (subject to dedup) containing opportunity details

**Validates: Requirements 9.5, 9.6**

---

### 15.12 PBT: OrderbookCache freshness, depth bounds, reference price source (Properties 28, 29 & 30)
- [x] Create `tests/property/test_orderbook_pbt.py`
- [ ] **Property 28** — `@given(st.builds(OrderbookSnapshot, fetched_at=st.datetimes()), st.integers(min_value=0, max_value=3600))`: assert `is_fresh()` returns `True` iff `(now - fetched_at).total_seconds() <= max_age_seconds`
- [ ] **Property 29** — `@given(st.lists(st.tuples(st.floats(0.01,0.99), st.floats(0.0,10000.0)), min_size=0, max_size=100))`: for any valid orderbook response, assert `depth_5pct` (or `depth_3pct_usd`) is `>= 0.0` and `<= total_volume_on_that_side`; empty orderbook produces `depth = 0.0` without exception
- [ ] **Property 30** — `@given(st.builds(MatchedPair, ...), st.builds(OrderbookSnapshot, ...))`: when fresh snapshot exists in cache, assert `reference_price` equals volume-weighted average of `yes_mid` values, not `yes_price` from market list

**Validates: Requirements 15.5, 15.6, 15.7, 15.8, 15.13, 15.15**

---

### 15.13 Integration tests
- [x] Create `tests/integration/test_statestore.py`: full write/read round-trips against real PostgreSQL; verify Alembic `upgrade head` then `downgrade base` succeeds
- [x] Create `tests/integration/test_scan_cycle.py`: full scan cycle with mocked platform clients returning fixture data; verify opportunities persisted, SSE events broadcast, metrics incremented

**References:** Design §Integration Tests

---
