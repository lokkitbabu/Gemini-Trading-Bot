# Requirements Document

## Introduction

The Prediction Arbitrage Production System is a hardened, observable, and deployable upgrade of the existing prototype (Python and Go) that scans Kalshi and Polymarket as read-only price feeds to detect mispricings on Gemini Predictions, then executes directional trades exclusively on Gemini Predictions. Kalshi and Polymarket serve as signal sources only — no orders are ever placed on those platforms. "Production" means the system is reliable enough to run unattended with real capital, observable enough to diagnose problems quickly, and deployable via standard infrastructure tooling.

The prototype already handles the core logic: event fetching, LLM/rule-based event matching, spread detection, Kelly-sized position execution, and dry-run simulation. The production system builds on that foundation by adding persistent state, structured observability, robust error handling, a risk management layer, secrets management, a deployment model, and a real-time web dashboard.

---

## Glossary

- **System**: The Prediction Arbitrage Production System as a whole.
- **Scanner**: The component that fetches live markets from Kalshi, Polymarket, and Gemini Predictions and identifies candidate arbitrage opportunities.
- **Matcher**: The component that determines whether two events from different platforms represent the same underlying outcome (LLM or rule-based).
- **Engine**: The component that scores, filters, and ranks arbitrage opportunities.
- **Executor**: The component that places, tracks, and manages orders exclusively on Gemini Predictions.
- **Risk_Manager**: The component that enforces capital limits, position caps, drawdown thresholds, and kill-switch logic.
- **State_Store**: The persistent storage layer (database) that records opportunities, trades, positions, and P&L history.
- **Metrics_Exporter**: The component that emits structured metrics (Prometheus-compatible) for monitoring.
- **Alert_Manager**: The component that sends notifications (email, Slack, PagerDuty, or webhook) when thresholds are breached.
- **Config_Service**: The component that loads and validates runtime configuration from environment variables and a config file, including secrets from a secrets manager.
- **API_Server**: The HTTP server exposing a read-only REST API consumed by the Dashboard and external health checks.
- **Dashboard**: The browser-based web UI that displays live system status, positions, P&L, opportunities, trades, risk metrics, and data feed health in real time.
- **Opportunity**: A detected price mismatch where Kalshi or Polymarket prices signal that a Gemini Predictions market is mispriced, meeting minimum spread and confidence thresholds.
- **Gemini_Position**: A single directional order placed on Gemini Predictions as a result of an identified Opportunity.
- **Kalshi_Client**: A read-only Platform_Client that fetches market data from Kalshi for use as a price signal. No orders are placed on Kalshi.
- **Polymarket_Client**: A read-only Platform_Client that fetches market data from Polymarket for use as a price signal. No orders are placed on Polymarket.
- **Gemini_Client**: The Platform_Client responsible for both fetching market data from and placing orders on Gemini Predictions.
- **Spread**: The absolute difference between the YES prices of a matched event pair across two platforms.
- **Spread_Pct**: The spread expressed as a percentage of the lower YES price.
- **Match_Confidence**: A 0.0–1.0 score produced by the Matcher indicating how likely two events are equivalent.
- **Risk_Score**: A 0.0–1.0 composite score produced by the Engine reflecting liquidity, time-to-resolution, and confidence risk.
- **Drawdown**: The percentage decline in total portfolio value from its peak.
- **Kelly_Fraction**: The position size fraction derived from the Kelly Criterion applied to the spread and win probability.
- **Dry_Run**: An execution mode where all trade logic runs but no real orders are submitted to any platform.
- **Live_Mode**: An execution mode where real orders are submitted to the Gemini Predictions API.
- **Scan_Cycle**: One complete pass of fetching, matching, scoring, and (optionally) executing across all configured platforms.
- **Platform_Client**: A module responsible for communicating with a single prediction market platform API (Kalshi, Polymarket, or Gemini Predictions).
- **Nginx_Proxy**: The nginx reverse proxy service in the docker-compose stack that terminates HTTPS on port 443 and forwards requests to the Dashboard and API_Server.
- **IAM_Instance_Profile**: The AWS IAM role attached to the EC2 instance, used to authenticate all AWS API calls (Secrets Manager, CloudWatch Logs) without static credentials.
- **Slow_Loop**: The periodic task (default every 300s) that fetches full market lists from all platforms and runs event matching to discover new Kalshi/Polymarket ↔ Gemini pairs.
- **Fast_Loop**: The periodic task (default every 30s) that fetches fresh orderbook snapshots for all active matched pairs and re-evaluates entry conditions.
- **Orderbook_Snapshot**: A point-in-time record of a market's best bid, best ask, mid-price, and depth captured from a platform's orderbook endpoint.
- **Reference_Price**: The volume-weighted average of Kalshi and Polymarket mid-prices for a matched event, used as the consensus signal against which Gemini's price is compared.
- **Price_Freshness**: The age in seconds of the most recently fetched orderbook snapshot for a given market. Prices older than `MAX_PRICE_AGE_SECONDS` are considered stale and must not be used for trade decisions.
- **Gemini_Depth**: The total USD value of resting orders within 3 cents of the best ask on the Gemini orderbook for the side being bought. Used to verify sufficient liquidity before sizing a position.
- **Position_Monitor**: The component that periodically checks open Gemini_Positions for convergence exit and stop-loss conditions.
- **Exit_Strategy**: The exit plan assigned to a Gemini_Position at entry time: either `hold_to_resolution` (wait for event settlement) or `target_convergence` (exit early when Gemini price converges toward the reference price).
- **Stop_Loss**: An automatic exit triggered when the Gemini mid-price moves more than `STOP_LOSS_PCT` against the position's entry price, capping the maximum loss per position.
- **Quarter_Kelly**: The position sizing convention used by the Engine: the full Kelly fraction multiplied by 0.25 to account for model uncertainty in the reference price estimate.

