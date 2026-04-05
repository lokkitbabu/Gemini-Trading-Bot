'use client';

import type { Trade } from '../../lib/api';

interface Props {
  trades: Trade[];
}

export default function TradesPanel({ trades }: Props) {
  const recent = trades.slice(0, 20);

  return (
    <div>
      <h2>Recent Trades</h2>
      {recent.length === 0 ? (
        <p>No closed trades.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>Side</th>
              <th>Amount (USD)</th>
              <th>Entry Price</th>
              <th>Exit Price</th>
              <th>Resolved P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((trade) => (
              <tr key={trade.id}>
                <td>{trade.event_title}</td>
                <td>{trade.side.toUpperCase()}</td>
                <td>${trade.amount_usd.toFixed(2)}</td>
                <td>{trade.entry_price.toFixed(4)}</td>
                <td>{trade.exit_price != null ? trade.exit_price.toFixed(4) : 'N/A'}</td>
                <td>
                  {trade.resolved_pnl != null
                    ? `$${trade.resolved_pnl.toFixed(2)}`
                    : 'N/A'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
