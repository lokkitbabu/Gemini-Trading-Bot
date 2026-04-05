# Deployment Flow Diagram

Visual guide to deploying the prediction arbitrage system.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    DEPLOYMENT DECISION TREE                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  Do you have    │
                    │ historical data?│
                    └─────────────────┘
                         │         │
                    No   │         │   Yes
                         │         │
            ┌────────────┘         └────────────┐
            ▼                                    ▼
    ┌──────────────┐                    ┌──────────────┐
    │ Run bot in   │                    │  Run backtest│
    │ dry-run mode │                    │  to validate │
    │ for 7+ days  │                    │   strategy   │
    └──────────────┘                    └──────────────┘
            │                                    │
            │                                    │
            └────────────┬───────────────────────┘
                         ▼
                ┌─────────────────┐
                │ Backtest results│
                │   acceptable?   │
                └─────────────────┘
                    │         │
               No   │         │   Yes
                    │         │
        ┌───────────┘         └───────────┐
        ▼                                  ▼
┌──────────────┐                  ┌──────────────┐
│ Adjust risk  │                  │Choose deploy │
│ parameters   │                  │  environment │
│ and re-test  │                  └──────────────┘
└──────────────┘                          │
        │                          ┌──────┴──────┐
        │                          │             │
        └──────────────────────────┘             │
                                   ▼             ▼
                          ┌──────────────┐  ┌──────────────┐
                          │    Local     │  │  Production  │
                          │Docker Compose│  │   AWS EC2    │
                          └──────────────┘  └──────────────┘
```

## Backtesting Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        BACKTEST FLOW                             │
└─────────────────────────────────────────────────────────────────┘

1. Setup Database
   ┌──────────────────────────────────────────────┐
   │ docker run postgres:16-alpine                │
   │ export DATABASE_URL=...                      │
   │ alembic upgrade head                         │
   └──────────────────────────────────────────────┘
                    │
                    ▼
2. Load Historical Data
   ┌──────────────────────────────────────────────┐
   │ SELECT * FROM opportunities                  │
   │ WHERE detected_at BETWEEN from_ts AND to_ts  │
   └──────────────────────────────────────────────┘
                    │
                    ▼
3. Replay Through Engine
   ┌──────────────────────────────────────────────┐
   │ For each opportunity:                        │
   │   - ArbitrageEngine.rank()                   │
   │   - RiskManager.evaluate()                   │
   │   - Simulate fill at entry_price             │
   │   - Compute P&L using resolved_price         │
   └──────────────────────────────────────────────┘
                    │
                    ▼
4. Compute Statistics
   ┌──────────────────────────────────────────────┐
   │ - Total opportunities                        │
   │ - Trades simulated                           │
   │ - Gross/Net P&L                              │
   │ - Win rate                                   │
   │ - Max drawdown                               │
   │ - Sharpe ratio                               │
   └──────────────────────────────────────────────┘
                    │
                    ▼
5. Output Results
   ┌──────────────────────────────────────────────┐
   │ stdout: JSON summary                         │
   │ stderr: Human-readable table                 │
   └──────────────────────────────────────────────┘
```

## Local Deployment Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    LOCAL DEPLOYMENT FLOW                         │
└─────────────────────────────────────────────────────────────────┘

1. Configuration
   ┌──────────────────────────────────────────────┐
   │ cp .env.template .env                        │
   │ # Edit .env with API keys                    │
   │ # Generate API_SERVER_TOKEN                  │
   └──────────────────────────────────────────────┘
                    │
                    ▼
2. SSL Certificate (Optional)
   ┌──────────────────────────────────────────────┐
   │ cd infra/nginx/ssl                           │
   │ openssl req -x509 -nodes ...                 │
   └──────────────────────────────────────────────┘
                    │
                    ▼
3. Start Services
   ┌──────────────────────────────────────────────┐
   │ cd infra                                     │
   │ docker compose up -d                         │
   │                                              │
   │ Services started:                            │
   │   - postgres (database)                      │
   │   - bot (Python)                             │
   │   - dashboard (Next.js)                      │
   │   - prometheus (metrics)                     │
   │   - nginx (reverse proxy)                    │
   └──────────────────────────────────────────────┘
                    │
                    ▼
4. Verify Deployment
   ┌──────────────────────────────────────────────┐
   │ curl http://localhost/healthz                │
   │ curl http://localhost/api/v1/status          │
   │ open http://localhost                        │
   └──────────────────────────────────────────────┘
                    │
                    ▼
5. Monitor (24h Dry-Run)
   ┌──────────────────────────────────────────────┐
   │ docker compose logs -f bot                   │
   │ # Watch for errors                           │
   │ # Verify opportunities detected              │
   │ # Check simulated trades                     │
   └──────────────────────────────────────────────┘
                    │
                    ▼
