#!/bin/bash
# Quick-start script for backtesting the prediction arbitrage system

set -e

echo "=========================================="
echo "Prediction Arbitrage Backtest Setup"
echo "=========================================="
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.12+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
if (( $(echo "$PYTHON_VERSION < 3.12" | bc -l) )); then
    echo "❌ Python 3.12+ required. Found: $PYTHON_VERSION"
    exit 1
fi

echo "✓ Python $PYTHON_VERSION found"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker"
    exit 1
fi

echo "✓ Docker found"
echo ""

# Start PostgreSQL container if not running
if ! docker ps | grep -q arb-postgres; then
    echo "Starting PostgreSQL container..."
    docker run -d \
        --name arb-postgres \
        -e POSTGRES_DB=arbdb \
        -e POSTGRES_USER=arb \
        -e POSTGRES_PASSWORD=changeme \
        -p 5432:5432 \
        postgres:16-alpine
    
    echo "Waiting for PostgreSQL to be ready..."
    sleep 5
    echo "✓ PostgreSQL started"
else
    echo "✓ PostgreSQL already running"
fi

echo ""

# Install dependencies
echo "Installing Python dependencies..."
cd "$(dirname "$0")/.."
pip install -q -e . 2>&1 | grep -v "already satisfied" || true
pip install -q -e ".[test]" 2>&1 | grep -v "already satisfied" || true
echo "✓ Dependencies installed"
echo ""

# Set database URL
export DATABASE_URL="postgresql+asyncpg://arb:changeme@localhost:5432/arbdb"

# Run migrations
echo "Running database migrations..."
alembic upgrade head > /dev/null 2>&1
echo "✓ Migrations complete"
echo ""

# Check if we have any data
echo "Checking for historical data..."
OPPORTUNITY_COUNT=$(docker exec arb-postgres psql -U arb -d arbdb -t -c "SELECT COUNT(*) FROM opportunities;" 2>/dev/null | tr -d ' ' || echo "0")

if [ "$OPPORTUNITY_COUNT" -eq "0" ]; then
    echo "⚠️  No historical opportunities found in database"
    echo ""
    echo "To backtest, you need historical data. Options:"
    echo "  1. Run the bot in dry-run mode for a few days to collect data"
    echo "  2. Import historical data from a backup"
    echo ""
    echo "To run the bot in dry-run mode:"
    echo "  cd prediction_arb"
    echo "  cp .env.template .env"
    echo "  # Edit .env with your API keys"
    echo "  docker compose -f infra/docker-compose.yml up -d"
    echo ""
    exit 0
fi

echo "✓ Found $OPPORTUNITY_COUNT historical opportunities"
echo ""

# Parse command line arguments
FROM_DATE=""
TO_DATE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --from)
            FROM_DATE="$2"
            shift 2
            ;;
        --to)
            TO_DATE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--from YYYY-MM-DD] [--to YYYY-MM-DD]"
            exit 1
            ;;
    esac
done

# Run backtest
echo "=========================================="
echo "Running Backtest"
echo "=========================================="
echo ""

if [ -n "$FROM_DATE" ] && [ -n "$TO_DATE" ]; then
    echo "Period: $FROM_DATE to $TO_DATE"
    python -m prediction_arb.bot.main --backtest --from "$FROM_DATE" --to "$TO_DATE"
elif [ -n "$FROM_DATE" ]; then
    echo "Period: $FROM_DATE to now"
    python -m prediction_arb.bot.main --backtest --from "$FROM_DATE"
elif [ -n "$TO_DATE" ]; then
    echo "Period: 30 days ago to $TO_DATE"
    python -m prediction_arb.bot.main --backtest --to "$TO_DATE"
else
    echo "Period: Last 30 days"
    python -m prediction_arb.bot.main --backtest
fi

echo ""
echo "=========================================="
echo "Backtest Complete"
echo "=========================================="
