# Quick Reference Card

Essential commands and information for operating the prediction arbitrage system.

## 🚀 Quick Start Commands

### Backtest
```bash
cd prediction_arb
./scripts/backtest.sh
# or
python -m prediction_arb.bot.main --backtest --from 2026-01-01 --to 2026-03-01
```

### Local Deployment
```bash
cd prediction_arb
cp .env.template .env && vim .env  # Add API keys
cd infra && docker compose up -d
```

### Production Deployment
```bash
# See DEPLOYMENT.md for complete guide
aws ec2 run-instances --user-data file://infra/bootstrap.sh ...
```

## 📊 Monitoring Commands

### Health Check
```bash
curl http://localhost/healthz
```

### API Status
```bash
export TOKEN="your_api_server_token"
curl -H "Authorization: Bearer $TOKEN" http://localhost/api/v1/status
```

### View Logs
```bash
# Local
docker compose logs -f bot

# AWS
aws logs tail /arb/bot --follow
```

### View Metrics
```bash
curl http://localhost/metrics
```

### Check Opportunities
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost/api/v1/opportunities
```

### Check Portfolio
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost/api/v1/portfolio
```

### Check Feed Health
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost/api/v1/feeds/health
```

## 🔧 Management Commands

### Start Services
```bash
cd prediction_arb/infra
docker compose up -d
```

### Stop Services
```bash
docker compose down
```

### Restart Bot
```bash
docker compose restart bot
```

### View Service Status
```bash
docker compose ps
```

### Update Configuration
```bash
vim .env
docker compose restart bot
```

### Backup Database
```bash
docker compose exec postgres pg_dump -U arb arbdb > backup_$(date +%Y%m%d).sql
```

### Restore Database
```bash
docker compose exec -T postgres psql -U arb arbdb < backup_20260320.sql
```

### Run Migrations
```bash
cd prediction_arb
alembic upgrade head
```

### View Database
```bash
docker compose exec postgres psql -U arb arbdb
# Then: SELECT COUNT(*) FROM opportunities;
```

## 🛑 Emergency Commands

### Stop Trading Immediately
```bash
# Option 1: Enable dry-run
sed -i 's/DRY_RUN=false/DRY_RUN=true/' .env
docker compose restart bot

# Option 2: Stop bot
docker compose stop bot
```

### Close All Positions
```bash
# Manual via Gemini UI or API
# Bot will auto-close on next monitor cycle if conditions met
```

### View Recent Errors
```bash
docker compose logs bot | grep ERROR | tail -20
```

### Check Drawdown Status
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost/api/v1/status | jq '.drawdown_pct'
```

## 📝 Configuration Quick Reference

### Essential Environment Variables

```bash
# Trading
DRY_RUN=true                    # false for live trading
CAPITAL=1000.0                  # Total capital
MIN_SPREAD_PCT=0.08             # Minimum spread (8%)
MAX_POSITIONS=10                # Max concurrent positions
MAX_POSITION_PCT=0.05           # Max per position (5%)

# Risk
MAX_DRAWDOWN_PCT=0.20           # Max drawdown (20%)
MIN_CONFIDENCE=0.70             # Min match confidence
MAX_RISK=0.80                   # Max risk score
STOP_LOSS_PCT=0.15              # Stop loss (15%)

# Intervals
SCAN_INTERVAL_SECONDS=300       # Market scan (5 min)
PRICE_POLL_INTERVAL_SECONDS=30  # Price poll (30 sec)
MONITOR_INTERVAL_SECONDS=60     # Position check (60 sec)

# Secrets
GEMINI_API_KEY=                 # Required
GEMINI_API_SECRET=              # Required
API_SERVER_TOKEN=               # Required
```

## 📈 Key Metrics

### Prometheus Metrics
- `arb_scan_cycles_total` - Total scans
- `arb_opportunities_detected_total` - Opportunities found
- `arb_trades_executed_total` - Trades executed
- `arb_open_positions` - Current positions
- `arb_realized_pnl_usd` - Realized P&L
- `arb_scan_duration_seconds` - Scan latency

### Health Check Response
```json
{
  "status": "ok",
  "timestamp": "2026-03-20T12:00:00Z",
  "components": {
    "database": "ok",
    "kalshi": "ok",
    "polymarket": "ok",
    "gemini": "ok"
  }
}
```

## 🔍 Troubleshooting Quick Fixes

### Bot Won't Start
```bash
docker compose logs bot | tail -50
# Check for: missing secrets, DB connection, migration errors
```