---

## Requirements

### Requirement 1: Persistent State and Trade Ledger

**User Story:** As an operator, I want all opportunities, trades, and P&L to be persisted to a database, so that I can audit history, resume after restarts, and analyze performance over time.

#### Acceptance Criteria

1. THE State_Store SHALL persist every detected Opportunity with its ID, detected timestamp, event titles, platform identifiers, reference_price, YES prices, spread, spread_pct, match_confidence, risk_score, days_to_resolution, signal_disagreement flag, and status.
2. THE State_Store SHALL persist every Gemini_Position with its opportunity ID, instrument_symbol, side, amount_usd, entry_price, quantity, exit_strategy, target_exit_price, stop_loss_price, status, executed_at timestamp, exit_reason, and resolved P&L.
3. WHEN the System restarts, THE State_Store SHALL restore all open Gemini_Positions so the Position_Monitor and Executor can continue monitoring and closing them.
4. WHEN a Gemini_Position resolves or exits, THE State_Store SHALL record the resolved_at timestamp, exit_price, exit_reason (`resolution` | `convergence` | `stop_loss`), and final P&L for that position.
5. THE State_Store SHALL persist Orderbook_Snapshots for each active matched pair, recording platform, market_ticker, best_bid, best_ask, yes_mid, depth_5pct, volume_24h, and fetched_at timestamp.
6. WHEN a database write fails, THE State_Store SHALL retry the write up to 3 times with exponential backoff before logging a critical error and continuing operation.
7. THE State_Store SHALL expose a query interface that returns aggregate P&L, win rate, average spread captured, exit reason breakdown, and total trades executed for any given time window.

---

### Requirement 2: Structured Logging

**User Story:** As an operator, I want all system events emitted as structured (JSON) log records with consistent fields, so that I can ingest logs into any log aggregation system and query them reliably.

#### Acceptance Criteria

1. THE System SHALL emit all log records in JSON format with at minimum the fields: `timestamp` (ISO 8601), `level`, `component`, `event`, and `message`.
2. WHEN an Opportunity is detected, THE System SHALL emit a log record at INFO level containing the opportunity ID, platform pair, spread_pct, match_confidence, and risk_score.
3. WHEN a Gemini_Position is submitted, THE System SHALL emit a log record at INFO level containing the trade ID, event ID, side, amount_usd, and entry price.
4. WHEN a platform API call fails, THE System SHALL emit a log record at WARNING level containing the platform name, endpoint, HTTP status code (if available), and error message.
5. WHEN a critical error occurs that prevents a Scan_Cycle from completing, THE System SHALL emit a log record at ERROR level containing the component name, error type, and stack trace.
6. THE System SHALL support configurable log levels (DEBUG, INFO, WARNING, ERROR) set via the Config_Service without requiring a restart.
7. THE System SHALL write logs to stdout and optionally to a rotating file, with maximum file size and retention period configurable via the Config_Service.

---

### Requirement 3: Metrics and Observability

**User Story:** As an operator, I want the system to expose Prometheus-compatible metrics, so that I can build dashboards and set up alerting on key operational and financial indicators.

#### Acceptance Criteria

