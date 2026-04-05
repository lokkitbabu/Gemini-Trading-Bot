'use client';

import type { FeedHealth } from '../../lib/api';

interface Props {
  feeds: FeedHealth[];
}

export default function FeedHealthPanel({ feeds }: Props) {
  if (!feeds.length) return <div>Loading feed health...</div>;

  return (
    <div>
      <h2>Feed Health</h2>
      <table>
        <thead>
          <tr>
            <th>Platform</th>
            <th>Status</th>
            <th>Last Success</th>
            <th>Consecutive Failures</th>
          </tr>
        </thead>
        <tbody>
          {feeds.map((feed) => (
            <tr key={feed.platform}>
              <td>{feed.platform}</td>
              <td style={{ color: feed.status === 'up' ? 'green' : 'red' }}>
                {feed.status}
              </td>
              <td>{feed.last_success_at ?? 'Never'}</td>
              <td>{feed.consecutive_failures}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
