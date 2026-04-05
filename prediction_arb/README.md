# Prediction Arbitrage Production System

A production-ready arbitrage bot for prediction markets (Kalshi, Polymarket, Gemini Predictions) with comprehensive backtesting, monitoring, and risk management.

## Features

- **Multi-Platform Arbitrage**: Monitors Kalshi, Polymarket, and Gemini Predictions for price discrepancies
- **Intelligent Event Matching**: Rule-based and LLM-powered (GPT-4o-mini, Claude Haiku) event matching
- **Risk Management**: Position limits, drawdown protection, Kelly sizing, stop-loss, convergence exits
- **Backtesting**: Replay historical opportunities to validate strategy before live trading
- **Real-Time Monitoring**: FastAPI server with SSE streaming, Prometheus metrics, Next.js dashboard
- **Production-Ready**: Docker Compose deployment, AWS integration, structured logging, alerting

## Quick Start

### 1. Backtest (Recommended First)

```bash
# Start PostgreSQL
docker run -d --name arb-postgres \
  -e POSTGRES_DB=arbdb -e POSTGRES_USER=arb -e POSTGRES_PASSWORD=changeme \
  -p 5432:5432 postgres:16-alpine

# Install and run
cd prediction_arb
pip install -e .
export DATABASE_URL="postgresql+asyncpg://arb:changeme@localhost:5432/arbdb"
alembic upgrade head
python -m prediction_arb.bot.main --backtest
```

See [QUICKSTART.md](QUICKSTART.md) for detailed instructions.

### 2. Local Deployment

```bash
# Configure
cd prediction_arb
cp .env.template .env
# Edit .env with your API keys

# Start services
cd infra
docker compose up -d

# Access dashboard
open http://localhost
```

### 3. Production Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete AWS deployment guide.

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** - Get started in 5 minutes
- **[DEPLOYMENT.md](DEPLOYMENT.md)** - Complete deployment and operations guide
- **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)** - Pre-deployment validation checklist
- **[.env.template](.env.template)** - All configuration options with descriptions

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Prediction Arbitrage Bot                 │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Scanner    │  │ EventMatcher │  │    Engine    │      │
│  │ (slow loop)  │→ │  (LLM/rule)  │→ │   (Kelly)    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│         ↓                                      ↓             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ PricePoller  │→ │ RiskManager  │→ │   Executor   │      │
│  │ (fast loop)  │  │  (limits)    │  │  (orders)    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│         ↓                                      ↓             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │PositionMonitor│ │  StateStore  │  │ AlertManager │      │
│  │ (exits)      │  │ (Postgres)   │  │ (Slack/email)│      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                               │
├─────────────────────────────────────────────────────────────┤
│                      API Server (FastAPI)                    │
│  /healthz  /api/v1/status  /api/v1/opportunities            │
│  /api/v1/trades  /api/v1/portfolio  /api/v1/events (SSE)    │
├─────────────────────────────────────────────────────────────┤
│                    Dashboard (Next.js 14)                    │
│  Status  P&L Chart  Positions  Opportunities  Feed Health   │
└─────────────────────────────────────────────────────────────┘
```

## System Components

### Core Components

- **Scanner**: Fetches market lists from all platforms every 5 minutes (configurable)
- **EventMatcher**: Matches events across platforms using rule-based or LLM matching
- **PricePoller**: Fetches orderbook data every 30 seconds (configurable)
- **ArbitrageEngine**: Scores opportunities using Kelly criterion and risk metrics
- **RiskManager**: Enforces position limits, spread thresholds, drawdown protection
- **Executor**: Places orders on Gemini (dry-run or live mode)
- **PositionMonitor**: Monitors open positions for stop-loss and convergence exits

### Platform Clients

- **KalshiClient**: Read-only access to Kalshi markets and orderbooks
- **PolymarketClient**: Read-only access to Polymarket markets and CLOB orderbooks
- **GeminiClient**: Full read/write access to Gemini Predictions (orders, positions)

### Infrastructure

- **StateStore**: PostgreSQL database with SQLAlchemy ORM and Alembic migrations
- **MetricsExporter**: Prometheus metrics for monitoring
- **AlertManager**: Slack/email/webhook alerts for critical events
- **SSEBroadcaster**: Server-sent events for real-time dashboard updates
- **API Server**: FastAPI REST API with bearer token authentication

## Configuration

Key environment variables (see `.env.template` for full list):

```bash
# Trading mode
DRY_RUN=true                    # Set to false for live trading

# Capital and risk
CAPITAL=1000.0                  # Total capital in USD
MIN_SPREAD_PCT=0.08             # Minimum 8% spread
MAX_POSITIONS=10                # Max concurrent positions
MAX_POSITION_PCT=0.05           # Max 5% per position
MAX_DRAWDOWN_PCT=0.20           # Max 20% drawdown before suspension

# Matching
MATCHER_BACKEND=rule_based      # Options: rule_based, openai, anthropic
MIN_CONFIDENCE=0.70             # Minimum match confidence (0.0-1.0)

# Secrets
GEMINI_API_KEY=                 # Required
GEMINI_API_SECRET=              # Required
API_SERVER_TOKEN=               # Required (generate random token)
```

## Backtesting

Backtest the system with historical data:

```bash
# Last 30 days (default)
python -m prediction_arb.bot.main --backtest

# Specific date range
python -m prediction_arb.bot.main --backtest \
  --from 2026-01-01 --to 2026-03-01

