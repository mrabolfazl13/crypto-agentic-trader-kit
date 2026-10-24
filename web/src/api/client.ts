/**
 * Typed fetch layer over the local paper/research API (`/api/v1`).
 *
 * Contract: docs/API_CONTRACT_FA.md. Two rules are enforced here, in code:
 *  1. every numeric field the backend may leave empty is typed `number | null`
 *     so no page can accidentally render `undefined` as `0` or a sample value;
 *  2. there is no "go live" call anywhere in this file — the mode is read from
 *     `/health` and the only mutation of execution state is the kill switch.
 */

import { useEffect, useMemo, useState } from "react";

// Same-origin by default: the dev server proxies `/api`, `serve` mounts the API
// and the SPA together, and the edge nginx routes `/api/` to the backend.
export const API_BASE: string = (import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");
const PREFIX = "/api/v1";

/* ------------------------------------------------------------------ shared */

export type ProvenanceKind = "history" | "simulated" | "paper" | "live";

export interface Provenance {
  kind: ProvenanceKind;
  source: string | null;
  /** e.g. "2024-10-23T00:00Z → 2026-10-23T00:00Z" */
  window: string | null;
  generated_at: string | null;
}

export type Mode = "research" | "paper" | "PAPER" | "LIVE" | "PAUSED" | string;

/* ----------------------------------------------------------------- health */

export interface Health {
  status: string | null;
  schema_version: string | null;
  mode: Mode;
  live_trading_enabled: boolean | null;
  /** Backend sends a plain boolean; PaperTrading also tolerates an object for safety. */
  kill_switch: boolean | null;
  counts: Record<string, number | null>;
  clock?: Record<string, unknown> | null;
  /** Timestamp of the last paper cycle, when the backend reports it. */
  last_cycle_at?: string | null;
  /** Timestamp of the freshest market data the engine trusts. */
  last_data_at?: string | null;
  warnings?: string[] | null;
  errors?: string[] | null;
  generated_at: string | null;
}

/* ----------------------------------------------------------------- config */

export interface ConfigView {
  mode: Mode;
  exchange: string | null;
  market_type: string | null;
  symbols: string[];
  timeframe: string | null;
  confirm_timeframe: string | null;
  allocated_capital: number | null;
  maker_fee_pct: number | null;
  taker_fee_pct: number | null;
  strategy_params: Record<string, string | number | boolean | null> | null;
  limits: Record<string, number | null> | null;
  digest: string | null;
  warnings: string[];
  generated_at: string | null;
}

/* --------------------------------------------------------------- universe */

export interface UniverseEntry {
  rank: number | null;
  ticker: string;
  name: string;
  symbol: string;
  gate_base_name?: string | null;
  market_cap_usd?: number | null;
  volume_24h_usd?: number | null;
  price_usd_at_snapshot?: number | null;
}

export interface UniverseInfo {
  available: boolean;
  error?: string;
  /** Effective allowlist enforced by the risk gate. */
  symbols: string[];
  count: number;
  manifest_count?: number | null;
  requested_count?: number | null;
  manifest_symbols?: string[] | null;
  matches_config?: boolean | null;
  digest?: string | null;
  generated_at?: string | null;
  source?: Record<string, unknown> | null;
  entries?: UniverseEntry[] | null;
  excluded?: { rank: number | null; ticker: string; name: string; reason: string }[] | null;
  skipped?: { rank: number | null; ticker: string; name: string; reason: string }[] | null;
}

/* -------------------------------------------------------------- portfolio */

export interface PositionRow {
  symbol: string;
  qty: number | null;
  avg_entry: number | null;
  mark_price?: number | null;
  unrealized?: number | null;
  stop: number | null;
  target: number | null;
  decision_id: string | null;
  opened_at: string | null;
  mode?: Mode;
}

export interface Portfolio {
  mode: Mode;
  allocated_capital: number | null;
  equity: number | null;
  cash: number | null;
  unrealized: number | null;
  realized: number | null;
  day_pnl: number | null;
  week_pnl?: number | null;
  exposure_pct: number | null;
  drawdown_pct: number | null;
  peak_equity?: number | null;
  trades_today: number | null;
  max_trades_per_day?: number | null;
  consecutive_errors: number | null;
  fees_paid: number | null;
  /** Realized slippage is not recorded by the ledger today -> may be absent. */
  slippage_paid?: number | null;
  updated_at: string | null;
  positions: PositionRow[];
}

/* ----------------------------------------------------------------- trades */

export interface TradeRow {
  id: number | null;
  symbol: string;
  mode: Mode;
  timeframe: string | null;
  strategy_label: string | null;
  entry_ts: number | null;
  exit_ts: number | null;
  qty: number | null;
  entry: number | null;
  exit: number | null;
  gross: number | null;
  fees: number | null;
  net: number | null;
  reason: string | null;
  decision_id: string | null;
  stop: number | null;
  target: number | null;
  context?: Record<string, unknown> | null;
}

export interface TradesPage {
  mode: Mode;
  rows: TradeRow[];
  total: number | null;
  limit: number | null;
  offset: number | null;
  provenance: Provenance | null;
}

/* --------------------------------------------------------------- decision */

export interface Decision {
  id: string;
  ts: string | null;
  date: string | null;
  symbol: string;
  action: string | null;
  confidence: number | null;
  approved: boolean;
  mode: Mode;
  strategy_label: string | null;
  rationale: string[];
  risk: {
    reasons?: string[] | null;
    checks_passed?: string[] | null;
    qty?: number | null;
    notional?: number | null;
    entry?: number | null;
    stop?: number | null;
    target?: number | null;
    reward_risk_ratio?: number | null;
  } & Record<string, unknown>;
  extras: Record<string, unknown>;
  source: string | null;
}

/* ---------------------------------------------------------------- metrics */

/** Ledger / run metrics. Every figure may be `null` — never a placeholder. */
export interface Metrics {
  trades: number | null;
  wins?: number | null;
  losses?: number | null;
  win_rate_pct: number | null;
  profit_factor: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  net_pnl: number | null;
  net_return_pct?: number | null;
  fees_paid?: number | null;
  fees?: number | null;
  slippage_paid?: number | null;
  max_drawdown_pct: number | null;
  sharpe_annualised_crude: number | null;
  sortino_annualised_crude: number | null;
  bars?: number | null;
  /** Backend's own caveat about the Sharpe figure; shown in a tooltip. */
  sharpe_note?: string | null;
  [key: string]: number | string | null | undefined;
}

export interface PerformanceView {
  symbol: string | null;
  window: string | null;
  mode: Mode;
  metrics: Metrics;
  provenance: Provenance | null;
}

export type EquityPoint = [string | number, number | null, number | null];

export interface EquityCurve {
  mode: Mode;
  points: EquityPoint[];
  /** Optional buy-and-hold reference series, same x axis. */
  buy_hold?: ([string | number, number | null])[];
  allocated_capital?: number | null;
  provenance: Provenance | null;
}

/* ------------------------------------------------------------------- runs */

export type RunKind = "backtest" | "walk_forward" | "grid";

export interface RunSummary {
  id: string;
  kind: RunKind | string;
  status: string | null;
  symbol: string;
  timeframe: string | null;
  params_label: string | null;
  params: Record<string, unknown>;
  dataset_id: string | null;
  window_start: string | null;
  window_end: string | null;
  created_at: string | null;
  duration_ms: number | null;
  error: string | null;
  metrics: Metrics;
  split?: Record<string, unknown>;
}

export interface RunTradeRow {
  id: number | null;
  run_id?: string | null;
  symbol: string;
  entry_time: number | null;
  exit_time: number | null;
  entry: number | null;
  exit: number | null;
  qty: number | null;
  pnl: number | null;
  fees: number | null;
  reason: string | null;
}

export interface RejectionRow {
  reason: string;
  count: number | null;
}

/** One out-of-sample fold of a walk-forward evaluation. */
export interface FoldResult {
  label: string;
  window: string | null;
  net_return_pct: number | null;
  win_rate_pct: number | null;
  trades: number | null;
  max_drawdown_pct: number | null;
}

export interface RunDetail extends RunSummary {
  trades: RunTradeRow[];
  rejections: RejectionRow[];
  equity_curve: ([number, number])[];
  folds?: FoldResult[];
  accepted?: boolean | null;
  decision?: string | null;
  provenance?: Provenance | null;
}

/* ------------------------------------------------------------- strategies */

export interface StrategyVersion {
  digest: string;
  name: string;
  version: string | null;
  params: Record<string, unknown>;
  source: string | null;
  parent: string | null;
  created_at: string | null;
  accepted: boolean;
  decision: string | null;
  train: Metrics;
  validation: Metrics;
  test: Metrics;
  walk_forward: Metrics & { folds?: FoldResult[] };
  provenance?: Provenance | null;
}

/* ---------------------------------------------------------------- datasets */

export interface DatasetInfo {
  id: string;
  symbol: string;
  timeframe: string;
  /** `exchange` (fetched history) or `csv` (user import). */
  origin: string | null;
  source: string | null;
  path: string | null;
  start_ts: number | null;
  end_ts: number | null;
  bars: number | null;
  created_at: string | null;
  note: string | null;
  quality: DatasetQuality | null;
  provenance?: Provenance | null;
}

/**
 * Dataset quality envelope as `/datasets` and `/datasets/fetch` return it.
 * Unlike the flat `Quality` record, `gaps` here is a structured object.
 */
export interface DatasetQuality {
  source?: string | null;
  origin?: string | null;
  timeframe?: string | null;
  bars?: number | null;
  closed_bars?: number | null;
  dropped_unclosed?: number | null;
  duplicates?: number | null;
  impossible_ohlc?: number | null;
  non_positive_prices?: number | null;
  coverage_pct?: number | null;
  expected_bars?: number | null;
  gaps?: { count?: number | null; missing_bars?: number | null; starts?: (number | null)[] | null } | null;
  stale_seconds?: number | null;
  is_stale?: boolean | null;
  median_spacing?: number | null;
  first_ts?: number | null;
  last_ts?: number | null;
  first_iso?: string | null;
  last_iso?: string | null;
  requested_bars?: number | null;
  requested_days?: number | null;
  bar_ratio_pct?: number | null;
  warnings?: string[] | null;
}

export interface Quality {
  dataset_id: string | null;
  symbol: string | null;
  timeframe: string | null;
  origin: string | null;
  source: string | null;
  bars: number | null;
  gaps: number | null;
  duplicates: number | null;
  invalid_ohlc: number | null;
  open_bar: number | null;
  stale: number | null;
  rejected: number | null;
  warnings: string[];
  is_fresh?: boolean | null;
  provenance?: Provenance | null;
}

/** OHLC bar as stored in the `bars` table (epoch seconds, only closed bars). */
export interface Bar {
  t: number;
  open: number;
  high: number;
  low: number;
  close: number;
  base_volume?: number | null;
  quote_volume?: number | null;
  closed?: boolean;
}

export interface BarsPage {
  dataset_id: string | null;
  symbol: string | null;
  timeframe: string | null;
  bars: Bar[];
  /** Total closed bars in the dataset, when the backend reports it. */
  total_bars?: number | null;
  /** EMA / overlay series aligned to `bars` when the backend supplies them. */
  overlays?: { label: string; points: (number | null)[] }[];
  provenance: Provenance | null;
}

/* ---------------------------------------------------------------- analysis */

export interface AnalysisView {
  symbol: string | null;
  timeframe: string | null;
  dataset_id: string | null;
  regime: string | null;
  summary: string | null;
  signals: { bullish: string[]; bearish: string[] };
  indicators: Record<string, number | string | null>;
  volume_state: string | null;
  scenarios: Record<string, string>;
  invalidation: string | null;
  quality?: Partial<Quality> | null;
  generated_at: string | null;
  provenance: Provenance | null;
}

/* -------------------------------------------------------------------- risk */

export interface RiskState {
  mode: Mode;
  kill_switch_active: boolean | null;
  safe_state: boolean | null;
  caps: Record<string, number | null>;
  usage: {
    day_realized_pnl?: number | null;
    week_realized_pnl?: number | null;
    trades_today?: number | null;
    drawdown_pct?: number | null;
    exposure_pct?: number | null;
    consecutive_errors?: number | null;
    open_positions?: number | null;
  };
  breaches: string[];
  warnings: string[];
  generated_at: string | null;
  provenance?: Provenance | null;
}

/* -------------------------------------------------------------------- logs */

export interface LogRow {
  id: number | null;
  ts: string | null;
  level: string | null;
  source: string | null;
  message: string;
  context: Record<string, unknown>;
}

export interface OrdersView {
  mode: Mode;
  rows: {
    id: string | null;
    client_order_id: string | null;
    decision_id: string | null;
    symbol: string | null;
    side: string | null;
    qty: number | null;
    price: number | null;
    status: string | null;
    mode: Mode;
    created_ts: string | null;
    updated_ts: string | null;
  }[];
  provenance: Provenance | null;
}

export interface DailyReport {
  day: string;
  markdown: string;
  metrics?: Metrics | null;
  portfolio?: Partial<Portfolio> | null;
  provenance: Provenance | null;
}

export interface PaperCycleResult {
  run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  mode: Mode;
  kill_switch: boolean | null;
  safe_state: boolean | null;
  equity: number | null;
  unrealized: number | null;
  drawdown_pct: number | null;
  errors: string[];
  outcomes: {
    symbol: string;
    action: string | null;
    approved: boolean | null;
    reasons: string[];
    order_id?: string | null;
    decision_id?: string | null;
  }[];
  provenance: Provenance | null;
}

/* --------------------------------------------------------------- transport */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly path: string;

  constructor(status: number, detail: string, path: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.path = path;
  }
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export function url(path: string, query?: Query): string {
  const clean = `${API_BASE}${PREFIX}${path}`;
  if (!query) return clean;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${clean}?${qs}` : clean;
}

/**
 * Unwrap a JSON response, converting the backend's `{detail}` error payloads
 * (422 invalid range, 503 data unavailable / proxy down) into an ApiError with
 * the Persian message so pages can show it verbatim.
 */
export async function unwrap<T>(response: Response): Promise<T> {
  const path = new URL(response.url).pathname;
  let payload: unknown = null;
  const body = await response.text();
  if (body) {
    try {
      payload = JSON.parse(body);
    } catch {
      payload = { detail: body.slice(0, 300) };
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload && typeof (payload as { detail: unknown }).detail === "string"
        ? (payload as { detail: string }).detail
        : `خطای سرور (${String(response.status)})`;
    throw new ApiError(response.status, detail, path);
  }
  if (payload === null) throw new ApiError(response.status, "پاسخ خالی از سرور", path);
  return payload as T;
}

async function getJSON<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url(path, query), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  return unwrap<T>(response);
}

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url(path), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  return unwrap<T>(response);
}

/* ------------------------------------------------------------ read verbs */

export const getHealth = (signal?: AbortSignal) => getJSON<Health>("/health", undefined, signal);

export const getConfig = (signal?: AbortSignal) => getJSON<ConfigView>("/config", undefined, signal);

export const getUniverse = (signal?: AbortSignal) => getJSON<UniverseInfo>("/universe", undefined, signal);

export const getPortfolio = (signal?: AbortSignal) => getJSON<Portfolio>("/portfolio", undefined, signal);

export const getPerformance = (params: { symbol?: string | null; window?: string | null; mode?: string | null } = {}, signal?: AbortSignal) =>
  getJSON<PerformanceView>("/performance", { symbol: params.symbol, window: params.window, mode: params.mode }, signal);

export const getEquityCurve = (mode = "PAPER", signal?: AbortSignal) =>
  getJSON<EquityCurve | EquityPoint[]>("/equity-curve", { mode }, signal).then(normaliseEquity);

export const getOrders = (status = "open", signal?: AbortSignal) => getJSON<OrdersView>("/orders", { status }, signal);

export const getTrades = (
  params: { mode?: string | null; symbol?: string | null; outcome?: "win" | "loss" | null; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) => getJSON<TradesPage | TradeRow[]>("/trades", { ...params }, signal).then(normaliseTrades);

export const getDecisions = (params: { date?: string | null; symbol?: string | null; limit?: number } = {}, signal?: AbortSignal) =>
  getJSON<{ rows: Decision[] } | Decision[]>("/decisions", { ...params }, signal).then(normaliseDecisions);

export const getDatasets = (signal?: AbortSignal) =>
  getJSON<{ rows: DatasetInfo[] } | DatasetInfo[]>("/datasets", undefined, signal).then(normaliseDatasets);

export const getDatasetQuality = (datasetId: string, signal?: AbortSignal) =>
  getJSON<Quality>(`/datasets/${encodeURIComponent(datasetId)}/quality`, undefined, signal);

/**
 * NOT in the published contract yet: the candle chart needs closed bars.
 * The backend agent should expose `GET /datasets/{id}/bars?limit=&from=&to=`
 * returning `BarsPage`. Until then this rejects with a Persian no-data error
 * and the chart renders `داده‌ای موجود نیست` — never a synthetic series.
 */
export const getDatasetBars = (datasetId: string, params: { limit?: number } = {}, signal?: AbortSignal) =>
  getJSON<BarsPage>(`/datasets/${encodeURIComponent(datasetId)}/bars`, { ...params }, signal);

/** Exchange candles for one allow-listed symbol; provenance names the source and fetch time. */
export interface MarketCandles {
  symbol: string;
  timeframe: string;
  bars: Bar[];
  count: number;
  closed_count: number;
  provenance: Provenance & { fetched_at?: string | null; cached?: boolean };
}

export const getMarketCandles = (
  params: { symbol: string; timeframe?: string | null; limit?: number },
  signal?: AbortSignal,
) => getJSON<MarketCandles>("/market/candles", { ...params }, signal);

export const getAnalysis = (
  params: { symbol?: string | null; timeframe?: string | null; dataset_id?: string | null } = {},
  signal?: AbortSignal,
) => getJSON<AnalysisView>("/analysis", { ...params }, signal);

export const getRuns = (params: { kind?: RunKind | null; symbol?: string | null; limit?: number } = {}, signal?: AbortSignal) =>
  getJSON<{ rows: RunSummary[] } | RunSummary[]>("/runs", { ...params }, signal).then(normaliseRuns);

export const getRun = (runId: string, signal?: AbortSignal) => getJSON<RunDetail>(`/runs/${encodeURIComponent(runId)}`, undefined, signal);

export const getStrategies = (signal?: AbortSignal) =>
  getJSON<{ rows: StrategyVersion[] } | StrategyVersion[]>("/strategies", undefined, signal).then(normaliseStrategies);

export const getRisk = (signal?: AbortSignal) => getJSON<RiskState>("/risk", undefined, signal);

export const getLogs = (params: { level?: string | null; limit?: number } = {}, signal?: AbortSignal) =>
  getJSON<{ rows: LogRow[] } | LogRow[]>("/logs", { ...params }, signal).then(normaliseLogs);

export const getReport = (day: string, signal?: AbortSignal) => getJSON<DailyReport>(`/reports/${encodeURIComponent(day)}`, undefined, signal);

/* ----------------------------------------------------------- write verbs */

/** All of these stay inside research/paper. There is intentionally no live endpoint. */

export const importDataset = (body: { path?: string; upload_name?: string; symbol: string; timeframe: string; origin?: string }) =>
  postJSON<{ dataset_id: string; quality: Quality }>("/datasets/import", body);

export const fetchDataset = (body: { symbol: string; timeframe: string; days: number }) =>
  postJSON<{ dataset_id: string; quality: Quality }>("/datasets/fetch", body);

export const runBacktest = (body: {
  symbol: string;
  timeframe: string;
  days?: number;
  dataset_id?: string;
  params?: Record<string, unknown>;
  warmup?: number;
  assumed_spread_pct?: number;
}) => postJSON<RunDetail>("/runs/backtest", body);

export const runWalkForward = (body: {
  symbol: string;
  timeframe: string;
  days: number;
  grid: Record<string, unknown[]>;
  splits?: number;
}) => postJSON<RunDetail>("/runs/walk-forward", body);

export const runPaperCycle = () => postJSON<PaperCycleResult>("/cycles/paper", {});

/** The only execution-state mutation the UI offers: the emergency stop. */
export const setKillSwitch = (active: boolean) =>
  postJSON<{ kill_switch_active: boolean | null; message?: string | null }>("/kill-switch", { active });

/* ----------------------------------------------------------- normalisers */

function asArray<T>(payload: unknown, keys: string[]): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (payload && typeof payload === "object") {
    for (const key of keys) {
      const value = (payload as Record<string, unknown>)[key];
      if (Array.isArray(value)) return value as T[];
    }
  }
  return [];
}

export function normaliseTrades(payload: unknown): TradesPage {
  const rows = asArray<TradeRow>(payload, ["rows", "trades", "items"]);
  const wrapper = (Array.isArray(payload) ? {} : (payload ?? {})) as Record<string, unknown>;
  return {
    mode: String(wrapper["mode"] ?? "PAPER"),
    rows,
    total: typeof wrapper["total"] === "number" ? wrapper["total"] : null,
    limit: typeof wrapper["limit"] === "number" ? wrapper["limit"] : null,
    offset: typeof wrapper["offset"] === "number" ? wrapper["offset"] : null,
    provenance: (wrapper["provenance"] as Provenance | null) ?? null,
  };
}

export function normaliseDecisions(payload: unknown): Decision[] {
  return asArray<Decision>(payload, ["rows", "decisions", "items"]).map((row) => ({
    ...row,
    rationale: Array.isArray(row.rationale) ? row.rationale : [],
    risk: row.risk && typeof row.risk === "object" ? row.risk : {},
    extras: row.extras && typeof row.extras === "object" ? row.extras : {},
    approved: Boolean(row.approved),
  }));
}

export function normaliseRuns(payload: unknown): RunSummary[] {
  return asArray<RunSummary>(payload, ["rows", "runs", "items"]).map((row) => ({
    ...row,
    params: row.params ?? {},
    metrics: (row.metrics ?? {}) as Metrics,
  }));
}

export function normaliseStrategies(payload: unknown): StrategyVersion[] {
  return asArray<StrategyVersion>(payload, ["rows", "strategies", "items"]).map((row) => ({
    ...row,
    params: row.params ?? {},
    train: (row.train ?? {}) as Metrics,
    validation: (row.validation ?? {}) as Metrics,
    test: (row.test ?? {}) as Metrics,
    walk_forward: (row.walk_forward ?? {}) as Metrics,
    accepted: Boolean(row.accepted),
  }));
}

export function normaliseDatasets(payload: unknown): DatasetInfo[] {
  return asArray<DatasetInfo>(payload, ["rows", "datasets", "items"]);
}

export function normaliseLogs(payload: unknown): LogRow[] {
  return asArray<LogRow>(payload, ["rows", "logs", "items"]).map((row) => ({
    ...row,
    message: row.message ?? "",
    context: row.context && typeof row.context === "object" ? row.context : {},
  }));
}

export function normaliseEquity(payload: unknown): EquityCurve {
  const points = asArray<EquityPoint>(payload, ["points", "rows", "curve", "items"]);
  const wrapper = (Array.isArray(payload) ? {} : (payload ?? {})) as Record<string, unknown>;
  return {
    mode: String(wrapper["mode"] ?? "PAPER"),
    points,
    buy_hold: Array.isArray(wrapper["buy_hold"]) ? (wrapper["buy_hold"] as EquityCurve["buy_hold"]) : undefined,
    allocated_capital: typeof wrapper["allocated_capital"] === "number" ? wrapper["allocated_capital"] : null,
    provenance: (wrapper["provenance"] as Provenance | null) ?? null,
  };
}

/* --------------------------------------------------------------- the hook */

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** True for 503/fail-closed or empty payload: the UI must say "no data". */
  isEmpty: boolean;
  reload: () => void;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return `داده در دسترس نیست — سرور در حالت امن ماند: ${error.detail}`;
    if (error.status === 422) return `ورودی نامعتبر: ${error.detail}`;
    return error.detail;
  }
  if (error instanceof TypeError) return `اتصال به سرویس برقرار نشد (${error.message})`;
  if (error instanceof Error) return error.message;
  return "خطای ناشناخته";
}

/**
 * `useAsync(fn, deps)` — the only data-fetching primitive pages should use.
 * `fn` must be a stable-ish callback; deps control refetching. Aborts the
 * in-flight request when deps change or the component unmounts.
 */
export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [nonce, setNonce] = useState<number>(0);
  const memoKey = useMemo(() => deps.map((d) => (typeof d === "object" ? JSON.stringify(d) : String(d))).join("|"), [deps]);

  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    setError(null);
    fn(controller.signal)
      .then((value) => {
        if (!alive) return;
        setData(value);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!alive || controller.signal.aborted) return;
        setData(null);
        setError(describeError(err));
      })
      .finally(() => {
        if (alive && !controller.signal.aborted) setLoading(false);
      });
    return () => {
      alive = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoKey, nonce]);

  const isEmpty = !loading && (data === null || (Array.isArray(data) && data.length === 0));
  return { data, error, loading, isEmpty, reload: () => setNonce((n) => n + 1) };
}

export function useHealth(signal?: AbortSignal) {
  return useAsync((s) => getHealth(s ?? signal), []);
}
