'use client';

import { useEffect, useState } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { fetchPnlHistory, type PnlPoint } from '../../lib/api';

type Range = '1h' | '24h' | '7d' | '30d';

const RANGES: { label: string; value: Range; hours: number }[] = [
  { label: '1h', value: '1h', hours: 1 },
  { label: '24h', value: '24h', hours: 24 },
  { label: '7d', value: '7d', hours: 168 },
  { label: '30d', value: '30d', hours: 720 },
];

interface Props {
  data?: PnlPoint[];
}

export default function PnlChart({ data: initialData }: Props) {
  const [range, setRange] = useState<Range>('24h');
  const [data, setData] = useState<PnlPoint[]>(initialData ?? []);

  useEffect(() => {
    const hours = RANGES.find((r) => r.value === range)?.hours ?? 24;
    const to = new Date();
    const from = new Date(to.getTime() - hours * 3600 * 1000);
    fetchPnlHistory(from.toISOString(), to.toISOString())
      .then(setData)
      .catch(console.error);
  }, [range]);

  return (
    <div>
      <h2>P&amp;L History</h2>
      <div>
        {RANGES.map((r) => (
          <button
            key={r.value}
            onClick={() => setRange(r.value)}
            style={{ fontWeight: range === r.value ? 'bold' : 'normal', marginRight: 4 }}
          >
            {r.label}
          </button>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" tick={{ fontSize: 10 }} />
          <YAxis />
          <Tooltip />
          <Line type="monotone" dataKey="realized_pnl_usd" name="Realized P&L" dot={false} />
          <Line type="monotone" dataKey="unrealized_pnl_usd" name="Unrealized P&L" dot={false} strokeDasharray="4 2" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