# Or use convenience script
./scripts/backtest.sh --from 2026-01-01 --to 2026-03-01
```

Output includes:
- Total opportunities and trades simulated
- Gross and net P&L
- Win rate
- Maximum drawdown
- Sharpe ratio

## Monitoring

### Health Check

```bash
curl http://localhost/healthz
```

### API Status

```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost/api/v1/status
```

### Prometheus Metrics

```bash
curl http://localhost/metrics
```

Key metrics:
- `arb_scan_cycles_total` - Total scan cycles completed
- `arb_opportunities_detected_total` - Opportunities detected
- `arb_trades_executed_total` - Trades executed
- `arb_open_positions` - Current open positions
- `arb_realized_pnl_usd` - Realized P&L in USD
- `arb_scan_duration_seconds` - Scan cycle duration

### Dashboard

Access the Next.js dashboard at `http://localhost` to view:
- Real-time system status
- P&L chart with time range selector
- Open positions table
- Recent opportunities
- Recent trades
- Feed health status

## Testing

### Unit Tests

```bash
cd prediction_arb
pytest tests/unit/ -v
```

### Integration Tests

```bash
# Start test database
docker run -d --name test-postgres \
  -e POSTGRES_DB=test_arb -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \
  -p 5433:5432 postgres:16-alpine

# Run tests
export DATABASE_URL="postgresql+asyncpg://test:test@localhost:5433/test_arb"
pytest tests/integration/ -v
```

### Property-Based Tests

```bash
pytest tests/property/ -v --hypothesis-show-statistics
```

## Deployment

### Local (Docker Compose)

```bash
cd prediction_arb
cp .env.template .env
# Edit .env with your API keys
cd infra
docker compose up -d
```

### Production (AWS EC2)

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete guide including:
- AWS Secrets Manager setup
- IAM instance profile creation
- Security group configuration
- EC2 instance launch
- TLS certificate setup
- CloudWatch logging
- Prometheus monitoring

## Security

- All secrets stored in AWS Secrets Manager (production) or `.env` (local)
- API authentication via bearer token
- TLS encryption for all external traffic
- `/metrics` endpoint restricted to VPC CIDR
- SSH access restricted to operator IP
- EBS volumes encrypted at rest
- No secrets in logs or version control

## Cost Estimation

### AWS (Monthly)

- EC2 t3.medium: ~$30
- EBS 30GB gp3: ~$3
- CloudWatch Logs: ~$2.50
- Secrets Manager: ~$2
- Data transfer: ~$5
- **Total**: ~$42.50/month

### API Costs

- Gemini: Free (no trading fees currently)
- Kalshi: Free (read-only public endpoints)
- Polymarket: Free (public API)
- OpenAI (optional): ~$0.15 per 1M tokens (GPT-4o-mini)
- Anthropic (optional): ~$0.25 per 1M tokens (Claude Haiku)

## Troubleshooting

### Bot won't start

```bash
docker compose logs bot
# Check for missing API keys, database connection, migration errors
```

### No opportunities detected

```bash
# Check feed health
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost/api/v1/feeds/health

# Lower confidence threshold
# Edit .env: MIN_CONFIDENCE=0.60
docker compose restart bot
```

### Trades not executing

```bash
# Check risk decisions
docker compose logs bot | grep "risk_decision"

# Common reasons:
# - Spread too small (increase MIN_SPREAD_PCT)
# - Low confidence (lower MIN_CONFIDENCE)
# - Position cap reached (increase MAX_POSITIONS)
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete troubleshooting guide.

## Development

### Project Structure

```
prediction_arb/
├── bot/                    # Core bot code
│   ├── api/               # FastAPI server and SSE
│   ├── clients/           # Platform API clients
│   ├── config.py          # Configuration and secrets loading
│   ├── engine.py          # Arbitrage scoring and Kelly sizing
│   ├── executor.py        # Order execution
│   ├── matcher.py         # Event matching (rule-based + LLM)
│   ├── monitor.py         # Position monitoring
│   ├── risk.py            # Risk management
│   ├── scanner.py         # Market scanning
│   ├── state.py           # Database state store
│   └── main.py            # Main entry point
├── dashboard/             # Next.js 14 dashboard
├── infra/                 # Infrastructure configs
│   ├── docker-compose.yml
│   ├── nginx/
│   ├── prometheus/
│   └── iam/
├── migrations/            # Alembic database migrations
├── tests/                 # Unit, integration, property tests
├── .env.template          # Environment variable template
├── Dockerfile             # Bot Docker image
└── pyproject.toml         # Python dependencies
```

### Adding a New Platform

1. Create client in `bot/clients/new_platform.py` extending `BaseClient`
2. Implement required methods: `get_markets()`, `get_orderbook()`
3. Add client to `Scanner` in `bot/scanner.py`
4. Add platform to `EventMatcher` in `bot/matcher.py`
5. Update tests in `tests/unit/test_clients.py`

### Adding a New Risk Check

1. Add check to `RiskManager.evaluate()` in `bot/risk.py`
2. Add corresponding test in `tests/unit/test_risk.py`
3. Add property-based test in `tests/property/test_risk_pbt.py`
4. Document in `.env.template` if configurable

## License

[Add your license here]

## Support

For issues or questions:
1. Check [DEPLOYMENT.md](DEPLOYMENT.md) troubleshooting section
2. Review logs: `docker compose logs -f bot`
3. Check metrics: `curl http://localhost/metrics`
4. Check health: `curl http://localhost/healthz`

## Acknowledgments

Built with:
- FastAPI - Modern Python web framework
- SQLAlchemy - SQL toolkit and ORM
- Prometheus - Monitoring and alerting
- Next.js - React framework for dashboard
- Docker - Containerization
- PostgreSQL - Database
- Hypothesis - Property-based testing