### No Opportunities Detected
```bash
# Lower confidence threshold
sed -i 's/MIN_CONFIDENCE=0.70/MIN_CONFIDENCE=0.60/' .env
docker compose restart bot
```

### Trades Not Executing
```bash
# Check risk decisions
docker compose logs bot | grep "risk_decision" | tail -20
# Common: spread_too_small, low_confidence, position_cap
```

### High API Latency
```bash
# Enable WebSocket streaming
sed -i 's/KALSHI_WS_ENABLED=false/KALSHI_WS_ENABLED=true/' .env
sed -i 's/POLYMARKET_WS_ENABLED=false/POLYMARKET_WS_ENABLED=true/' .env
docker compose restart bot
```

### Database Full
```bash
# Check size
docker compose exec postgres psql -U arb arbdb -c "SELECT pg_size_pretty(pg_database_size('arbdb'));"

# Clean old data (careful!)
docker compose exec postgres psql -U arb arbdb -c "DELETE FROM opportunities WHERE detected_at < NOW() - INTERVAL '90 days';"
```

## 🔐 Security Checklist

- [ ] Secrets in AWS Secrets Manager (production)
- [ ] Strong API_SERVER_TOKEN (32+ bytes)
- [ ] SSH restricted to your IP
- [ ] TLS certificate valid
- [ ] /metrics restricted to VPC
- [ ] EBS encrypted
- [ ] No secrets in logs
- [ ] Database password strong

## 📚 Documentation Links

- **[README.md](README.md)** - Project overview
- **[QUICKSTART.md](QUICKSTART.md)** - Quick start guide
- **[DEPLOYMENT.md](../DEPLOYMENT.md)** - Complete deployment guide
- **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)** - Pre-deployment checklist
- **[DEPLOYMENT_FLOW.md](DEPLOYMENT_FLOW.md)** - Visual flow diagrams
- **[.env.template](.env.template)** - Configuration reference

## 🆘 Emergency Contacts

```
# Add your team contacts here
Technical Lead: [name] [email] [phone]
Operations: [name] [email] [phone]
On-Call: [rotation] [pagerduty]
```

## 📊 Performance Targets

### Backtest Targets
- Win rate: > 50%
- Sharpe ratio: > 1.0
- Max drawdown: < 20%
- Net P&L: Positive

### Live Trading Targets
- Scan cycle: < 10s
- API latency p95: < 5s
- Uptime: > 99%
- Alert response: < 5 min

## 💰 Cost Tracking

### Monthly Costs
- AWS EC2 t3.medium: ~$30
- AWS EBS 30GB: ~$3
- AWS CloudWatch: ~$2.50
- AWS Secrets Manager: ~$2
- Data transfer: ~$5
- LLM API (optional): ~$5-20
- **Total**: ~$50-65/month

## 🎯 Quick Decision Matrix

### Should I enable live trading?
- ✅ Backtest win rate > 50%
- ✅ Backtest Sharpe > 1.0
- ✅ 24h dry-run stable
- ✅ No errors in logs
- ✅ Opportunities detected
- ✅ Simulated trades correct
- ✅ Monitoring working
- ✅ Alerts tested

### Should I increase capital?
- ✅ Live trading for 30+ days
- ✅ Realized P&L positive
- ✅ Win rate > 55%
- ✅ Max drawdown < 15%
- ✅ No execution errors
- ✅ Feed health consistently good

### Should I adjust risk parameters?
- ⚠️ Win rate < 50% → Increase MIN_SPREAD_PCT
- ⚠️ Too few trades → Lower MIN_CONFIDENCE
- ⚠️ Drawdown > 15% → Lower MAX_POSITION_PCT
- ⚠️ Frequent stops → Increase STOP_LOSS_PCT

## 🔄 Maintenance Schedule

### Daily
- [ ] Check health endpoint
- [ ] Review logs for errors
- [ ] Check P&L

### Weekly
- [ ] Review metrics
- [ ] Run backtest with new data
- [ ] Check feed health trends

### Monthly
- [ ] Performance review
- [ ] Update dependencies
- [ ] Rotate secrets
- [ ] Review configuration

### Quarterly
- [ ] Security audit
- [ ] Cost optimization
- [ ] Strategy review
- [ ] Documentation update

## 📞 Support

For issues:
1. Check logs: `docker compose logs -f bot`
2. Check health: `curl http://localhost/healthz`
3. Check metrics: `curl http://localhost/metrics`
4. Review documentation
5. Contact team (see Emergency Contacts)

---

**Last Updated**: 2026-03-20
**Version**: 1.0.0