1. THE Metrics_Exporter SHALL expose a `/metrics` HTTP endpoint in Prometheus text exposition format.
2. THE Metrics_Exporter SHALL emit a counter `arb_scan_cycles_total` incremented on every completed Scan_Cycle.
3. THE Metrics_Exporter SHALL emit a counter `arb_opportunities_detected_total` labeled by `platform_pair` incremented for each Opportunity that passes the minimum spread and confidence filters.
4. THE Metrics_Exporter SHALL emit a counter `arb_trades_executed_total` labeled by `platform` and `side` incremented for each order submitted.
5. THE Metrics_Exporter SHALL emit a gauge `arb_open_positions` reflecting the current count of open Gemini_Positions.
6. THE Metrics_Exporter SHALL emit a gauge `arb_available_capital_usd` reflecting the current available capital in USD.
7. THE Metrics_Exporter SHALL emit a histogram `arb_scan_duration_seconds` recording the wall-clock time of each Scan_Cycle.
8. THE Metrics_Exporter SHALL emit a histogram `arb_platform_api_latency_seconds` labeled by `platform` and `endpoint` recording the latency of each Platform_Client API call.
9. THE Metrics_Exporter SHALL emit a gauge `arb_realized_pnl_usd` reflecting cumulative realized P&L since system start.
10. WHEN the `/metrics` endpoint is unavailable, THE System SHALL continue normal operation and log a WARNING.

---

### Requirement 4: Risk Management and Kill Switch

**User Story:** As an operator, I want automated risk controls that halt trading when predefined thresholds are breached, so that the system cannot lose more than an acceptable amount without human intervention.

#### Acceptance Criteria

1. THE Risk_Manager SHALL prevent the Executor from opening a new Gemini_Position WHEN the number of open Gemini_Positions equals or exceeds the configured `MAX_POSITIONS` limit (default: 10).
2. THE Risk_Manager SHALL prevent the Executor from sizing any single Gemini_Position above the configured `MAX_POSITION_PCT` of available capital (default: 5%).
3. WHEN the total portfolio drawdown from peak exceeds the configured `MAX_DRAWDOWN_PCT` (default: 20%), THE Risk_Manager SHALL suspend all new trade execution and emit an ALERT.
4. WHEN a single Scan_Cycle produces more than the configured `MAX_OPPORTUNITIES_PER_SCAN` actionable opportunities (default: 50), THE Risk_Manager SHALL cap execution at that limit and log a WARNING, treating the excess as a potential data anomaly.
5. WHEN the Gemini_Client returns an error on an order placement, THE Risk_Manager SHALL mark the corresponding Gemini_Position as `failed`, log the failure at ERROR level, and preserve available capital by not deducting the failed order amount.
6. THE Risk_Manager SHALL enforce a minimum spread threshold of `MIN_SPREAD_PCT` (default: 8%) and a minimum match confidence of `MIN_CONFIDENCE` (default: 0.70) before any opportunity is passed to the Executor.
7. THE Risk_Manager SHALL reject any opportunity where the Orderbook_Snapshot for either the reference platform or Gemini is older than `MAX_PRICE_AGE_SECONDS` (default: 60s), logging the rejection as `stale_price`.
8. THE Risk_Manager SHALL reject any opportunity where the Gemini_Depth for the intended buy side is less than `MIN_GEMINI_DEPTH_USD` (default: $50), logging the rejection as `insufficient_liquidity`.
9. THE Risk_Manager SHALL reject any opportunity where the spread falls entirely within Gemini's own bid-ask spread, logging the rejection as `spread_inside_noise`.
10. WHEN the Risk_Manager suspends trading due to drawdown, THE System SHALL require an explicit operator action (config flag reset or API call) to resume Live_Mode execution.
11. THE Risk_Manager SHALL log every risk decision (allow, deny, suspend) at INFO level with the reason and relevant metrics.

---

### Requirement 5: Platform Client Resilience

**User Story:** As an operator, I want each Platform_Client to handle transient API failures gracefully, so that a single platform outage does not crash the system or cause missed opportunities on other platforms.

#### Acceptance Criteria

1. WHEN a Platform_Client HTTP request times out, THE Platform_Client SHALL retry the request up to 3 times with exponential backoff starting at 1 second before returning an empty result.
2. WHEN a Platform_Client receives an HTTP 429 (rate limit) response, THE Platform_Client SHALL wait for the duration specified in the `Retry-After` header (or 60 seconds if absent) before retrying.
3. WHEN a Platform_Client receives an HTTP 5xx response, THE Platform_Client SHALL retry up to 3 times with exponential backoff before returning an empty result and logging a WARNING.
4. WHEN a Platform_Client fails to return data for a Scan_Cycle, THE Scanner SHALL continue the Scan_Cycle using data from the remaining available platforms and log a WARNING identifying the unavailable platform.
5. THE Kalshi_Client SHALL be read-only: THE Kalshi_Client SHALL implement RSA-based request signing only for authenticated market data endpoints and SHALL NOT submit any orders to Kalshi.
6. THE Polymarket_Client SHALL be read-only: THE Polymarket_Client SHALL fetch market prices via the public Polymarket API and SHALL NOT submit any orders to Polymarket.
7. THE Gemini_Client SHALL implement HMAC-SHA384 request signing for all authenticated endpoints, including order placement, using the configured API key and secret.
8. WHEN a Platform_Client's authentication token expires, THE Platform_Client SHALL automatically re-authenticate before the next request without requiring a system restart.
9. THE System SHALL track per-platform consecutive failure counts and emit a WARNING when any platform exceeds 5 consecutive failures within a single Scan_Cycle window.

