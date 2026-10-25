/**
 * Typed client for the local quant research engine (`/api/v1/quant`).
 *
 * Rules match the rest of the API layer: absent numbers stay `number | null`
 * and are never rendered as zero. The only mutations are a manual PAPER
 * backtest and start/stop of the resumable research loop — there is no order,
 * mode or risk endpoint here, by design.
 */
import { unwrap, url } from "./client";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url(path), { method: "GET", headers: { Accept: "application/json" }, signal });
  return unwrap<T>(response);
}

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url(path), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  return unwrap<T>(response);
}

/* ------------------------------------------------------------------ types */

export interface QuantDataError {
  symbol: string | null;
  month: string | null;
  error: string | null;
  at: string | null;
}

export interface QuantDataSummary {
  data_version: string | null;
  months_expected: string[];
  months_stored: string[];
  symbols_stored: number | null;
  symbols_complete: number | null;
  rows: number | null;
  bytes: number | null;
  coverage_pct: number | null;
  missing_minutes: number | null;
  gaps: number | null;
  manifest_generated_at: string | null;
  errors: QuantDataError[];
}

export interface QuantStorageCategory {
  bytes: number | null;
  cap_bytes: number | null;
  used_pct: number | null;
  within_budget: boolean | null;
}

export interface QuantStorage {
  data_dir: string;
  categories: Record<string, QuantStorageCategory>;
  total_bytes: number | null;
  max_total_bytes: number | null;
  usable_total_bytes: number | null;
  reserve_bytes: number | null;
  used_pct: number | null;
  within_budget: boolean | null;
  disk_free_bytes: number | null;
  disk_free_gib: number | null;
  policy: string;
}

export interface QuantProcess {
  alive: boolean;
  pid?: number | null;
  started_at?: string | null;
  python?: string | null;
  log?: string | null;
  command?: string[];
}

export interface QuantLoopRun {
  id: string;
  started_at: string | null;
  ended_at: string | null;
  status: string | null;
  message: string | null;
  experiments: number | null;
  champion_id: string | null;
  resumable?: boolean;
}

export interface QuantExperimentMetrics {
  trades?: number | null;
  net_return_pct?: number | null;
  profit_factor?: number | null;
  max_drawdown_pct?: number | null;
  effective_drawdown_pct?: number | null;
  win_rate_pct?: number | null;
  fees?: number | null;
  exposure_pct?: number | null;
  benchmark_return_pct?: number | null;
  excess_return_pct?: number | null;
  rejections?: Record<string, number> | null;
}

export interface QuantWalkForward {
  folds?: number | null;
  positive_folds?: number | null;
  positive_fold_pct?: number | null;
  net_return_pct_sum?: number | null;
  trades?: number | null;
  max_drawdown_pct?: number | null;
}

export interface QuantExperiment {
  id: string;
  loop_id: string | null;
  seq: number | null;
  created_at: string | null;
  updated_at: string | null;
  strategy: string | null;
  timeframe: string | null;
  status: string | null;
  acceptance: string | null;
  reason: string | null;
  error: string | null;
  trials: number | null;
  duration_s: number | null;
  hypothesis: string | null;
  params: Record<string, unknown>;
  metrics: QuantExperimentMetrics;
  validation: QuantExperimentMetrics;
  walk_forward: QuantWalkForward | null;
  code_version: string | null;
  data_version: string | null;
}

export interface QuantChampion {
  experiment_id?: string | null;
  strategy?: string | null;
  timeframe?: string | null;
  params?: Record<string, unknown> | null;
  validation_net_pct?: number | null;
  validation_pf?: number | null;
  created_at?: string | null;
}

export interface QuantLoopStatus {
  process: QuantProcess;
  running: boolean;
  log_tail: string[];
  log_path: string;
  latest?: QuantLoopRun | null;
  counts?: Record<string, number | null>;
  last_resources?: { ts?: string | null; cpu_pct?: number | null; ram_mb?: number | null; disk_free_gb?: number | null } | null;
  champion?: QuantChampion | null;
  resources?: {
    cpu_pct: number | null;
    ram_used_gb: number | null;
    ram_total_gb: number | null;
    ram_pct: number | null;
    disk_free_gb: number | null;
    process_ram_mb: number | null;
  } | null;
  error?: string | null;
}

export interface QuantStatus {
  mode: string;
  live_trading_enabled: boolean;
  dependencies: Record<string, boolean>;
  enabled: boolean;
  reason?: string;
  server_time?: string;
  research_db?: string;
  database_bytes?: number | null;
  interpreter?: { python: string; probe: { ok: boolean; missing: string[]; error: string | null } | null };
  gates?: {
    min_trades: number;
    min_profit_factor: number;
    max_drawdown_pct: number;
    min_positive_fold_pct: number;
    max_experiments: number;
    trials_per_experiment: number;
    max_minutes: number;
  };
  data?: QuantDataSummary;
  storage?: QuantStorage;
  loop?: QuantLoopStatus;
  counts?: Record<string, number | null>;
  champion?: QuantChampion | null;
}

export interface QuantDataSymbol {
  symbol: string | null;
  rows: number | null;
  months: number | null;
  coverage_pct: number | null;
  bytes: number | null;
  first_iso: string | null;
  last_iso: string | null;
}

export interface QuantDataView {
  summary: QuantDataSummary;
  storage: QuantStorage;
  symbols: QuantDataSymbol[];
  note?: string;
}

export interface QuantBacktestRequest {
  strategy: string;
  timeframe: string;
  limit?: number;
  params?: Record<string, number>;
  position_pct?: number;
  walk_forward?: boolean;
  folds?: number;
  test_days?: number;
}

export interface QuantBacktestResult {
  kind: string;
  strategy?: string;
  timeframe?: string;
  metrics?: QuantExperimentMetrics;
  benchmark?: { return_pct?: number | null };
  trades_total?: number | null;
  symbols_evaluated?: number | null;
  elapsed_seconds?: number | null;
  folds?: { fold: number; metrics: QuantExperimentMetrics }[];
  aggregate?: QuantWalkForward;
  errors?: Record<string, string>;
  note?: string;
}

export interface QuantLoopStartRequest {
  resume: boolean;
  search_symbols: number;
  full_symbols?: number | null;
  max_experiments?: number | null;
  max_minutes?: number | null;
}

/* ---------------------------------------------------------------- requests */

export const getQuantStatus = (signal?: AbortSignal) => getJson<QuantStatus>("/quant/status", signal);

export const getQuantData = (limit = 100, signal?: AbortSignal) =>
  getJson<QuantDataView>(`/quant/data?limit=${encodeURIComponent(String(limit))}`, signal);

export const getQuantExperiments = (limit = 50, loopId?: string | null, signal?: AbortSignal) => {
  const query = loopId ? `?limit=${limit}&loop_id=${encodeURIComponent(loopId)}` : `?limit=${limit}`;
  return getJson<{ count: number; loop_id: string | null; items: QuantExperiment[] }>(`/quant/experiments${query}`, signal);
};

export const startQuantLoop = (body: QuantLoopStartRequest) =>
  postJson<{ started: boolean; pid: number; started_at: string; python: string; log: string; log_tail: string[] }>(
    "/quant/loop/start",
    body,
  );

export const stopQuantLoop = () =>
  postJson<{ stopped: boolean; pid?: number; reason?: string; still_alive?: boolean; note?: string }>("/quant/loop/stop", {});

export const runQuantBacktest = (body: QuantBacktestRequest) =>
  postJson<QuantBacktestResult>("/quant/backtest", body);
