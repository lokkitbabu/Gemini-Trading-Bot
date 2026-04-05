# Prediction Arbitrage System - Deployment & Backtesting Guide

## Overview

This guide covers how to:
1. **Backtest** the system using historical data
2. **Deploy locally** with Docker Compose
3. **Deploy to production** on AWS EC2

---

## 1. Backtesting

Backtesting runs the system against historical opportunity data without making any live API calls or placing real orders.

### Prerequisites

```bash
# Install Python 3.12+
python --version  # Should be 3.12 or higher

# Install dependencies
cd prediction_arb
pip install -e .
pip install -e ".[test]"
```

### Setup Database for Backtesting

```bash
# Start a local PostgreSQL instance
docker run -d \
  --name arb-postgres \
  -e POSTGRES_DB=arbdb \
  -e POSTGRES_USER=arb \
  -e POSTGRES_PASSWORD=changeme \
  -p 5432:5432 \
  postgres:16-alpine

# Set database URL
export DATABASE_URL="postgresql+asyncpg://arb:changeme@localhost:5432/arbdb"

# Run migrations
cd prediction_arb
alembic upgrade head
```

### Run Backtest

```bash
# Backtest last 30 days (default)
python -m prediction_arb.bot.main --backtest

# Backtest specific date range
python -m prediction_arb.bot.main --backtest \
  --from 2026-01-01 \
  --to 2026-03-01

# Output:
# - stdout: JSON summary with metrics
# - stderr: Human-readable table
```

### Backtest Output

**JSON (stdout):**
```json
{
  "total_opportunities": 1250,
  "trades_simulated": 87,
  "gross_pnl": 142.35,
  "net_pnl": 138.92,
  "win_rate": 0.632,
  "max_drawdown": 0.087,
  "sharpe_ratio": 1.42
}
```

**Table (stderr):**
```
====================================================
  BACKTEST SUMMARY
  Period : 2026-01-01 → 2026-03-01
====================================================
  Total opportunities loaded :       1250
  Trades simulated           :         87
  Gross P&L                  :   142.3500
  Net P&L (after fees)       :   138.9200
  Win rate                   :      63.2%
  Max drawdown               :       8.7%
  Sharpe ratio (ann.)        :     1.4200
====================================================
```

### Interpreting Results

- **total_opportunities**: Number of opportunities detected in the period
- **trades_simulated**: Number of trades that passed risk checks
- **gross_pnl**: P&L before fees
- **net_pnl**: P&L after fees (uses `FEE_PER_CONTRACT` from config)
- **win_rate**: Percentage of profitable trades
- **max_drawdown**: Maximum peak-to-trough equity decline
- **sharpe_ratio**: Annualized risk-adjusted return (252 trading days)

### Backtest Configuration

Edit `.env` to adjust backtest parameters:

```bash
# Trading parameters
MIN_SPREAD_PCT=0.08          # Minimum 8% spread
MAX_POSITIONS=10             # Max concurrent positions
MAX_POSITION_PCT=0.05        # Max 5% capital per trade
CAPITAL=1000.0               # Starting capital
MIN_CONFIDENCE=0.70          # Minimum match confidence
MAX_RISK=0.80                # Maximum risk score

# Fee model
FEE_PER_CONTRACT=0.0         # Gemini currently charges 0
```

---

## 2. Local Deployment (Docker Compose)

### Prerequisites

- Docker 24+ and Docker Compose v2
- At least 4GB RAM available
- Ports 80, 443 available

### Setup

1. **Copy environment template:**

```bash
cd prediction_arb
cp .env.template .env
```

2. **Configure secrets in `.env`:**

```bash
# Required secrets
GEMINI_API_KEY=your_gemini_api_key
GEMINI_API_SECRET=your_gemini_api_secret
API_SERVER_TOKEN=generate_random_token_here

# Optional: For LLM-based matching
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key

# Optional: For alerts
SLACK_WEBHOOK_URL=your_slack_webhook

# Database (default works for local)
DATABASE_URL=postgresql+asyncpg://arb:changeme@postgres:5432/arbdb
POSTGRES_PASSWORD=changeme
```

