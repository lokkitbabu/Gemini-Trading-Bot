# Deployment Checklist

Use this checklist to ensure a safe and successful deployment.

## Pre-Deployment

### 1. Backtesting

- [ ] Historical data collected (minimum 7 days recommended)
- [ ] Backtest run with current configuration
- [ ] Backtest results reviewed and acceptable:
  - [ ] Win rate > 50%
  - [ ] Sharpe ratio > 1.0
  - [ ] Max drawdown < 20%
  - [ ] Net P&L positive
- [ ] Backtest results documented

### 2. Configuration Review

- [ ] `.env` file created from `.env.template`
- [ ] All required secrets configured:
  - [ ] `GEMINI_API_KEY`
  - [ ] `GEMINI_API_SECRET`
  - [ ] `API_SERVER_TOKEN` (strong random token)
- [ ] Trading parameters reviewed:
  - [ ] `CAPITAL` set to appropriate amount
  - [ ] `MIN_SPREAD_PCT` validated via backtest
  - [ ] `MAX_POSITIONS` appropriate for capital
  - [ ] `MAX_POSITION_PCT` appropriate for risk tolerance
  - [ ] `MAX_DRAWDOWN_PCT` set to acceptable level
- [ ] Risk parameters validated:
  - [ ] `MIN_CONFIDENCE` appropriate for strategy
  - [ ] `MAX_RISK` appropriate for risk tolerance
  - [ ] `MIN_GEMINI_DEPTH_USD` appropriate for position sizes
- [ ] Intervals configured:
  - [ ] `SCAN_INTERVAL_SECONDS` appropriate for strategy
  - [ ] `PRICE_POLL_INTERVAL_SECONDS` appropriate for latency requirements
  - [ ] `MONITOR_INTERVAL_SECONDS` appropriate for exit strategy

### 3. Security Review

