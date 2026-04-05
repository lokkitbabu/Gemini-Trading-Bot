'use client';

import type { Opportunity } from '../../lib/api';

interface Props {
  opportunities: Opportunity[];
}

export default function OpportunitiesPanel({ opportunities }: Props) {
  const recent = opportunities.slice(0, 20);

  return (
    <div>
      <h2>Opportunities</h2>
      {recent.length === 0 ? (
        <p>No opportunities detected.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>Spread %</th>
              <th>Confidence</th>
              <th>Risk Score</th>
              <th>Sources</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((opp) => (
              <tr key={opp.id}>
                <td>{opp.event_title}</td>
                <td>{(opp.spread_pct * 100).toFixed(2)}%</td>
                <td>{(opp.match_confidence * 100).toFixed(1)}%</td>
                <td>{opp.risk_score.toFixed(2)}</td>
                <td>{opp.signal_sources.join(', ')}</td>
                <td>{opp.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