3. **Generate API token:**

```bash
# Generate a secure random token
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Add this to .env as API_SERVER_TOKEN
```

4. **Configure SSL (optional for local):**

```bash
# For local development, use self-signed cert
cd infra/nginx/ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout privkey.pem -out fullchain.pem \
  -subj "/CN=localhost"
```

### Start Services

```bash
cd prediction_arb/infra
docker compose up -d

# Check logs
docker compose logs -f bot

# Check status
docker compose ps
```

### Access Dashboard

- **HTTP**: http://localhost
- **HTTPS**: https://localhost (self-signed cert warning expected)
- **API**: http://localhost/api/v1/status
- **Metrics**: http://localhost/metrics (restricted to VPC in production)

### Verify Deployment

```bash
# Health check
curl http://localhost/healthz

# Expected response:
# {"status":"ok","timestamp":"2026-03-20T...","components":{"database":"ok","kalshi":"ok","polymarket":"ok","gemini":"ok"}}

# Check API with auth
curl -H "Authorization: Bearer YOUR_API_SERVER_TOKEN" \
  http://localhost/api/v1/status
```

### Stop Services

```bash
cd prediction_arb/infra
docker compose down

# Remove volumes (WARNING: deletes all data)
docker compose down -v
```

---

## 3. Production Deployment (AWS EC2)

### Architecture

```
Internet → ALB (443) → EC2 Instance
                       ├─ nginx (reverse proxy)
                       ├─ bot (Python)
                       ├─ dashboard (Next.js)
                       ├─ postgres (data)
                       └─ prometheus (metrics)
```

### Prerequisites

- AWS account with EC2, Secrets Manager, CloudWatch access
- IAM permissions to create instances, security groups, secrets
- Domain name (optional, for TLS)

### Step 1: Create Secrets in AWS Secrets Manager

```bash
# Create secret for Gemini credentials
aws secretsmanager create-secret \
  --name arb/gemini/api_key \
  --secret-string "your_gemini_api_key"

aws secretsmanager create-secret \
  --name arb/gemini/api_secret \
  --secret-string "your_gemini_api_secret"

# Create secret for API token
aws secretsmanager create-secret \
  --name arb/api/server_token \
  --secret-string "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Optional: LLM keys
aws secretsmanager create-secret \
  --name arb/openai/api_key \
  --secret-string "your_openai_key"

# Optional: Slack webhook
aws secretsmanager create-secret \
  --name arb/slack/webhook_url \
  --secret-string "your_slack_webhook"
```

### Step 2: Create IAM Instance Profile

```bash
# Create policy (see infra/iam/policy.json)
aws iam create-policy \
  --policy-name ArbBotPolicy \
  --policy-document file://prediction_arb/infra/iam/policy.json

# Create role
aws iam create-role \
  --role-name ArbBotRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# Attach policy to role
aws iam attach-role-policy \
  --role-name ArbBotRole \
  --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/ArbBotPolicy

# Create instance profile
aws iam create-instance-profile \
  --instance-profile-name ArbBotInstanceProfile

aws iam add-role-to-instance-profile \
  --instance-profile-name ArbBotInstanceProfile \
  --role-name ArbBotRole
```

### Step 3: Create Security Group

```bash
# Create security group
aws ec2 create-security-group \
  --group-name arb-bot-sg \
  --description "Security group for arbitrage bot" \
  --vpc-id vpc-YOUR_VPC_ID

# Allow HTTP/HTTPS from anywhere
aws ec2 authorize-security-group-ingress \
  --group-id sg-YOUR_SG_ID \
  --protocol tcp --port 80 --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress \
  --group-id sg-YOUR_SG_ID \
  --protocol tcp --port 443 --cidr 0.0.0.0/0

# Allow SSH from your IP only
aws ec2 authorize-security-group-ingress \
  --group-id sg-YOUR_SG_ID \
  --protocol tcp --port 22 --cidr YOUR_IP/32
```