6. Enable Live Trading (Optional)
   ┌──────────────────────────────────────────────┐
   │ # Edit .env: DRY_RUN=false                   │
   │ docker compose restart bot                   │
   │ # Monitor closely!                           │
   └──────────────────────────────────────────────┘
```

## Production Deployment Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                   PRODUCTION DEPLOYMENT FLOW                     │
└─────────────────────────────────────────────────────────────────┘

1. AWS Secrets Manager
   ┌──────────────────────────────────────────────┐
   │ aws secretsmanager create-secret             │
   │   --name arb/gemini/api_key                  │
   │   --secret-string "..."                      │
   │                                              │
   │ aws secretsmanager create-secret             │
   │   --name arb/gemini/api_secret               │
   │   --secret-string "..."                      │
   │                                              │
   │ aws secretsmanager create-secret             │
   │   --name arb/api/server_token                │
   │   --secret-string "..."                      │
   └──────────────────────────────────────────────┘
                    │
                    ▼
2. IAM Instance Profile
   ┌──────────────────────────────────────────────┐
   │ aws iam create-policy                        │
   │   --policy-name ArbBotPolicy                 │
   │   --policy-document file://policy.json       │
   │                                              │
   │ aws iam create-role                          │
   │   --role-name ArbBotRole                     │
   │                                              │
   │ aws iam attach-role-policy                   │
   │   --role-name ArbBotRole                     │
   │   --policy-arn arn:aws:iam::...              │
   │                                              │
   │ aws iam create-instance-profile              │
   │   --instance-profile-name ArbBotProfile      │
   └──────────────────────────────────────────────┘
                    │
                    ▼
3. Security Group
   ┌──────────────────────────────────────────────┐
   │ aws ec2 create-security-group                │
   │   --group-name arb-bot-sg                    │
   │                                              │
   │ aws ec2 authorize-security-group-ingress     │
   │   --protocol tcp --port 80 --cidr 0.0.0.0/0  │
   │                                              │
   │ aws ec2 authorize-security-group-ingress     │
   │   --protocol tcp --port 443 --cidr 0.0.0.0/0 │
   │                                              │
   │ aws ec2 authorize-security-group-ingress     │
   │   --protocol tcp --port 22 --cidr YOUR_IP/32 │
   └──────────────────────────────────────────────┘
                    │
                    ▼
4. Launch EC2 Instance
   ┌──────────────────────────────────────────────┐
   │ aws ec2 run-instances                        │
   │   --image-id ami-0c55b159cbfafe1f0           │
   │   --instance-type t3.medium                  │
   │   --iam-instance-profile Name=ArbBotProfile  │
   │   --security-group-ids sg-...                │
   │   --user-data file://bootstrap.sh            │
   │   --block-device-mappings '[{                │
   │     "DeviceName": "/dev/xvda",               │
   │     "Ebs": {"Encrypted": true}               │
   │   }]'                                        │
   └──────────────────────────────────────────────┘
                    │
                    ▼
5. Bootstrap Script Runs
   ┌──────────────────────────────────────────────┐
   │ 1. Install Docker                            │
   │ 2. Clone repository                          │
   │ 3. Fetch secrets from AWS Secrets Manager    │
   │ 4. Generate .env file                        │
   │ 5. Setup TLS certificate (Let's Encrypt)     │
   │ 6. Run database migrations                   │
   │ 7. Start docker compose                      │
   │ 8. Verify health check                       │
   └──────────────────────────────────────────────┘
                    │
                    ▼
6. Configure DNS (Optional)
   ┌──────────────────────────────────────────────┐
   │ # Get instance public IP                     │
   │ aws ec2 describe-instances ...               │
   │                                              │
   │ # Create A record                            │
   │ arb.yourdomain.com → INSTANCE_IP             │
   └──────────────────────────────────────────────┘
                    │
                    ▼
7. Verify Deployment
   ┌──────────────────────────────────────────────┐
   │ ssh ec2-user@INSTANCE_IP                     │
   │ cd /opt/arb/infra                            │
   │ sudo docker compose ps                       │
   │ sudo docker compose logs -f bot              │
   │                                              │
   │ curl https://arb.yourdomain.com/healthz      │
   └──────────────────────────────────────────────┘
                    │
                    ▼
8. Configure Monitoring
   ┌──────────────────────────────────────────────┐
   │ CloudWatch Logs:                             │
   │   aws logs tail /arb/bot --follow            │
   │                                              │
   │ Prometheus:                                  │
   │   ssh -L 9090:localhost:9090 ec2-user@...    │
   │   open http://localhost:9090                 │
   │                                              │
   │ Alerts:                                      │
   │   Configure Slack webhook in Secrets Manager │
   └──────────────────────────────────────────────┘
                    │
                    ▼
9. 24h Dry-Run Validation
   ┌──────────────────────────────────────────────┐
   │ Monitor for 24 hours:                        │
   │   - No crashes                               │
   │   - Opportunities detected                   │
   │   - Simulated trades logged                  │
   │   - Metrics recorded                         │
   │   - Alerts working                           │
   └──────────────────────────────────────────────┘
                    │
                    ▼
10. Enable Live Trading (Optional)
   ┌──────────────────────────────────────────────┐
   │ # Update secret in AWS Secrets Manager       │
   │ aws secretsmanager update-secret             │
   │   --secret-id arb/config/dry_run             │
   │   --secret-string "false"                    │
   │                                              │
   │ # Restart bot (picks up new config)          │
   │ ssh ec2-user@INSTANCE_IP                     │
   │ cd /opt/arb/infra                            │
   │ sudo docker compose restart bot              │
   │                                              │
   │ # Monitor closely!                           │
   └──────────────────────────────────────────────┘
```

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      SYSTEM ARCHITECTURE                         │
└─────────────────────────────────────────────────────────────────┘

