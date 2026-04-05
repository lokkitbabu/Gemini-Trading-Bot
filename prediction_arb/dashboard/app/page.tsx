'use client';

import { useEffect, useState } from 'react';
import { useSSE } from '../lib/sse';
import {
  fetchStatus,
  fetchPortfolio,
  fetchOpportunities,
  fetchTrades,
  fetchFeedsHealth,
  type StatusResponse,
  type Portfolio,
  type Opportunity,
  type Trade,
  type FeedHealth,
  type PnlPoint,
} from '../lib/api';
import StatusPanel from './components/StatusPanel';
import RiskPanel from './components/RiskPanel';
import FeedHealthPanel from './components/FeedHealthPanel';
import PnlChart from './components/PnlChart';
import PositionsTable from './components/PositionsTable';
import OpportunitiesPanel from './components/OpportunitiesPanel';
import TradesPanel from './components/TradesPanel';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';
const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? '';

export default function DashboardPage() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [feeds, setFeeds] = useState<FeedHealth[]>([]);
  const [pnlData] = useState<PnlPoint[]>([]);

  const { lastEvent, isConnected } = useSSE(`${API_BASE}/api/v1/events`, API_TOKEN);

  // Initial data load
  useEffect(() => {
    Promise.all([
      fetchStatus().then(setStatus).catch(console.error),
      fetchPortfolio().then(setPortfolio).catch(console.error),
      fetchOpportunities().then(setOpportunities).catch(console.error),
      fetchTrades(20, 0).then(setTrades).catch(console.error),
      fetchFeedsHealth().then(setFeeds).catch(console.error),
    ]);
  }, []);

  // Fallback polling when SSE is unavailable
  useEffect(() => {
    if (isConnected) return;
    const id = setInterval(() => {
      fetchStatus().then(setStatus).catch(console.error);
    }, 10000);
    return () => clearInterval(id);
  }, [isConnected]);

  // Handle SSE events
  useEffect(() => {
    if (!lastEvent) return;
    const { type, data } = lastEvent;
    if (type === 'opportunity_detected') {
      setOpportunities((prev) => [data as Opportunity, ...prev].slice(0, 50));
    } else if (type === 'position_opened') {
      setPortfolio((prev) =>
        prev
          ? { ...prev, open_positions: [...prev.open_positions, data as never] }
          : prev
      );
    } else if (type === 'position_closed') {
      fetchTrades(20, 0).then(setTrades).catch(console.error);
      fetchPortfolio().then(setPortfolio).catch(console.error);
    } else if (type === 'risk_suspended') {
      setPortfolio((prev) => (prev ? { ...prev, suspended: true } : prev));
    }
  }, [lastEvent]);

  return (
    <main>
      <h1>Prediction Arb Dashboard</h1>
      {!isConnected && (
        <div style={{ background: '#ffe', padding: '8px', marginBottom: '8px' }}>
          ⚠ Reconnecting to live feed...
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '16px' }}>
        <StatusPanel status={status} />
        <RiskPanel portfolio={portfolio} />
        <FeedHealthPanel feeds={feeds} />
      </div>
      <div style={{ marginTop: '16px' }}>
        <PnlChart data={pnlData} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginTop: '16px' }}>
        <PositionsTable positions={portfolio?.open_positions ?? []} />
        <OpportunitiesPanel opportunities={opportunities} />
      </div>
      <div style={{ marginTop: '16px' }}>
        <TradesPanel trades={trades} />
      </div>
    </main>
  );
}