### Step 4: Launch EC2 Instance

```bash
# Launch instance with user data
aws ec2 run-instances \
  --image-id ami-0c55b159cbfafe1f0 \  # Amazon Linux 2023
  --instance-type t3.medium \
  --key-name your-key-pair \
  --security-group-ids sg-YOUR_SG_ID \
  --iam-instance-profile Name=ArbBotInstanceProfile \
  --block-device-mappings '[{
    "DeviceName": "/dev/xvda",
    "Ebs": {
      "VolumeSize": 30,
      "VolumeType": "gp3",
      "Encrypted": true
    }
  }]' \
  --user-data file://prediction_arb/infra/bootstrap.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=arb-bot}]'
```

### Step 5: Configure DNS (Optional)

```bash
# Get instance public IP
aws ec2 describe-instances \
  --instance-ids i-YOUR_INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress'

# Create A record in Route53 or your DNS provider
# arb.yourdomain.com → INSTANCE_PUBLIC_IP
```

### Step 6: Setup TLS Certificate

**Option A: Let's Encrypt (recommended for production)**

SSH into the instance and run:

```bash
sudo yum install -y certbot
sudo certbot certonly --standalone \
  -d arb.yourdomain.com \
  --email your@email.com \
  --agree-tos

# Copy certs to nginx directory
sudo cp /etc/letsencrypt/live/arb.yourdomain.com/fullchain.pem \
  /opt/arb/infra/nginx/ssl/
sudo cp /etc/letsencrypt/live/arb.yourdomain.com/privkey.pem \
  /opt/arb/infra/nginx/ssl/

# Restart nginx
cd /opt/arb/infra
sudo docker compose restart nginx
```

**Option B: Self-signed (development only)**

```bash
# Already handled by bootstrap.sh
```

### Step 7: Verify Deployment

```bash
# SSH into instance
ssh -i your-key.pem ec2-user@INSTANCE_PUBLIC_IP

# Check services
cd /opt/arb/infra
sudo docker compose ps

# Check logs
sudo docker compose logs -f bot

# Test health endpoint
curl http://localhost/healthz

# Test from outside
curl https://arb.yourdomain.com/healthz
```

### Step 8: Configure Monitoring

**CloudWatch Logs:**

Logs are automatically sent to `/arb/bot` log group (configured in docker-compose.yml).

View logs:
```bash
aws logs tail /arb/bot --follow
```

**Prometheus Metrics:**

Access Prometheus UI (internal only):
```bash
# SSH tunnel
ssh -L 9090:localhost:9090 ec2-user@INSTANCE_PUBLIC_IP

# Open browser
open http://localhost:9090
```

**Grafana (optional):**

Add Grafana to docker-compose.yml and configure dashboards for:
- `arb_scan_cycles_total`
- `arb_opportunities_detected_total`
- `arb_trades_executed_total`
- `arb_realized_pnl_usd`
- `arb_open_positions`

---

## 4. Configuration Reference

### Environment Variables

See `.env.template` for full list. Key variables:

**Trading:**
- `DRY_RUN=true` - Set to `false` for live trading
- `CAPITAL=1000.0` - Total capital in USD
- `MIN_SPREAD_PCT=0.08` - Minimum 8% spread
- `MAX_POSITIONS=10` - Max concurrent positions
- `MAX_POSITION_PCT=0.05` - Max 5% per position

**Secrets:**
- `SECRET_BACKEND=aws` - Use AWS Secrets Manager
- `GEMINI_API_KEY` - Gemini API key
- `GEMINI_API_SECRET` - Gemini API secret
- `API_SERVER_TOKEN` - Bearer token for API

