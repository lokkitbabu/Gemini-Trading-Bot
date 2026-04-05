'use client';

import type { Position } from '../../lib/api';

interface Props {
  positions: Position[];
}

export default function PositionsTable({ positions }: Props) {
  return (
    <div>
      <h2>Open Positions</h2>
      {positions.length === 0 ? (
        <p>No open positions.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>Side</th>
              <th>Entry Price</th>
              <th>Amount (USD)</th>
              <th>Quantity</th>
              <th>Unrealized P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((pos) => (
              <tr key={pos.id}>
                <td>{pos.event_title}</td>
                <td>{pos.side.toUpperCase()}</td>
                <td>{pos.entry_price.toFixed(4)}</td>
                <td>${pos.amount_usd.toFixed(2)}</td>
                <td>{pos.quantity}</td>
                <td>
                  {pos.unrealized_pnl != null
                    ? `$${pos.unrealized_pnl.toFixed(2)}`
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
