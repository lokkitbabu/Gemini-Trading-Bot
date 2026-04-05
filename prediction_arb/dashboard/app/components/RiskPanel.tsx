'use client';

import type { Portfolio } from '../../lib/api';

interface Props {
  portfolio: Portfolio | null;
}

export default function RiskPanel({ portfolio }: Props) {
  if (!portfolio) return <div>Loading risk data...</div>;

  return (
    <div>
      <h2>Risk</h2>
      {portfolio.suspended && (
        <div style={{ color: 'red', fontWeight: 'bold' }}>⚠ SUSPENDED</div>
      )}
      <table>
        <tbody>
          <tr><td>Drawdown</td><td>{(portfolio.drawdown_pct * 100).toFixed(2)}%</td></tr>
          <tr><td>Capital Utilization</td><td>{(portfolio.capital_utilization_pct * 100).toFixed(2)}%</td></tr>
          <tr><td>Available Capital</td><td>${portfolio.available_capital_usd.toFixed(2)}</td></tr>
          <tr><td>Total Capital</td><td>${portfolio.total_capital_usd.toFixed(2)}</td></tr>
          <tr><td>Suspended</td><td>{portfolio.suspended ? 'Yes' : 'No'}</td></tr>
        </tbody>
      </table>
    </div>
  );
}
