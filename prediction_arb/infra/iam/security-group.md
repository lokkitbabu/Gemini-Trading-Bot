# EC2 Security Group Rules

## Inbound Rules

| Port | Protocol | Source            | Description                        |
|------|----------|-------------------|------------------------------------|
| 80   | TCP      | 0.0.0.0/0         | HTTP (redirected to HTTPS by nginx) |
| 443  | TCP      | 0.0.0.0/0         | HTTPS (nginx TLS termination)       |
| 22   | TCP      | <operator-CIDR>   | SSH — restrict to operator IP range only |

All other ports (5432 PostgreSQL, 9090 Prometheus, 8000 bot API, 3000 dashboard) are **internal only** — no public inbound rules. Traffic on these ports is restricted to the internal Docker network.

## Outbound Rules

| Port | Protocol | Destination | Description          |
|------|----------|-------------|----------------------|
| All  | All      | 0.0.0.0/0   | All outbound allowed |

## Notes

- Replace `<operator-CIDR>` with the actual operator IP range (e.g. `203.0.113.0/32`).
- The `/metrics` endpoint is additionally restricted at the nginx layer to `10.0.0.0/8` (VPC CIDR).
- EBS encryption-at-rest must be enabled at EC2 launch time — this cannot be configured via user-data or security groups.