---

### Requirement 6: Configuration Management and Secrets

**User Story:** As an operator, I want all configuration and secrets loaded from environment variables or a secrets manager, so that no credentials are ever stored in source code or config files committed to version control.

#### Acceptance Criteria

1. THE Config_Service SHALL load all configuration values from environment variables at startup, with documented defaults for every non-secret parameter.
2. THE Config_Service SHALL use AWS Secrets Manager as the primary secret backend when deployed on EC2, with HashiCorp Vault supported as an alternative backend, selectable via the `SECRET_BACKEND` environment variable (values: `aws`, `vault`, `env`; default: `aws`).
3. WHEN `SECRET_BACKEND` is set to `aws`, THE Config_Service SHALL authenticate to AWS Secrets Manager using the EC2 instance's IAM instance profile role, with no hardcoded AWS credentials or access keys permitted in the configuration or image.
4. WHEN a required secret (API key or private key) is missing at startup, THE Config_Service SHALL log a CRITICAL error identifying the missing variable and exit with a non-zero status code.
5. WHEN an optional configuration value is missing, THE Config_Service SHALL use the documented default and log an INFO record indicating the default was applied.
6. THE Config_Service SHALL validate that numeric configuration values (MIN_SPREAD_PCT, MAX_POSITIONS, CAPITAL, etc.) are within defined safe ranges and exit with a non-zero status code if any value is out of range.
7. THE System SHALL never log the value of any secret (API key, private key, wallet key) at any log level.
8. WHEN `SECRET_BACKEND` is set to `aws`, THE Config_Service SHALL refresh secrets from AWS Secrets Manager every 3600 seconds without requiring a restart.

---

### Requirement 7: Event Matching Quality and Caching

**User Story:** As an operator, I want the Matcher to cache results and avoid redundant LLM calls, so that API costs are controlled and scan latency is minimized.

#### Acceptance Criteria

1. THE Matcher SHALL cache match results keyed by a deterministic hash of the two event titles and resolution dates, with a configurable TTL (default: 3600 seconds).
2. WHEN a cached match result exists and has not expired, THE Matcher SHALL return the cached result without making an LLM API call.
3. WHEN the LLM provider returns an error or times out after 10 seconds, THE Matcher SHALL fall back to rule-based matching for that pair and log a WARNING.
4. THE Matcher SHALL expose a cache hit rate metric `arb_matcher_cache_hit_rate` as a gauge updated after each Scan_Cycle.
5. THE Matcher SHALL support a configurable `MATCHER_BACKEND` of `rule_based`, `openai`, or `anthropic`, with `rule_based` as the default.
6. WHEN the `rule_based` backend is active, THE Matcher SHALL produce a match result for any event pair within 100 milliseconds.
7. THE Matcher SHALL parse and compare price levels, asset symbols, direction keywords (above/below), and resolution dates as independent scoring dimensions, each contributing a documented weight to the final confidence score.
8. FOR ALL event pairs where the rule-based Matcher returns a confidence score, parsing the event title then re-running the Matcher on the same title SHALL produce an equivalent confidence score (round-trip stability).

---

### Requirement 8: Deployment and Containerization

**User Story:** As an operator, I want the system packaged as a Docker container with a compose file, so that I can deploy it on a single EC2 instance with a single command and have it survive reboots and container restarts.

#### Acceptance Criteria

