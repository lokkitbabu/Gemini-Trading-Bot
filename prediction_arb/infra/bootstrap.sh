#!/bin/bash
set -euo pipefail

# NOTE: EBS encryption-at-rest must be enabled at EC2 launch time via the
# "Encrypt this volume" option in the EC2 console or via the --block-device-mappings
# flag in the AWS CLI / CloudFormation / Terraform. It cannot be enabled via user-data
# after the instance has launched.

# Install Docker
apt-get update -y
apt-get install -y docker.io docker-compose-plugin awscli

# Start Docker
systemctl enable docker
systemctl start docker

# Clone repo (replace with actual repo URL)
git clone https://github.com/your-org/prediction-arb.git /opt/prediction-arb
cd /opt/prediction-arb/prediction_arb

# Populate .env from AWS Secrets Manager
aws secretsmanager get-secret-value --secret-id arb/env --query SecretString --output text > .env

# TLS cert setup (self-signed fallback)
mkdir -p infra/nginx/ssl
if [ ! -f infra/nginx/ssl/cert.pem ]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout infra/nginx/ssl/key.pem \
        -out infra/nginx/ssl/cert.pem \
        -subj "/CN=localhost"
fi

# Run Alembic migrations
docker compose run --rm bot alembic upgrade head

# Start all services
docker compose up -d

# Health check
sleep 10
curl -f http://localhost/healthz || echo "Health check failed"