**Intervals:**
- `SCAN_INTERVAL_SECONDS=300` - Market scan every 5 min
- `PRICE_POLL_INTERVAL_SECONDS=30` - Price poll every 30s
- `MONITOR_INTERVAL_SECONDS=60` - Position check every 60s

**Matching:**
- `MATCHER_BACKEND=rule_based` - Use rule-based matching (free)
- `MATCHER_BACKEND=openai` - Use GPT-4o-mini (requires API key)
- `MATCHER_BACKEND=anthropic` - Use Claude Haiku (requires API key)

**Alerts:**
- `ALERT_CHANNEL=slack` - Send alerts to Slack
- `SLACK_WEBHOOK_URL` - Slack webhook URL

---

## 5. Operational Procedures

### Enable Live Trading

```bash
# 1. Verify backtest results are acceptable
python -m prediction_arb.bot.main --backtest

# 2. Start in dry-run mode and monitor for 24h
# (DRY_RUN=true in .env)
docker compose up -d

# 3. Review logs and metrics
docker compose logs -f bot

# 4. If satisfied, enable live trading
# Edit .env: DRY_RUN=false
docker compose restart bot

# 5. Monitor closely for first few trades
```

### Monitor System Health

```bash
# Check health endpoint
curl http://localhost/healthz

# Check API status
curl -H "Authorization: Bearer $API_SERVER_TOKEN" \
  http://localhost/api/v1/status

# Check feed health
curl -H "Authorization: Bearer $API_SERVER_TOKEN" \
  http://localhost/api/v1/feeds/health

# View open positions
curl -H "Authorization: Bearer $API_SERVER_TOKEN" \
  http://localhost/api/v1/portfolio
```

### Handle Drawdown Suspension

When drawdown exceeds `MAX_DRAWDOWN_PCT`, the system automatically suspends trading and sends an alert.

To resume:
```bash
# 1. Review what went wrong
curl -H "Authorization: Bearer $API_SERVER_TOKEN" \
  http://localhost/api/v1/trades?limit=50

# 2. Adjust risk parameters if needed
# Edit .env: MAX_DRAWDOWN_PCT, MIN_SPREAD_PCT, etc.

# 3. Restart bot (automatically resumes)
docker compose restart bot
```

### Update Configuration

```bash
# 1. Edit .env
vim .env

# 2. Restart services
docker compose restart bot

# 3. Verify new config loaded
docker compose logs bot | grep "bot_starting"
```

### Backup Database

```bash
# Export database
docker compose exec postgres pg_dump -U arb arbdb > backup.sql

# Restore database
docker compose exec -T postgres psql -U arb arbdb < backup.sql
```

### View Metrics

```bash
# Prometheus metrics endpoint
curl http://localhost/metrics

# Key metrics:
# - arb_scan_cycles_total
# - arb_opportunities_detected_total
# - arb_trades_executed_total
# - arb_open_positions
# - arb_realized_pnl_usd
# - arb_scan_duration_seconds
```

---

## 6. Troubleshooting

### Bot won't start

```bash
# Check logs
docker compose logs bot

# Common issues:
# - Missing secrets: Check .env file
# - Database connection: Check DATABASE_URL
# - Migration failure: Run `alembic upgrade head` manually
```

### No opportunities detected

```bash
# Check feed health
curl -H "Authorization: Bearer $API_SERVER_TOKEN" \
  http://localhost/api/v1/feeds/health

# Check if markets are active
curl -H "Authorization: Bearer $API_SERVER_TOKEN" \
  http://localhost/api/v1/opportunities

# Adjust matching parameters
# Edit .env: MIN_CONFIDENCE=0.60 (lower threshold)
```

### Trades not executing