1. THE System SHALL be packaged as a Docker image with a multi-stage build that produces a minimal runtime image under 200 MB.
2. THE System SHALL expose a `/healthz` HTTP endpoint that returns HTTP 200 with a JSON body `{"status": "ok"}` when the system is running normally, compatible with EC2 Target Group health checks when the instance is placed behind an Application Load Balancer.
3. WHEN the system is unhealthy (e.g., database unreachable, all platforms failing), THE `/healthz` endpoint SHALL return HTTP 503 with a JSON body describing the failing component.
4. THE System SHALL include a `docker-compose.yml` that starts the bot, a PostgreSQL instance, a Prometheus instance with a pre-configured scrape config, and an nginx reverse proxy service that terminates HTTPS on port 443 and proxies the Dashboard and API_Server.
5. THE `docker-compose.yml` SHALL define a named Docker volume for PostgreSQL data persistence so that database contents survive container restarts and image upgrades; on EC2 this volume is backed by the instance's EBS root volume by default.
6. THE System SHALL support graceful shutdown: WHEN a SIGTERM signal is received, THE System SHALL complete the current Scan_Cycle, close all open HTTP connections, flush pending log records, and exit within 30 seconds.
7. THE System SHALL be configurable entirely via environment variables passed to the container, with no config files required inside the image.
8. THE System SHALL include a Dockerfile and compose file that pass a `docker build` and `docker compose up` without manual intervention beyond providing a `.env` file.
9. THE System SHALL emit all log output to stdout in JSON format so that logs are automatically captured by the Docker logging driver and forwarded to CloudWatch Logs when the `awslogs` Docker log driver is configured.
10. THE System SHALL include a documented EC2 bootstrap procedure (user-data script or equivalent setup guide) covering: Docker installation, repository clone, `.env` population from AWS Secrets Manager, and `docker compose up -d` invocation.

---

### Requirement 9: Alerting and Notifications

**User Story:** As an operator, I want the system to send alerts when important events occur, so that I can respond quickly to trading anomalies, errors, or opportunities without watching logs continuously.

#### Acceptance Criteria

1. THE Alert_Manager SHALL support at minimum one notification channel configurable via environment variable: `ALERT_CHANNEL` with values `slack`, `email`, `webhook`, or `none` (default: `none`).
2. WHEN the Risk_Manager suspends trading due to drawdown, THE Alert_Manager SHALL send an alert within 60 seconds containing the current drawdown percentage and available capital.
3. WHEN a platform is unavailable for more than 3 consecutive Scan_Cycles, THE Alert_Manager SHALL send an alert identifying the platform and the duration of unavailability.
4. WHEN a Gemini_Position fails to execute, THE Alert_Manager SHALL send an alert immediately containing the event ID, side, amount at risk, and the error returned by the Gemini_Client.
5. WHEN the system detects an Opportunity with a spread_pct above a configurable `ALERT_SPREAD_THRESHOLD` (default: 20%), THE Alert_Manager SHALL send an alert containing the opportunity details.
6. THE Alert_Manager SHALL deduplicate alerts of the same type within a configurable `ALERT_DEDUP_WINDOW` (default: 300 seconds) to prevent alert storms.
7. IF the Alert_Manager fails to deliver a notification, THEN THE Alert_Manager SHALL log the failure at WARNING level and continue normal operation without retrying more than once.

---

### Requirement 10: Backtesting and Simulation Mode

**User Story:** As a developer, I want a backtesting mode that replays historical opportunity data against the execution logic, so that I can validate risk parameters and strategy changes before deploying them live.

#### Acceptance Criteria

1. THE System SHALL support a `--backtest` mode that reads historical Opportunity records from the State_Store and replays them through the Engine and Risk_Manager without making any platform API calls.
2. WHEN running in backtest mode, THE Executor SHALL simulate Gemini_Position fills at the recorded prices and compute P&L using the same Kelly sizing and risk rules as Live_Mode.
3. WHEN backtest mode completes, THE System SHALL output a summary report containing: total opportunities replayed, trades simulated, gross P&L, net P&L after a configurable fee model, win rate, max drawdown, and Sharpe ratio.
4. THE backtest summary report SHALL be emitted as both a structured JSON record to stdout and as a human-readable table to stderr.
5. FOR ALL backtest runs on the same historical dataset with the same configuration, THE System SHALL produce identical P&L and trade count results (determinism property).

---

### Requirement 11: API Server

**User Story:** As an operator, I want a lightweight HTTP API that exposes current system state and streams real-time updates, so that the Dashboard and external tools can display live data without polling.

#### Acceptance Criteria

