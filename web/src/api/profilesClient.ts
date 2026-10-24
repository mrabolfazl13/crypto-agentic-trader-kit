/**
 * Typed client for the staged experiment profiles (`/api/v1/profiles`).
 *
 * Contract: `docs/API_CONTRACT_FA.md`. Same rules as the rest of the API layer:
 * every optional number is `number | null` (nothing is turned into a fake zero),
 * and there is no mutation here — profiles are read-only backtest records.
 */
import { unwrap, url, type Provenance } from "./client";

export type ProfileMarket = "crypto" | "tse";

export interface ProfileStats {
  trades: number;
  symbols: number;
  symbols_with_trades: number;
  gross_profit: number;
  gross_loss: number;
  net_pnl: number;
  fees_sum: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  best_trade: number | null;
  worst_trade: number | null;
  mean_net_return_pct: number | null;
  median_net_return_pct: number | null;
  positive_symbols_pct: number | null;
  median_hold_hours: number | null;
  mean_hold_hours: number | null;
  within_day_pct: number | null;
  within_3h_pct: number | null;
  exits: Record<string, number>;
  median_max_drawdown_pct: number | null;
}

export interface ProfileTrade {
  symbol: string;
  pnl: number;
  fees: number;
  reason: string;
  entry_time: number;
  exit_time: number;
  entry: number;
  exit: number;
  qty: number;
  holding_hours: number;
  return_pct: number | null;
}

export interface ProfileSymbolRow {
  symbol: string;
  trades: number;
  net_pnl: number;
  net_return_pct: number | null;
  win_rate_pct: number;
  fees: number;
}

export interface ProfileStage {
  id: string;
  title: string;
  change: string;
  strategy: string;
  horizon: "none" | "day" | "short";
  max_hold_bars: number;
  max_hold_hours: number | null;
  params: Record<string, unknown>;
  all: ProfileStats;
  train: ProfileStats;
  validation: ProfileStats;
  top_symbols: ProfileSymbolRow[];
  bottom_symbols: ProfileSymbolRow[];
  trades: ProfileTrade[];
  trades_truncated: number;
  skipped: Record<string, string>;
  /** [unix seconds, percent change] */
  basket_curve: [number, number][];
}

export interface ProfileVerdict {
  finalist: string | null;
  promoted: boolean;
  stable_stages: string[];
  text_fa: string;
  selection_rule: string;
  stable_rule: string;
}

export interface ProfileMeta {
  run_id: string;
  generated_at: string;
  market: string;
  venue: string | null;
  timeframe: string;
  days: number;
  warmup_bars: number;
  horizon: "day" | "short";
  horizon_note: string;
  symbols_requested: number;
  symbols_with_data: number;
  data_errors: Record<string, string>;
  source: string | null;
  coverage_end: number | null;
  coverage_end_iso: string | null;
  currency: string;
  capital_per_symbol: number;
  assumed_spread_pct: number;
  split_rule: string;
  disclaimer: string;
  live_gate: string;
}

export interface ProfileRunSummary {
  run_id: string;
  market: string;
  timeframe: string | null;
  days: number | null;
  horizon: string | null;
  generated_at: string | null;
  coverage_end: number | null;
  coverage_end_iso: string | null;
  source: string | null;
  symbols_with_data: number | null;
  currency: string | null;
  stages: number;
  verdict: ProfileVerdict | null;
}

export interface ProfileRun {
  meta: ProfileMeta;
  stages: ProfileStage[];
  verdict: ProfileVerdict;
  provenance: Provenance | null;
}

export interface ProfilesResponse {
  market: string;
  runs: ProfileRunSummary[];
  count: number;
  note: string | null;
  provenance: Provenance | null;
}

export async function getProfiles(market: ProfileMarket, signal?: AbortSignal): Promise<ProfilesResponse> {
  const response = await fetch(url("/profiles", { market }), { headers: { Accept: "application/json" }, signal });
  return unwrap<ProfilesResponse>(response);
}

export async function getProfileRun(runId: string, market: ProfileMarket, signal?: AbortSignal): Promise<ProfileRun> {
  const response = await fetch(url(`/profiles/${encodeURIComponent(runId)}`, { market }), {
    headers: { Accept: "application/json" },
    signal,
  });
  return unwrap<ProfileRun>(response);
}
