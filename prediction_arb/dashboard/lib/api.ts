/**
 * Typed API client for prediction-arb backend.
 * 
 * All endpoints require Bearer token authentication via NEXT_PUBLIC_API_TOKEN.
 * Base URL is configured via NEXT_PUBLIC_API_BASE (defaults to http://localhost:8000).
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';
const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? '';

function authHeaders(): HeadersInit {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${API_TOKEN}`,
  };
}

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { 
    headers: authHeaders(),
    cache: 'no-store',
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`API error ${res.status} on ${path}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ---- Response types matching backend routes.py ----

export interface StatusResponse {
  mode: 'dry_run' | 'live';
  uptime_seconds: number;
  scan_count: number;
  open_positions: number;
  available_capital_usd: number;
  realized_pnl_usd: number;
  last_scan_at: string | null;
}

export interface Opportunity {
  id: string;
  detected_at: string;
  event_title: string;
  asset: string;
  price_level: string | null;
  resolution_date: string;
  signal_platform: string;
  signal_event_id: string;
  signal_yes_price: number;
  gemini_event_id: string;
  gemini_yes_price: number;
  gemini_bid: number;
  gemini_ask: number;
  gemini_depth: number;
  spread: number;
  spread_pct: number;
  direction: string;
  entry_price: number;
  kelly_fraction: number;
  match_confidence: number;
  days_to_resolution: number;
  risk_score: number;
  status: string;
  signal_disagreement: boolean;
  inverted: boolean;
  price_age_seconds: number;
}

export interface OpportunitiesResponse {
  opportunities: Opportunity[];
  count: number;
}

export interface Trade {
  id: string;
  opportunity_id: string;
  event_id: string;
  side: string;
  quantity: number;
  entry_price: number;
  size_usd: number;
  exit_strategy: string;
  status: string;
  opened_at: string | null;
  closed_at: string | null;
  exit_price: number | null;
  realized_pnl: number | null;
}

export interface TradesResponse {
  trades: Trade[];
  limit: number;
  offset: number;
  count: number;
}

export interface Position {
  id: string;
  event_id: string;
  side: string;
  quantity: number;
  entry_price: number;
  size_usd: number;
  exit_strategy: string;
  status: string;
  opened_at: string | null;
  unrealized_pnl: number | null;
  days_to_resolution: number;
}

export interface PortfolioSummary {
  open_positions: number;
  available_capital_usd: number;
  peak_capital_usd: number;
  drawdown_pct: number;
  realized_pnl_usd: number;
  win_rate: number;
  trade_count: number;
  suspended: boolean;
}

export interface PortfolioResponse {
  summary: PortfolioSummary;
  positions: Position[];
}

export interface PnlSnapshot {
  [key: string]: string | number;
}

export interface PnlHistoryResponse {
  snapshots: PnlSnapshot[];
  count: number;
  from: string;
  to: string;
}

export interface FeedHealth {
  status: string;
  last_success_at: string | null;
  consecutive_failures: number;
}

export interface FeedsHealthResponse {
  feeds: Record<string, FeedHealth>;
}

export interface Orderbook {
  platform: string;
  ticker: string;
  best_bid: number | null;
  best_ask: number | null;
  yes_mid: number | null;
  depth_5pct: number | null;
  depth_3pct_usd: number | null;
  volume_24h: number | null;
  fetched_at: string;
}

export interface OrderbooksResponse {
  orderbooks: Orderbook[];
  count: number;
}

// ---- API functions ----

/**
 * GET /api/v1/status
 * Returns system status summary including mode, uptime, scan count, and capital.
 */
export function fetchStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>('/api/v1/status');
}

/**
 * GET /api/v1/opportunities
 * Returns current list of actionable arbitrage opportunities.
 */
export async function fetchOpportunities(): Promise<Opportunity[]> {
  const response = await apiFetch<OpportunitiesResponse>('/api/v1/opportunities');
  return response.opportunities;
}

/**
 * GET /api/v1/trades?limit=X&offset=Y
 * Returns paginated trade history.
 */
export async function fetchTrades(limit = 20, offset = 0): Promise<Trade[]> {
  const response = await apiFetch<TradesResponse>(
    `/api/v1/trades?limit=${limit}&offset=${offset}`
  );
  return response.trades;
}

/**
 * GET /api/v1/portfolio
 * Returns portfolio summary and open positions.
 */
export function fetchPortfolio(): Promise<PortfolioResponse> {
  return apiFetch<PortfolioResponse>('/api/v1/portfolio');
}

/**
 * GET /api/v1/pnl/history?from=X&to=Y
 * Returns time-series P&L snapshots between the given ISO 8601 timestamps.
 */
export async function fetchPnlHistory(from: string, to: string): Promise<PnlSnapshot[]> {
  const response = await apiFetch<PnlHistoryResponse>(
    `/api/v1/pnl/history?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
  );
  return response.snapshots;
}

/**
 * GET /api/v1/feeds/health
 * Returns connectivity status for each platform (Kalshi, Polymarket, Gemini).
 */
export async function fetchFeedsHealth(): Promise<Record<string, FeedHealth>> {
  const response = await apiFetch<FeedsHealthResponse>('/api/v1/feeds/health');
  return response.feeds;
}

/**
 * GET /api/v1/orderbooks
 * Returns current in-memory orderbook snapshots for all active matched pairs.
 */
export async function fetchOrderbooks(): Promise<Orderbook[]> {
  const response = await apiFetch<OrderbooksResponse>('/api/v1/orderbooks');
  return response.orderbooks;
}