1. THE API_Server SHALL expose a `GET /api/v1/status` endpoint returning a JSON object with: mode (dry_run/live), uptime_seconds, scan_count, open_positions, available_capital_usd, realized_pnl_usd, and last_scan_at.
2. THE API_Server SHALL expose a `GET /api/v1/opportunities` endpoint returning the current list of actionable opportunities with all scored fields.
3. THE API_Server SHALL expose a `GET /api/v1/trades` endpoint returning paginated trade history from the State_Store, supporting `limit` and `offset` query parameters.
4. THE API_Server SHALL expose a `GET /api/v1/portfolio` endpoint returning the current portfolio summary including open Gemini_Positions, capital utilization, and per-position P&L.
5. THE API_Server SHALL expose a `GET /api/v1/pnl/history` endpoint returning time-series P&L data points from the State_Store, supporting `from` and `to` ISO 8601 timestamp query parameters.
6. THE API_Server SHALL expose a `GET /api/v1/feeds/health` endpoint returning the current connectivity status, last successful fetch timestamp, and consecutive failure count for each Platform_Client (Kalshi, Polymarket, Gemini).
7. THE API_Server SHALL expose a `GET /api/v1/events` Server-Sent Events endpoint that pushes real-time updates to connected Dashboard clients whenever a new Opportunity is detected, a Gemini_Position is opened or closed, or a risk threshold is breached.
8. THE API_Server SHALL be read-only for all non-SSE endpoints: WHEN a mutating HTTP method (POST, PUT, DELETE, PATCH) is received on any REST endpoint, THE API_Server SHALL return HTTP 405.
9. THE API_Server SHALL require a configurable bearer token (`API_SERVER_TOKEN`) for all requests and return HTTP 401 when the token is absent or incorrect.
10. THE API_Server SHALL set CORS headers permitting requests from the configured `DASHBOARD_ORIGIN` so that the Dashboard can be served from a different port or domain.
11. WHEN the API_Server is disabled via configuration (`API_SERVER_ENABLED=false`), THE System SHALL start normally without binding any HTTP port for the API.

---

### Requirement 12: Web Dashboard

**User Story:** As an operator, I want a browser-based dashboard that displays live system status, positions, P&L, and feed health, so that I can monitor the system at a glance without reading logs or querying APIs manually.

#### Acceptance Criteria

1. THE Dashboard SHALL be a browser-accessible web UI served over HTTPS, deployable as a service within the existing docker-compose stack alongside the bot, API_Server, database, and nginx reverse proxy.
2. THE Dashboard SHALL be accessible via the EC2 instance's public IP address or a configured domain name over HTTPS (port 443), proxied by the nginx service in the compose stack.
3. THE nginx service SHALL terminate SSL/TLS using either a self-signed certificate (for internal or development use) or a Let's Encrypt certificate obtained via certbot, with the certificate path configurable via environment variable.
4. THE Dashboard SHALL display a live system status panel showing: current mode (Dry_Run / Live_Mode), uptime, scan count, last scan timestamp, and available capital.
5. THE Dashboard SHALL display a current open positions table showing all open Gemini_Positions with columns for event title, side, entry price, amount_usd, quantity, and unrealized P&L.
6. THE Dashboard SHALL display a P&L over time chart rendered as a line chart, sourced from the `GET /api/v1/pnl/history` endpoint, with a configurable time range selector (1h, 24h, 7d, 30d).
7. THE Dashboard SHALL display a recent opportunities panel listing the last 20 detected Opportunities with their spread_pct, match_confidence, risk_score, signal sources (Kalshi and/or Polymarket), and status.
8. THE Dashboard SHALL display a recent trades panel listing the last 20 executed Gemini_Positions with their event title, side, amount_usd, entry price, and resolved P&L (if closed).
9. THE Dashboard SHALL display a risk metrics panel showing: current drawdown percentage, capital utilization percentage, and whether the Risk_Manager has suspended trading.
10. THE Dashboard SHALL display a data feed health panel showing the connectivity status, last successful fetch time, and consecutive failure count for each Platform_Client (Kalshi, Polymarket, Gemini).
11. WHEN the API_Server pushes a Server-Sent Event, THE Dashboard SHALL update the affected panel within 2 seconds without requiring a full page reload.
12. THE Dashboard SHALL poll the `GET /api/v1/status` endpoint at a configurable interval (default: 10 seconds) as a fallback when the SSE connection is unavailable.
13. IF the SSE connection to the API_Server is lost, THEN THE Dashboard SHALL display a visible reconnecting indicator and attempt to reconnect with exponential backoff up to a maximum interval of 30 seconds.
14. THE Dashboard SHALL be implemented as a lightweight frontend (React or Next.js) that consumes the API_Server endpoints, with no separate backend process required beyond the API_Server.
15. THE Dashboard service SHALL be defined in the `docker-compose.yml` and SHALL be reachable via the nginx reverse proxy on port 443; direct access to the Dashboard's internal port SHALL be restricted to localhost within the compose network.
16. WHERE `API_SERVER_TOKEN` is configured, THE Dashboard SHALL include the bearer token in all API_Server requests using a token provided via a `NEXT_PUBLIC_API_TOKEN` environment variable, and SHALL NOT expose the token in client-side source code beyond what is necessary for browser-side API calls.

---

### Requirement 13: AWS Infrastructure and Security

**User Story:** As an operator, I want the EC2 deployment to follow AWS security best practices, so that the system's attack surface is minimized and credentials are never exposed.

#### Acceptance Criteria