- [ ] All secrets stored securely (AWS Secrets Manager for production)
- [ ] No secrets committed to version control
- [ ] API token is strong (32+ bytes random)
- [ ] SSH access restricted to operator IP only
- [ ] `/metrics` endpoint restricted to VPC CIDR
- [ ] TLS certificate valid (Let's Encrypt or valid CA)
- [ ] Database password is strong
- [ ] IAM instance profile has minimal permissions
- [ ] EBS volumes encrypted at rest

### 4. Infrastructure Preparation (Production Only)

- [ ] AWS account configured
- [ ] IAM instance profile created with correct permissions
- [ ] Security group created with correct rules
- [ ] Secrets created in AWS Secrets Manager
- [ ] CloudWatch log group created (`/arb/bot`)
- [ ] EC2 instance type selected (t3.medium recommended)
- [ ] EBS volume size appropriate (30GB minimum)
- [ ] VPC and subnet configured
- [ ] Domain name configured (optional)
- [ ] DNS A record created (optional)

## Deployment

### 5. Initial Deployment

- [ ] Docker and Docker Compose installed
- [ ] Repository cloned to deployment location
- [ ] `.env` file in place with all secrets
- [ ] SSL certificates generated or obtained
- [ ] Database migrations run successfully (`alembic upgrade head`)
- [ ] All services started (`docker compose up -d`)
- [ ] Health check passes (`curl http://localhost/healthz`)
- [ ] API accessible with authentication
- [ ] Dashboard accessible
- [ ] Prometheus metrics accessible

### 6. Dry-Run Validation

- [ ] System running in dry-run mode (`DRY_RUN=true`)
- [ ] Logs show no errors for 1 hour
- [ ] Market scans completing successfully
- [ ] Opportunities being detected
- [ ] Simulated trades being logged
- [ ] Metrics being recorded
- [ ] Alerts working (if configured)
- [ ] Dashboard showing live data
- [ ] SSE events streaming correctly
- [ ] No memory leaks observed
- [ ] No CPU spikes observed
- [ ] Database growing at expected rate

### 7. 24-Hour Dry-Run

- [ ] System stable for 24 hours in dry-run mode
- [ ] No crashes or restarts
- [ ] Feed health consistently "ok" for all platforms
- [ ] Opportunity detection rate acceptable
- [ ] Simulated P&L tracking correctly
- [ ] Position monitoring working
- [ ] Exit strategies executing correctly (simulated)
- [ ] Logs reviewed for warnings/errors
- [ ] Metrics reviewed for anomalies
- [ ] Database size within expectations

## Go-Live

### 8. Pre-Live Checks

- [ ] Dry-run results reviewed and acceptable
- [ ] All stakeholders notified of go-live
- [ ] Monitoring dashboard open and ready
- [ ] Alert channels tested and working
- [ ] Emergency stop procedure documented
- [ ] Rollback plan documented
- [ ] Capital amount confirmed
- [ ] Risk parameters confirmed
- [ ] Trading hours confirmed (if applicable)

### 9. Enable Live Trading

- [ ] Backup of database taken
- [ ] `.env` updated: `DRY_RUN=false`
- [ ] Services restarted (`docker compose restart bot`)
- [ ] Logs confirm live mode enabled
- [ ] First scan cycle completed successfully
- [ ] First opportunity detected (if available)

### 10. First Trade Monitoring

- [ ] First trade executed successfully
- [ ] Order placed on Gemini correctly
- [ ] Position recorded in database
- [ ] Position appears in dashboard
- [ ] Metrics updated correctly
- [ ] Alert sent (if configured)
- [ ] SSE event broadcast
- [ ] Capital deducted correctly
- [ ] Position monitoring active

### 11. First Hour Live

- [ ] System stable for 1 hour
- [ ] Multiple trades executed (if opportunities available)
- [ ] No execution errors
- [ ] All trades recorded correctly
- [ ] P&L tracking correctly
- [ ] Position monitoring working
- [ ] No unexpected behavior
- [ ] Logs clean (no errors)
- [ ] Metrics look normal

### 12. First 24 Hours Live

- [ ] System stable for 24 hours
- [ ] Multiple trades executed and closed
- [ ] Realized P&L matches expectations
- [ ] Win rate within expected range
- [ ] No drawdown suspension triggered
- [ ] Exit strategies working correctly
- [ ] Stop-loss working (if triggered)
- [ ] Convergence exits working (if triggered)
- [ ] Feed health consistently good
- [ ] No platform API issues

## Post-Deployment

### 13. Ongoing Monitoring

- [ ] Daily health check review
- [ ] Daily P&L review
- [ ] Daily log review for errors/warnings
- [ ] Weekly metrics review
- [ ] Weekly backtest with new data
- [ ] Weekly configuration review
- [ ] Monthly performance review
- [ ] Monthly security review

### 14. Maintenance Schedule

- [ ] Database backup schedule configured (daily recommended)
- [ ] Log rotation configured
- [ ] Secrets rotation schedule (quarterly recommended)
- [ ] Dependency update schedule (monthly recommended)
- [ ] Security patch schedule (as needed)
- [ ] Performance tuning schedule (monthly recommended)

### 15. Incident Response

- [ ] Emergency stop procedure documented and tested
- [ ] Rollback procedure documented and tested
- [ ] Contact list for incidents
- [ ] Escalation procedure defined
- [ ] Post-incident review process defined

## Emergency Procedures

### Stop Trading Immediately

```bash
# Option 1: Enable dry-run mode
# Edit .env: DRY_RUN=true
docker compose restart bot

# Option 2: Stop bot entirely
docker compose stop bot

# Option 3: Close all positions manually via Gemini UI
```

### Rollback Deployment

```bash
# 1. Stop services
docker compose down

# 2. Restore database from backup
docker compose exec -T postgres psql -U arb arbdb < backup.sql

# 3. Revert code changes
git checkout <previous-commit>

# 4. Restart services
docker compose up -d
```

### Handle Drawdown Suspension

```bash
# 1. Review recent trades
curl -H "Authorization: Bearer $API_TOKEN" \
  http://localhost/api/v1/trades?limit=50

# 2. Analyze what went wrong
# - Check logs for errors
# - Review opportunity quality
# - Check feed health
# - Review market conditions

# 3. Adjust configuration if needed
# Edit .env with new risk parameters

# 4. Restart bot (automatically resumes)
docker compose restart bot
```

## Sign-Off

### Pre-Deployment Sign-Off

- [ ] Technical lead reviewed and approved
- [ ] Risk manager reviewed and approved
- [ ] Operations reviewed and approved
- [ ] Security reviewed and approved

Date: ________________

### Go-Live Sign-Off

- [ ] Dry-run validation complete
- [ ] All pre-live checks passed
- [ ] Monitoring in place
- [ ] Emergency procedures tested
- [ ] Stakeholders notified

Date: ________________

Approved by: ________________

### Post-Deployment Sign-Off

- [ ] First 24 hours stable
- [ ] Performance within expectations
- [ ] No critical issues
- [ ] Monitoring confirmed working
- [ ] Documentation updated

Date: ________________

Approved by: ________________

## Notes

Use this section to document any deviations from the checklist or additional steps taken:

```
[Add notes here]
```