Internet
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│                         nginx (443)                           │
│  - TLS termination                                           │
│  - Reverse proxy                                             │
│  - Rate limiting                                             │
└──────────────────────────────────────────────────────────────┘
   │
   ├─────────────────┬─────────────────┬─────────────────┐
   │                 │                 │                 │
   ▼                 ▼                 ▼                 ▼
┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
│   Bot   │   │Dashboard│   │Prometheus│   │/metrics │
│ (8000)  │   │ (3000)  │   │ (9090)  │   │endpoint │
└─────────┘   └─────────┘   └─────────┘   └─────────┘
   │
   ├──────────────────────────────────────────────────┐
   │                                                   │
   ▼                                                   ▼
┌─────────────────────────────────────┐   ┌─────────────────┐
│         PostgreSQL (5432)           │   │  External APIs  │
│  - opportunities                    │   │  - Kalshi       │
│  - gemini_positions                 │   │  - Polymarket   │
│  - pnl_snapshots                    │   │  - Gemini       │
│  - match_cache                      │   │  - OpenAI       │
│  - orderbook_snapshots              │   │  - Anthropic    │
└─────────────────────────────────────┘   └─────────────────┘
```

## Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                          DATA FLOW                               │
└─────────────────────────────────────────────────────────────────┘

Slow Loop (5 min):
   Scanner → EventMatcher → matched_pairs (in-memory)

Fast Loop (30 sec):
   matched_pairs → PricePoller → OrderbookCache
                                      ↓
                                 ArbitrageEngine
                                      ↓
                                 RiskManager
                                      ↓
                                  Executor
                                      ↓
                                  StateStore (DB)
                                      ↓
                                 SSEBroadcaster
                                      ↓
                                  Dashboard

Monitor Loop (60 sec):
   StateStore → PositionMonitor → Executor → StateStore
```

## Monitoring Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                       MONITORING FLOW                            │
└─────────────────────────────────────────────────────────────────┘

Bot Components
   │
   ├─ Structured Logs → stdout → CloudWatch Logs
   │                                    ↓
   │                              Log Insights
   │                              Alarms
   │
   ├─ Prometheus Metrics → /metrics → Prometheus
   │                                       ↓
   │                                   Grafana
   │                                   Alerts
   │
   └─ SSE Events → SSEBroadcaster → Dashboard
                                        ↓
                                   Real-time UI
```

## Alert Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                         ALERT FLOW                               │
└─────────────────────────────────────────────────────────────────┘

Trigger Events:
   - Drawdown > MAX_DRAWDOWN_PCT
   - Platform unavailable > 3 cycles
   - Order execution failure
   - Spread > ALERT_SPREAD_THRESHOLD

                    ↓
            AlertManager
                    ↓
         Deduplication Check
                    ↓
        ┌───────────┴───────────┐
        │                       │
        ▼                       ▼
   Slack Webhook          SMTP Email
        │                       │
        ▼                       ▼
   #alerts channel      ops@example.com
```

## Backup and Recovery Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                   BACKUP AND RECOVERY FLOW                       │
└─────────────────────────────────────────────────────────────────┘

Daily Backup:
   PostgreSQL → pg_dump → S3 bucket
                              ↓
                         Lifecycle policy
                         (30 day retention)

Recovery:
   S3 bucket → pg_restore → PostgreSQL
                                ↓
                           Verify data
                                ↓
                           Restart bot
```

## Scaling Considerations

```
┌─────────────────────────────────────────────────────────────────┐
│                     SCALING CONSIDERATIONS                       │
└─────────────────────────────────────────────────────────────────┘

Current: Single EC2 instance (t3.medium)
   - Handles ~100 opportunities/scan
   - ~10 concurrent positions
   - ~$1000 capital

Scale Up (Vertical):
   t3.medium → t3.large → t3.xlarge
   - More CPU for LLM calls
   - More memory for cache
   - Higher capital ($10k+)

Scale Out (Horizontal):
   - Multiple instances with load balancer
   - Shared PostgreSQL (RDS)
   - Distributed cache (Redis)
   - Message queue (SQS) for coordination
   - Higher capital ($100k+)
```