1. THE EC2 instance SHALL be assigned an IAM instance profile whose attached policy grants only the minimum permissions required: `secretsmanager:GetSecretValue` and `secretsmanager:DescribeSecret` on the specific secret ARNs used by the system, and `logs:CreateLogGroup`, `logs:CreateLogStream`, and `logs:PutLogEvents` on the target CloudWatch log group.
2. THE System SHALL include an example IAM policy document (JSON) and an example EC2 security group configuration in the repository, documenting the exact permissions and port rules required.
3. THE EC2 security group SHALL permit inbound traffic on ports 80 and 443 from `0.0.0.0/0`; all other ports (including PostgreSQL 5432, Prometheus 9090, and the API_Server internal port) SHALL be restricted to localhost (`127.0.0.1/32`) or the VPC CIDR, with no public inbound rules.
4. WHERE `LOG_DESTINATION` is set to `cloudwatch`, THE System SHALL forward structured JSON log output to the configured AWS CloudWatch Logs log group using the Docker `awslogs` log driver, with no additional log agent required.
5. THE EBS volume backing the EC2 instance SHALL be configured with encryption at rest enabled; the repository's setup documentation SHALL note this as a required configuration step when launching the instance.
6. THE System SHALL never store AWS access keys or secret keys on disk or in environment variables on the EC2 instance; all AWS API calls SHALL be authenticated exclusively via the IAM instance profile credential chain.

---

### Requirement 14: LLM-Based Event Matching

**User Story:** As an operator, I want the event matcher to accurately and efficiently identify when a Kalshi or Polymarket event represents the same outcome as a Gemini Predictions market, so that the system only trades on real mispricings and not on incorrectly matched events.

#### Acceptance Criteria

1. THE Matcher SHALL implement a three-stage pipeline: rule-based pre-filter, structured field extraction, and LLM semantic judgment — where the LLM is only invoked for event pairs whose rule-based score falls in the ambiguous range `[0.40, 0.75)`.
2. THE Matcher SHALL score each event pair across four independent dimensions — asset symbol (weight 0.30), price level (weight 0.35), direction (weight 0.15), and resolution date (weight 0.20) — and document the weight for each dimension.
3. WHEN the rule-based score is `< 0.40`, THE Matcher SHALL reject the pair immediately without making any LLM API call.
4. WHEN the rule-based score is `>= 0.75`, THE Matcher SHALL accept the pair as a match without making any LLM API call.
5. THE Matcher SHALL support `openai` (`gpt-4o-mini`) and `anthropic` (`claude-3-haiku`) as LLM backends, selectable via `MATCHER_BACKEND` config, with `rule_based` as the default when no API key is provided.
6. THE Matcher SHALL detect and correctly handle inverted event pairs — where one platform phrases the event as YES=above and the other as YES=below — by setting an `inverted` flag that the Engine uses to flip the reference price direction before computing the spread.
7. WHEN an LLM call times out after 10 seconds or returns malformed JSON, THE Matcher SHALL fall back to the rule-based result for that pair, log a WARNING, and continue without raising an exception.
8. THE Matcher SHALL persist match results to a `match_cache` PostgreSQL table with a configurable TTL (default 3600s), and warm-load the in-memory cache from this table on startup to avoid a cold-start LLM call wave.
9. THE Matcher SHALL apply an asset pre-filter before scoring: if both events have a detected asset and those assets differ, the pair SHALL be rejected without scoring, reducing unnecessary comparisons.
10. THE Matcher SHALL cap concurrent LLM calls within a single batch at `MAX_CONCURRENT_LLM_CALLS` (default 5) using an asyncio semaphore, to avoid exceeding provider rate limits.
11. THE Matcher SHALL emit a `arb_matcher_cache_hit_rate` Prometheus gauge after each batch, computed as `hits / (hits + misses)` over that batch.
12. THE Matcher SHALL emit a `arb_matcher_llm_calls_total` Prometheus counter incremented for each LLM API call made, labeled by `backend` and `outcome` (`success` | `timeout` | `parse_error`).
13. FOR ALL event pairs where the rule-based Matcher returns a confidence score, calling `match()` twice with the same inputs SHALL produce identical `confidence` and `equivalent` values regardless of cache state (determinism property).
14. FOR ALL matched pairs returned by `batch_match(min_confidence=t)`, the `result.confidence` SHALL be `>= t` (no under-threshold pairs returned).
15. THE Matcher SHALL use OpenAI function-calling (tool-use) mode rather than free-form JSON prompting when the `openai` backend is active, binding the response to a typed `match_event_pair` tool schema so that structured output is enforced by the API and JSON parsing errors are eliminated.
16. THE Matcher SHALL use Anthropic tool-use mode when the `anthropic` backend is active, with an equivalent `match_event_pair` tool schema, so that both backends produce identically structured output.
17. THE Matcher SHALL include the current live orderbook context — Kalshi yes_mid, Polymarket yes_mid, and Gemini yes_mid for the candidate pair — in the LLM prompt when those prices are available, so the model can use price convergence as an additional signal for equivalence.
18. THE Matcher SHALL implement a `MatchingToolRegistry` that registers extraction helper functions (`extract_asset`, `extract_price_level`, `extract_direction`, `extract_date`) as callable tools available to the LLM, allowing the model to invoke them on ambiguous titles rather than guessing.
19. WHEN the LLM invokes a registered extraction tool during a match call, THE Matcher SHALL execute the tool synchronously, return the result to the LLM in the next turn, and complete the match within the same 10-second timeout budget.
20. THE Matcher SHALL log every LLM tool invocation at DEBUG level, including the tool name, input arguments, and return value, to support prompt debugging and cost auditing.

