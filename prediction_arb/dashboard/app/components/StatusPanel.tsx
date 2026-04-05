'use client';

import type { StatusResponse } from '../../lib/api';

interface Props {
  status: StatusResponse | null;
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return `${h}h ${m}m ${s}s`;
}

export default function StatusPanel({ status }: Props) {
  if (!status) return <div>Loading status...</div>;

  return (
    <div>
      <h2>System Status</h2>
      <table>
        <tbody>
          <tr><td>Mode</td><td>{status.mode}</td></tr>
          <tr><td>Uptime</td><td>{formatUptime(status.uptime_seconds)}</td></tr>
          <tr><td>Scan Count</td><td>{status.scan_count}</td></tr>
          <tr><td>Last Scan</td><td>{status.last_scan_at ?? 'N/A'}</td></tr>
          <tr><td>Available Capital</td><td>${status.available_capital_usd.toFixed(2)}</td></tr>
          <tr><td>Open Positions</td><td>{status.open_positions}</td></tr>
          <tr><td>Realized P&amp;L</td><td>${status.realized_pnl_usd.toFixed(2)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}
