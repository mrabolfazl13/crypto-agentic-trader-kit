/**
 * Typed client for the trade-analysis reports (`/api/v1/trade-analysis`).
 *
 * Same contract as the rest of the API layer: absent numbers stay
 * `number | null` (nothing is coerced into a fake zero) and every number the
 * UI shows comes from a stored report built on real backtest runs.
 */
import { unwrap, url } from "./client";

export interface GroupStat {
  key: string;
  label: string;
  group_by: string;
  n: number;
  sample_quality: "sufficient" | "weak" | "insufficient" | string;
  win_rate_pct: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  net_pnl: number;
  expectancy: number | null;
  expectancy_pct: number | null;
  profit_factor: number | null;
  max_drawdown_pnl: number;
  max_consecutive_losses: number;
  best_trade_share_pct: number | null;
  guidance: string;
}

/** What the engine recorded at decision time — never rewritten after the trade. */
export interface CodeRecorded {
  entry_reason: string | null;
  exit_reason: string | null;
  decision_time: number | null;
  confidence: number | null;
  regime: string | null;
  volume_state: string | null;
  bullish: string[];
  bearish: string[];
  indicators: Record<string, number | string | null>;
  initial_stop: number | null;
  initial_target: number | null;
}

export interface ExplanationLedger {
  symbol: string | null;
  run_id: string | null;
  entry_time: number | null;
  exit_time: number | null;
  entry: number | null;
  exit: number | null;
  qty: number | null;
  pnl: number | null;
  fees: number | null;
}

/** Clearly-labelled post-trade reading of the ledger + evidence; not the entry justification. */
export interface PostTradeAnalysis {
  realized_r: number | null;
  risk_pct: number | null;
  rr_ratio: number | null;
  mfe_pct: number | null;
  mae_pct: number | null;
  capital_pct: number | null;
  lines: string[];
  uncertainties: string[];
}

export interface TradeExplanation {
  code_recorded: CodeRecorded;
  ledger: ExplanationLedger;
  analysis: PostTradeAnalysis;
  note: string;
}

export interface TradeHypothesis {
  id: string;
  question: string;
  supporting_evidence: string[];
  opposing_evidence: string[];
  related_trades: number;
  mechanism: string;
  proposed_change: string;
  success_criterion: string;
  failure_criterion: string;
  required_experiment: string;
  grid: Record<string, unknown[]>;
  status: string;
}

export interface OverlapPair {
  symbols: string;
  overlaps: number;
  overlap_hours: number;
}

export interface OverlapReport {
  max_concurrent: number;
  peak_ts?: number | null;
  symbols: string[];
  pairs: OverlapPair[];
  note: string;
}

export interface RunLabel {
  run_id: string | null;
  symbol: string | null;
  timeframe: string | null;
  params_label: string | null;
  dataset_id: string | null;
  created_at: string | null;
}

export interface TradeAnalysisReport {
  report_id: string;
  generated_at: string;
  run_ids: string[];
  run_labels: RunLabel[];
  trade_count: number;
  symbols: string[];
  groups: Record<string, GroupStat[]>;
  overlap: OverlapReport;
  discovery: { hypotheses: TradeHypothesis[]; note: string };
  explanations: TradeExplanation[];
  note: string;
  config_snapshot?: Record<string, unknown> | null;
}

export interface TradeAnalysisSummary {
  report_id: string;
  generated_at: string | null;
  trade_count: number | null;
  symbols: string[] | null;
  run_ids: string[] | null;
}

/** `GET /trade-analysis` — newest reports first. */
export async function listTradeAnalysisReports(signal?: AbortSignal): Promise<TradeAnalysisSummary[]> {
  const response = await fetch(url("/trade-analysis"), { headers: { Accept: "application/json" }, signal });
  return unwrap(response);
}

export async function getTradeAnalysisReport(reportId: string, signal?: AbortSignal): Promise<TradeAnalysisReport> {
  const response = await fetch(url(`/trade-analysis/${encodeURIComponent(reportId)}`), { headers: { Accept: "application/json" }, signal });
  return unwrap(response);
}

/** `POST /trade-analysis` — assembles a fresh report from stored backtest runs. */
export async function buildTradeAnalysisReport(
  body: { run_ids?: string[] | null; limit_runs?: number },
  signal?: AbortSignal,
): Promise<TradeAnalysisReport> {
  const response = await fetch(url("/trade-analysis"), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  return unwrap(response);
}