---

### Requirement 15: Multi-Platform Orderbook Data Collection

**User Story:** As an operator, I want the system to routinely fetch and store full orderbook snapshots from Kalshi and Polymarket for all active matched markets, so that the reference price is always based on current, liquid data rather than stale mid-prices from market list endpoints.

#### Acceptance Criteria

1. THE PricePoller SHALL fetch full orderbook depth from Kalshi (`GET /trade-api/v2/markets/{ticker}/orderbook`) for every active matched Kalshi market on every fast-loop tick (default every 30s), not just the mid-price from the market list endpoint.
2. THE PricePoller SHALL fetch full orderbook depth from Polymarket (`POST https://clob.polymarket.com/books`) in a single batched request covering all active matched Polymarket token IDs on every fast-loop tick, batching up to 500 token IDs per request.
3. THE PricePoller SHALL fetch the Gemini orderbook (`GET /v1/book/{symbol}`) for every active matched Gemini market on every fast-loop tick, in addition to the continuous WebSocket top-of-book stream.
4. THE System SHALL persist every fetched orderbook as an `OrderbookSnapshot` record in the database, capturing: platform, market_ticker, best_bid, best_ask, yes_mid, depth_5pct (contracts within 5¢ of best bid), depth_3pct_usd (USD value within 3¢ of best ask on Gemini), volume_24h, and fetched_at timestamp.
5. THE PricePoller SHALL compute `depth_5pct` for Kalshi orderbooks by summing contract quantities for all YES bid levels within 5¢ of the best YES bid, using the `orderbook_fp` response field.
6. THE PricePoller SHALL compute `depth_5pct` for Polymarket orderbooks by summing contract sizes for all bid levels within 5¢ of the best bid from the CLOB `/books` response.
7. THE PricePoller SHALL compute `depth_3pct_usd` for Gemini orderbooks by summing `price * quantity` for all ask levels within 3¢ of the best ask, converting to USD.
8. THE System SHALL maintain an in-memory `OrderbookCache` keyed by `(platform, ticker)` holding the most recent `OrderbookSnapshot` for each active market, so that the Engine can access current prices without a database read on every scoring pass.
9. WHEN a Kalshi or Polymarket orderbook fetch fails for a specific market, THE PricePoller SHALL log a WARNING for that market, retain the previous snapshot in the cache with its original `fetched_at` timestamp, and continue polling other markets.
10. THE System SHALL optionally subscribe to Kalshi's WebSocket `orderbook_delta` channel for active matched markets when `KALSHI_WS_ENABLED=true`, applying incremental delta updates to the in-memory orderbook state between REST poll cycles to reduce REST API load.
11. THE System SHALL optionally subscribe to Polymarket's WebSocket `book` channel for active matched markets when `POLYMARKET_WS_ENABLED=true`, applying incremental updates to the in-memory orderbook state between REST poll cycles.
12. THE System SHALL track per-platform orderbook fetch latency as a `arb_orderbook_fetch_duration_seconds` histogram labeled by `platform`, and emit a WARNING when any platform's p95 fetch latency exceeds 5 seconds over a rolling 5-minute window.
13. THE Engine SHALL reject any opportunity where the `OrderbookSnapshot.fetched_at` for any required platform is older than `MAX_PRICE_AGE_SECONDS` (default 60s), logging the rejection as `stale_orderbook`.
14. THE System SHALL expose a `GET /api/v1/orderbooks` endpoint returning the current in-memory orderbook snapshot for each active matched market across all platforms, for dashboard display and debugging.
15. FOR ALL active matched pairs, the `reference_price` used by the Engine SHALL be derived exclusively from the most recent `OrderbookSnapshot` mid-prices, never from the market list endpoint's `yes_price` field (which may be stale by up to `SCAN_INTERVAL_SECONDS`).
