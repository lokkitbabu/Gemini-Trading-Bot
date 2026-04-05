# Quick Start Guide

Get the prediction arbitrage system running in 5 minutes.

## Option 1: Backtest (Recommended First Step)

Test the system with historical data before deploying.

```bash
# 1. Start PostgreSQL
docker run -d --name arb-postgres \
  -e POSTGRES_DB=arbdb \
  -e POSTGRES_USER=arb \
  -e POSTGRES_PASSWORD=changeme \
  -p 5432:5432 \
  postgres:16-alpine

# 2. Install dependencies
cd prediction_arb
pip install -e .
pip install -e ".[test]"

# 3. Run migrations
export DATABASE_URL="postgresql+asyncpg://arb:changeme@localhost:5432/arbdb"
alembic upgrade head

# 4. Run backtest (requires historical data)
python -m prediction_arb.bot.main --backtest

# Or use the convenience script
./scripts/backtest.sh
```

**Note**: Backtesting requires historical opportunity data. If you don't have any, run the bot in dry-run mode first to collect data.

## Option 2: Local Deployment (Docker Compose)

Run the full system locally with Docker.

```bash
# 1. Copy environment template
cd prediction_arb
cp .env.template .env

# 2. Edit .env with your API keys
# Required:
#   - GEMINI_API_KEY
#   - GEMINI_API_SECRET
#   - API_SERVER_TOKEN (generate with: python -c "import secrets; print(secrets.token_urlsafe(32))")
vim .env

# 3. Generate self-signed SSL cert (for local testing)
cd infra/nginx/ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout privkey.pem -out fullchain.pem \
  -subj "/CN=localhost"
cd ../../..

# 4. Start all services
cd infra
docker compose up -d

# 5. Check logs
docker compose logs -f bot

# 6. Access dashboard
open http://localhost
# or
open https://localhost  # (self-signed cert warning expected)

# 7. Test API
curl http://localhost/healthz
```

## Option 3: Production Deployment (AWS EC2)

Deploy to AWS with full monitoring and alerting.

```bash
# 1. Create secrets in AWS Secrets Manager
aws secretsmanager create-secret \
  --name arb/gemini/api_key \
  --secret-string "your_gemini_api_key"

aws secretsmanager create-secret \
  --name arb/gemini/api_secret \
  --secret-string "your_gemini_api_secret"

aws secretsmanager create-secret \
  --name arb/api/server_token \
  --secret-string "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# 2. Create IAM instance profile
aws iam create-policy \
  --policy-name ArbBotPolicy \
  --policy-document file://prediction_arb/infra/iam/policy.json

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

aws iam attach-role-policy \
  --role-name ArbBotRole \
  --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/ArbBotPolicy

aws iam create-instance-profile \
  --instance-profile-name ArbBotInstanceProfile

aws iam add-role-to-instance-profile \
  --instance-profile-name ArbBotInstanceProfile \
  --role-name ArbBotRole

# 3. Create security group
aws ec2 create-security-group \
  --group-name arb-bot-sg \
  --description "Security group for arbitrage bot" \
  --vpc-id vpc-YOUR_VPC_ID

aws ec2 authorize-security-group-ingress \
  --group-id sg-YOUR_SG_ID \
  --protocol tcp --port 80 --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress \
  --group-id sg-YOUR_SG_ID \
  --protocol tcp --port 443 --cidr 0.0.0.0/0

aws ec2 authorize-security-group-ingress \
  --group-id sg-YOUR_SG_ID \
  --protocol tcp --port 22 --cidr YOUR_IP/32

# 4. Launch EC2 instance
aws ec2 run-instances \
  --image-id ami-0c55b159cbfafe1f0 \
  --instance-type t3.medium \
  --key-name your-key-pair \
  --security-group-ids sg-YOUR_SG_ID \
  --iam-instance-profile Name=ArbBotInstanceProfile \
  --block-device-mappings '[{
    "DeviceName": "/dev/xvda",
    "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "Encrypted": true}
  }]' \
  --user-data file://prediction_arb/infra/bootstrap.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=arb-bot}]'

# 5. Wait for instance to be ready (5-10 minutes)
aws ec2 describe-instances \
  --instance-ids i-YOUR_INSTANCE_ID \
  --query 'Reservations[0].Instances[0].State.Name'

# 6. Get public IP and test
INSTANCE_IP=$(aws ec2 describe-instances \
  --instance-ids i-YOUR_INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)

curl http://$INSTANCE_IP/healthz
```

## Configuration

### Key Environment Variables

Edit `.env` to configure the system:

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

# Intervals
SCAN_INTERVAL_SECONDS=300       # Market scan every 5 minutes
PRICE_POLL_INTERVAL_SECONDS=30  # Price poll every 30 seconds
MONITOR_INTERVAL_SECONDS=60     # Position check every 60 seconds

# Alerts
ALERT_CHANNEL=slack             # Options: none, slack, email, webhook
SLACK_WEBHOOK_URL=your_webhook  # Required if ALERT_CHANNEL=slack
```

## Verification

### Check System Health

```bash
# Health check
curl http://localhost/healthz

# Expected response:
# {"status":"ok","timestamp":"...","components":{"database":"ok","kalshi":"ok","polymarket":"ok","gemini":"ok"}}
```

### Check API Status

```bash
# Get API token from .env
export API_TOKEN="your_api_server_token"

# Check status
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost/api/v1/status

# Check opportunities
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost/api/v1/opportunities

# Check portfolio
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost/api/v1/portfolio
```

### View Logs

```bash
# Docker Compose
docker compose logs -f bot

# AWS CloudWatch
aws logs tail /arb/bot --follow
```

### View Metrics

```bash
# Prometheus metrics
curl http://localhost/metrics

# Key metrics:
# - arb_scan_cycles_total
# - arb_opportunities_detected_total
# - arb_trades_executed_total
# - arb_open_positions
# - arb_realized_pnl_usd
```

## Next Steps

1. **Run in dry-run mode** for 24-48 hours to verify everything works
2. **Review logs and metrics** to ensure no errors
3. **Backtest** with collected data to validate strategy
4. **Enable live trading** by setting `DRY_RUN=false` in `.env`
5. **Monitor closely** for the first few trades

## Troubleshooting

### Bot won't start

```bash
# Check logs
docker compose logs bot

# Common issues:
# - Missing API keys in .env
# - Database connection failed
# - Migration errors
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
# Check risk decisions in logs
docker compose logs bot | grep "risk_decision"

# Common reasons:
# - Spread too small (increase MIN_SPREAD_PCT)
# - Low confidence (lower MIN_CONFIDENCE)
# - Position cap reached (increase MAX_POSITIONS)
# - Insufficient liquidity (lower MIN_GEMINI_DEPTH_USD)
```

## Documentation

- **Full deployment guide**: See `DEPLOYMENT.md`
- **API documentation**: See `prediction_arb/bot/api/README.md`
- **Configuration reference**: See `.env.template`
- **Architecture**: See `.kiro/specs/prediction-arbitrage-production/design.md`

## Support

For detailed documentation, see:
- `DEPLOYMENT.md` - Complete deployment and operations guide
- `.env.template` - All configuration options with descriptions
- `infra/` - Infrastructure configuration files
- `.kiro/specs/` - Full system specification and design
