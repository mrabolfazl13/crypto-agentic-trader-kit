/**
 * Typed client for the autonomous optimization loop (`/api/v1/optimizer`).
 *
 * Same rules as the rest of the API layer: absent numbers stay `number | null`
 * (nothing is converted into a fake zero) and the only mutations are start,
 * stop and resume of a research-only loop — there is no mode/risk endpoint here.
 */
import { unwrap, url } from "./client";

export type OptimizerStatus = "running" | "stopping" | "stopped" | "done" | "failed";

export interface OptimizerRunSummary {
  run_id: string;
  status: OptimizerStatus | string;
  symbol: string | null;
  timeframe: string | null;
  dataset_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  planned: number;
  completed: number;
  accepted: number;
  rejected: number;
  current: string | null;
  error: string | null;
  best_net_return_pct: number | null;
  champion_label: string | null;
  disclaimer?: string | null;
}

export interface OptimizerExperiment {
  index: number | null;
  hypothesis_id: string;
  question: string;
  grid: Record<string, unknown[]>;
  /** Entry-trigger family when the hypothesis changes it instead of a parameter. */
  strategy?: string;
  verdict: "accepted" | "rejected" | string | null;
  reasons: string[];
  candidate_label: string | null;
  champion_label: string | null;
  net_return_pct: number | null;
  max_drawdown_pct: number | null;
  profit_factor: number | null;
  trades: number | null;
  /** Candidate's net return on the validation window and the same window's fee-free buy&hold reference. */
  oos_net_return_pct?: number | null;
  champion_oos_net_return_pct?: number | null;
  buy_hold_pct?: number | null;
  /** Mark-to-market curves, percent of initial capital, sampled; absent on experiments recorded before this field existed. */
  oos_equity_curve?: [number, number][] | null;
  oos_benchmark_curve?: [number, number][] | null;
  champion_oos_equity_curve?: [number, number][] | null;
  dataset_id: string | null;
  split: Record<string, unknown> | null;
  learning: string;
  decided_at: string | null;
}

export interface OptimizerChampion {
  params_label: string | null;
  validation?: Record<string, unknown> | null;
  walk_forward?: Record<string, unknown> | null;
  accepted?: boolean;
}

export interface OptimizerState {
  run_id: string;
  status: OptimizerStatus | string;
  created_at: string | null;
  updated_at: string | null;
  symbol: string;
  timeframe: string;
  dataset_id: string;
  folds: number;
  warmup: number;
  spread_pct: number;
  queue: string[];
  current: string | null;
  experiments: OptimizerExperiment[];
  champion: OptimizerChampion | null;
  learnings: string[];
  next_reason: string;
  error: string | null;
  disclaimer: string;
}

export interface OptimizerStartBody {
  symbol: string;
  timeframe: string;
  days?: number;
  dataset_id?: string;
  max_experiments: number;
  start_at?: string;
  folds: number;
  warmup: number;
  assumed_spread_pct: number;
}

export interface RegisteredHypothesis {
  id: string;
  question: string;
  grid: Record<string, unknown[]>;
  /** Entry-trigger family from `strategy_lab`; empty when only parameters are searched. */
  strategy?: string;
}

/** `GET /optimizer` — newest runs first plus the standing disclaimer. */
export async function listOptimizerRuns(
  limit = 20,
  signal?: AbortSignal,
): Promise<{ runs: OptimizerRunSummary[]; hypotheses?: RegisteredHypothesis[]; disclaimer: string }> {
  const response = await fetch(url("/optimizer", { limit }), { headers: { Accept: "application/json" }, signal });
  return unwrap(response);
}

export async function getOptimizerRun(runId: string, signal?: AbortSignal): Promise<OptimizerState> {
  const response = await fetch(url(`/optimizer/${encodeURIComponent(runId)}`), { headers: { Accept: "application/json" }, signal });
  return unwrap(response);
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(url(path), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  return unwrap<T>(response);
}

export const startOptimizer = (body: OptimizerStartBody) => post<OptimizerState>("/optimizer/start", body);
export const stopOptimizer = (runId: string) => post<OptimizerState>(`/optimizer/${encodeURIComponent(runId)}/stop`, {});
export const resumeOptimizer = (runId: string) => post<OptimizerState>(`/optimizer/${encodeURIComponent(runId)}/resume`, {});

/** Download link for one run's history; built from the same base as every other call. */
export const optimizerExportUrl = (runId: string, format: "csv" | "json") =>
  url(`/optimizer/${encodeURIComponent(runId)}/export`, { format });