```bash
# Check risk manager logs
docker compose logs bot | grep "risk_decision"

# Common reasons:
# - spread_too_small: Increase MIN_SPREAD_PCT
# - low_confidence: Lower MIN_CONFIDENCE
# - insufficient_liquidity: Lower MIN_GEMINI_DEPTH_USD
# - position_cap: Increase MAX_POSITIONS
```

### High API latency

```bash
# Check metrics
curl http://localhost/metrics | grep arb_platform_api_latency

# If p95 > 5s:
# - Enable WebSocket streaming (KALSHI_WS_ENABLED=true)
# - Increase PRICE_POLL_INTERVAL_SECONDS
# - Check network connectivity
```

---

## 7. Testing

### Run Unit Tests

```bash
cd prediction_arb
pytest tests/unit/ -v
```

### Run Integration Tests

```bash
# Start test database
docker run -d --name test-postgres \
  -e POSTGRES_DB=test_arb \
  -e POSTGRES_USER=test \
  -e POSTGRES_PASSWORD=test \
  -p 5433:5432 \
  postgres:16-alpine

# Run tests
export DATABASE_URL="postgresql+asyncpg://test:test@localhost:5433/test_arb"
pytest tests/integration/ -v

# Cleanup
docker stop test-postgres && docker rm test-postgres
```

### Run Property-Based Tests

```bash
# Warning: These can take 10-30 minutes
pytest tests/property/ -v --hypothesis-show-statistics
```

---

## 8. Security Checklist

- [ ] All secrets stored in AWS Secrets Manager (not in .env)
- [ ] API_SERVER_TOKEN is strong random token (32+ bytes)
- [ ] SSH access restricted to operator IP only
- [ ] /metrics endpoint restricted to VPC CIDR
- [ ] EBS volumes encrypted at rest
- [ ] TLS certificate valid and auto-renewing
- [ ] CloudWatch logs enabled
- [ ] IAM instance profile has minimal permissions
- [ ] Database password is strong and rotated regularly
- [ ] Dry-run mode tested before live trading

---

## 9. Performance Tuning

### Optimize Scan Frequency

```bash
# High-frequency (more opportunities, higher API costs)
SCAN_INTERVAL_SECONDS=60
PRICE_POLL_INTERVAL_SECONDS=10

# Low-frequency (fewer opportunities, lower costs)
SCAN_INTERVAL_SECONDS=600
PRICE_POLL_INTERVAL_SECONDS=60
```

### Enable WebSocket Streaming

```bash
# Reduces API latency and rate limit pressure
KALSHI_WS_ENABLED=true
POLYMARKET_WS_ENABLED=true
```

### Adjust LLM Concurrency

```bash
# More concurrent calls = faster matching, higher API costs
MAX_CONCURRENT_LLM_CALLS=10

# Fewer calls = slower matching, lower costs
MAX_CONCURRENT_LLM_CALLS=2
```

---

## 10. Cost Estimation

### AWS Costs (Monthly)

- **EC2 t3.medium**: ~$30/month
- **EBS 30GB gp3**: ~$3/month
- **CloudWatch Logs (5GB)**: ~$2.50/month
- **Secrets Manager (5 secrets)**: ~$2/month
- **Data transfer**: ~$5/month
- **Total**: ~$42.50/month

### API Costs

- **Gemini**: Free (no trading fees currently)
- **Kalshi**: Free (read-only public endpoints)
- **Polymarket**: Free (public API)
- **OpenAI (optional)**: ~$0.15 per 1M tokens (GPT-4o-mini)
- **Anthropic (optional)**: ~$0.25 per 1M tokens (Claude Haiku)

### Estimated Total

- **Infrastructure**: $42.50/month
- **LLM (if enabled)**: $5-20/month depending on volume
- **Total**: $50-65/month

---

## Support

For issues or questions:
1. Check logs: `docker compose logs -f bot`
2. Review metrics: `curl http://localhost/metrics`
3. Check health: `curl http://localhost/healthz`
4. Review this guide's troubleshooting section
