import { useMemo, useState } from "react";
import {
  describeError,
  getConfig,
  getHealth,
  getRuns,
  getStrategies,
  runWalkForward,
  useAsync,
  type Provenance,
  type RunSummary,
} from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import Filters from "../components/Filters";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks, { EmptyBlock, LoadingBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import MetricBars from "../charts/MetricBars";
import { NO_DATA, dateTime, num, pct, signedMoney, text, tone } from "../format";

/* -------------------------------------------------------------------------- */
/* Local helpers.                                                             */
/* `api/client.ts` and `components/*` are shared with other agents, so the     */
/* defensive readers for the research payload live here, in this page file.    */
/* The walk-forward payload is read key-by-key from `unknown`: a key the        */
/* backend has not finalised renders «داده‌ای موجود نیست» instead of a guess.   */
/* -------------------------------------------------------------------------- */

type Rec = Record<string, unknown>;

function asRec(value: unknown): Rec {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Rec) : {};
}

function numAt(value: unknown, ...keys: string[]): number | null {
  const source = asRec(value);
  for (const key of keys) {
    const raw = source[key];
    if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  }
  return null;
}

function strAt(value: unknown, ...keys: string[]): string | null {
  const source = asRec(value);
  for (const key of keys) {
    const raw = source[key];
    if (typeof raw === "string" && raw.trim() !== "" && raw !== "no_data") return raw;
  }
  return null;
}

function boolAt(value: unknown, ...keys: string[]): boolean | null {
  const source = asRec(value);
  for (const key of keys) {
    const raw = source[key];
    if (typeof raw === "boolean") return raw;
    if (raw === 1) return true;
    if (raw === 0) return false;
  }
  return null;
}

function listAt(value: unknown, key: string): unknown[] {
  const raw = asRec(value)[key];
  return Array.isArray(raw) ? raw : [];
}

/** `evaluate()` blocks nest their numbers under `metrics`; stored versions may already be flat. */
function metricsOf(block: unknown): Rec {
  const source = asRec(block);
  const nested = asRec(source["metrics"]);
  return Object.keys(nested).length > 0 ? nested : source;
}

function reasonPairs(value: unknown): { reason: string; count: number | null }[] {
  return Object.entries(asRec(value)).map(([reason, count]) => ({ reason, count: typeof count === "number" ? count : null }));
}

/* --------------------------------------------------- research payload view */

interface SegmentView {
  name: string;
  label: string;
  bars: number | null;
  window: string | null;
  gapFree: boolean | null;
  contiguous: boolean | null;
}

interface BlockView {
  label: string;
  metrics: Rec;
  trades: number | null;
  bars: number | null;
  window: string | null;
  paramsLabel: string | null;
  buyHold: number | null;
  rejections: { reason: string; count: number | null }[];
}

interface FoldView {
  label: string;
  window: string | null;
  trainWindow: string | null;
  net: number | null;
  profitFactor: number | null;
  trades: number | null;
  drawdown: number | null;
  winRate: number | null;
  buyHold: number | null;
  testBars: number | null;
}

interface GridCandidate {
  paramsLabel: string | null;
  trainNet: number | null;
  validationNet: number | null;
}

interface RiskCheck {
  key: string;
  passed: boolean | null;
}

interface ResearchView {
  verdict: string | null;
  accepted: boolean | null;
  reasons: string[];
  decision: string | null;
  candidateLabel: string | null;
  championLabel: string | null;
  symbol: string | null;
  timeframe: string | null;
  mode: string | null;
  datasetId: string | null;
  generatedAt: string | null;
  segments: SegmentView[];
  train: BlockView | null;
  validation: BlockView | null;
  test: BlockView | null;
  folds: FoldView[];
  aggregate: Rec | null;
  championAggregate: Rec | null;
  positiveFolds: number | null;
  totalFolds: number | null;
  candidates: GridCandidate[];
  riskChecks: RiskCheck[];
  minTrades: number | null;
  testNote: string | null;
  disclaimer: string | null;
  oosNote: string | null;
  provenance: Provenance | null;
}

function toBlock(label: string, block: unknown): BlockView | null {
  const source = asRec(block);
  if (Object.keys(source).length === 0) return null;
  const metrics = metricsOf(source);
  const start = strAt(source, "start_iso") ?? dateTime(numAt(source, "start"));
  const end = strAt(source, "end_iso") ?? dateTime(numAt(source, "end"));
  return {
    label,
    metrics,
    trades: numAt(metrics, "trades") ?? numAt(source, "trades"),
    bars: numAt(metrics, "bars") ?? numAt(source, "bars"),
    window: start !== NO_DATA && end !== NO_DATA ? `${start} ← ${end}` : null,
    paramsLabel: strAt(source, "params_label"),
    buyHold: numAt(metrics, "buy_hold_pct") ?? numAt(source, "buy_hold_pct"),
    rejections: reasonPairs(source["rejected"]),
  };
}

function toSegments(split: unknown): SegmentView[] {
  const labels: Record<string, string> = { train: "آموزش", validation: "اعتبارسنجی", test: "آزمون (hold-out)" };
  return ["train", "validation", "test"].map((name) => {
    const part = asRec(asRec(split)[name]);
    const start = strAt(part, "start");
    const end = strAt(part, "end");
    return {
      name,
      label: labels[name] ?? name,
      bars: numAt(part, "bars"),
      window: start && end ? `${start} ← ${end}` : null,
      gapFree: boolAt(part, "gap_free"),
      contiguous: boolAt(part, "contiguous_with_next"),
    };
  });
}

function toFolds(walkForward: unknown): FoldView[] {
  return listAt(walkForward, "folds").map((raw, index) => {
    const fold = asRec(raw);
    const metrics = metricsOf(fold["test_metrics"]);
    const testStart = strAt(fold, "test_start");
    const testEnd = strAt(fold, "test_end");
    const trainStart = strAt(fold, "train_start");
    const trainEnd = strAt(fold, "train_end");
    const number = numAt(fold, "fold");
    return {
      label: `فولد ${String(number ?? index + 1)}`,
      window: testStart && testEnd ? `${testStart} ← ${testEnd}` : null,
      trainWindow: trainStart && trainEnd ? `${trainStart} ← ${trainEnd}` : null,
      net: numAt(metrics, "net_return_pct"),
      profitFactor: numAt(metrics, "profit_factor"),
      trades: numAt(metrics, "trades"),
      drawdown: numAt(metrics, "max_drawdown_pct"),
      winRate: numAt(metrics, "win_rate_pct"),
      buyHold: numAt(fold, "buy_hold_pct"),
      testBars: numAt(fold, "test_bars"),
    };
  });
}

function toRiskChecks(checks: unknown): RiskCheck[] {
  return Object.entries(asRec(checks)).map(([key, value]) => ({ key, passed: typeof value === "boolean" ? value : null }));
}

/** Everything comes from the response; a missing key never becomes a number. */
function toResearch(payload: unknown): ResearchView {
  const root = asRec(payload);
  const walkForward = root["walk_forward"];
  const championForward = root["champion_walk_forward"];
  const consistency = asRec(asRec(walkForward)["consistency"]);
  const verdict = strAt(root, "verdict");
  const accepted = boolAt(root, "accepted");
  const reasonsRaw = Array.isArray(root["reasons"]) ? (root["reasons"] as unknown[]) : [];
  return {
    verdict,
    accepted: accepted ?? (verdict === null ? null : verdict.toLowerCase() === "accepted"),
    reasons: reasonsRaw.map((item) => String(item)),
    decision: strAt(root, "decision"),
    candidateLabel: strAt(root, "candidate_label") ?? strAt(asRec(walkForward), "params_label"),
    championLabel: strAt(root, "champion_label"),
    symbol: strAt(root, "symbol"),
    timeframe: strAt(root, "timeframe"),
    mode: strAt(root, "mode"),
    datasetId: strAt(root, "dataset_id"),
    generatedAt: strAt(root, "generated_at"),
    segments: toSegments(root["split"]),
    train: toBlock("آموزش (train)", root["train"]),
    validation: toBlock("اعتبارسنجی (validation)", root["validation"]),
    test: toBlock("آزمون (test)", root["test"]),
    folds: toFolds(walkForward),
    aggregate: Object.keys(asRec(asRec(walkForward)["aggregate"])).length > 0 ? asRec(asRec(walkForward)["aggregate"]) : null,
    championAggregate:
      Object.keys(asRec(asRec(championForward)["aggregate"])).length > 0 ? asRec(asRec(championForward)["aggregate"]) : null,
    positiveFolds: numAt(consistency, "positive_folds"),
    totalFolds: numAt(consistency, "total_folds"),
    candidates: listAt(root, "grid").map((raw) => {
      const entry = asRec(raw);
      return {
        paramsLabel: strAt(entry, "params_label"),
        trainNet: numAt(entry, "train_net_return_pct"),
        validationNet: numAt(entry, "validation_net_return_pct"),
      };
    }),
    riskChecks: toRiskChecks(root["risk_checks"]),
    minTrades: numAt(root, "min_trades"),
    testNote: strAt(root, "test_note"),
    disclaimer: strAt(root, "disclaimer"),
    oosNote: strAt(walkForward, "note"),
    provenance: (root["provenance"] as Provenance | undefined) ?? null,
  };
}

/* ------------------------------------------------------------- edge verdict */

type EdgeState = "no_data" | "untested" | "not_profitable" | "measured_positive";

/** Out-of-sample evidence only (walk-forward aggregate or validation). Never train. */
function edgeState(aggregate: Rec | null): { state: EdgeState; net: number | null; profitFactor: number | null } {
  const net = numAt(aggregate, "net_return_pct", "net_return_pct_median");
  const profitFactor = numAt(aggregate, "profit_factor", "profit_factor_mean");
  if (aggregate === null || (net === null && profitFactor === null)) return { state: "untested", net, profitFactor };
  if (net !== null && net > 0 && profitFactor !== null && profitFactor >= 1) return { state: "measured_positive", net, profitFactor };
  return { state: "not_profitable", net, profitFactor };
}

const EDGE_COPY: Record<EdgeState, { title: string; body: string; tone: "neg" | "warn" | "neutral" }> = {
  no_data: {
    title: "هنوز استراتژی سودآور تأیید نشده است",
    body: "هیچ ارزیابی برون‌نمونه‌ای ثبت نشده است؛ تا وقتی walk-forward اجرا نشود، هیچ ادعایی درباره‌ی لبه‌ی سودآوری وجود ندارد.",
    tone: "warn",
  },
  untested: {
    title: "هنوز استراتژی سودآور تأیید نشده است",
    body: "پاسخ سرور عدد برون‌نمونه‌ای کاملی برنگردانده است (net return یا profit factor روی داده‌ی خارج از نمونه). بدون این دو عدد، پذیرش معنایی ندارد و عددی ساخته نمی‌شود.",
    tone: "warn",
  },
  not_profitable: {
    title: "هنوز استراتژی سودآور تأیید نشده است",
    body: "بازده خالص برون‌نمونه‌ای و/یا profit factor زیر آستانه‌ی سودآوری است. بک‌ست‌های پیشین این ساختار حدود ۰٪ تا −۲٪ خالص با profit factor کوچک‌تر از ۱ بوده‌اند؛ این وضعیت واقعی و قابل‌ثبت است، نه نقص UI.",
    tone: "neg",
  },
  measured_positive: {
    title: "نتیجه‌ی برون‌نمونه‌ای مثبت ثبت شد — نه تضمین",
    body: "در همین پنجره‌ی اندازه‌گیری، بازده خالص مثبت و profit factor ≥ ۱ ثبت شده است. این عدد پیش‌بینی نیست؛ پذیرش فقط با گذشتن از همه‌ی دروازه‌های ضد-overfitting انجام می‌شود.",
    tone: "neutral",
  },
};

/* ------------------------------------------------------ parameter dictionary */

type ParamKind = "number" | "boolean" | "text";

interface ParamMeta {
  label: string;
  effect: string;
  kind: ParamKind;
  /** Documented bounds enforced by research.PARAM_SPACE_HINTS; absent = not documented. */
  bounds?: readonly [number, number];
  options?: string[];
}

const PARAM_META: Record<string, ParamMeta> = {
  ema_fast: { label: "میانگین متحرک سریع (EMA fast)", effect: "کاهش: سیگنال‌های زودتر و بیشتر، نویز بالاتر · افزایش: دیرتر و کمتر", kind: "number", bounds: [5, 60] },
  ema_slow: { label: "میانگین متحرک کند (EMA slow)", effect: "کاهش: تقاطع‌های بیشتر ولی سطحی · افزایش: فقط روندهای بلندمدت", kind: "number", bounds: [20, 240] },
  rsi_period: { label: "دوره‌ی RSI", effect: "کاهش: RSI نوسانی‌تر، ورودهای بیشتر · افزایش: نرم‌تر، ورود کمتر", kind: "number", bounds: [5, 40] },
  atr_period: { label: "دوره‌ی ATR", effect: "کاهش: اندازه‌گیری نوسان واکنشی‌تر · افزایش: سقف/هدف پایدارتر", kind: "number", bounds: [5, 40] },
  atr_stop_mult: { label: "ضریب حد ضرر (ATR)", effect: "کاهش: حد ضرر نزدیک‌تر، زیان کوچک‌تر ولی برد بیشتر · افزایش: تنفس بیشتر، زیان بزرگ‌تر هر باخت", kind: "number", bounds: [0.5, 4] },
  atr_target_mult: { label: "ضریب هدف قیمت (ATR)", effect: "افزایش: ریسک‌به‌بافت خواسته‌شده بزرگ‌تر ولی احتمال رسیدن به هدف کمتر · کاهش: خروج زودتر — این عدد ضریب قیمت است، نه وعده‌ی سود", kind: "number", bounds: [1, 8] },
  min_bars: { label: "حداقل کندل‌های تاریخچه", effect: "افزایش: تحلیل فقط روی داده‌ی بیشتر (ممکن است سیگنال را حذف کند) · کاهش: ورود با داده‌ی ناکافی", kind: "number", bounds: [50, 300] },
  vol_lookback: { label: "دوره‌ی محاسبه‌ی نوسان", effect: "کاهش: آستانه‌های نوسان سریع تغییر می‌کنند · افزایش: صاف‌شده و کند", kind: "number", bounds: [10, 120] },
  slope_lookback: { label: "دوره‌ی شیب روند", effect: "کاهش: تشخیص روند حساس‌تر · افزایش: فقط شیب پایدار", kind: "number", bounds: [10, 120] },
  volume_avg_bars: { label: "دوره‌ی میانگین حجم", effect: "کاهش: مقایسه‌ی حجم با پنجره‌ی کوتاه · افزایش: معیار حجمی بلندمدت‌تر", kind: "number", bounds: [10, 120] },
  regime_gate: {
    label: "دروازه‌ی رژیم بازار",
    effect: "ورود فقط در رژیم انتخاب‌شده؛ تنگ‌تر کردن = فیلتر سخت‌تر و معامله‌ی کمتر",
    kind: "text",
    options: ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE", "ANY"],
  },
  require_multiple_signals: { label: "نیاز به چند سیگنال هم‌زمان", effect: "روشن: تأیید بیشتر، ورود کمتر و دیرتر · خاموش: ورود زودتر با ریسک نویز", kind: "boolean" },
  no_trade_if_signals_conflict: { label: "رد سیگنال در تضاد", effect: "روشن: اگر سیگنال‌ها مخالف هم باشند معامله نمی‌شود · خاموش: ریسک تصمیم‌گیری روی داده‌ی متناقض", kind: "boolean" },
  rsi_bullish_low: { label: "کف بازه‌ی RSI صعودی", effect: "کاهش: اجازه‌ی خرید در RSI پایین‌تر · افزایش: فقط pullbackهای کم‌عمق", kind: "number", bounds: [20, 60] },
  rsi_bullish_high: { label: "سقف بازه‌ی RSI صعودی", effect: "افزایش: تحمل RSI بالاتر · کاهش: خروج زودتر از ناحیه‌ی خرید", kind: "number", bounds: [45, 85] },
  rsi_stretched: { label: "آستانه‌ی RSI بیش‌ازحد گرم", effect: "کاهش: رد سریع‌تر ورود (ریسک سقف)، ورودهای کمتر", kind: "number", bounds: [60, 95] },
  rsi_weak: { label: "آستانه‌ی RSI ضعیف", effect: "افزایش: حساسیت بیشتر به ضعف، معاملات کمتر", kind: "number", bounds: [15, 50] },
  volume_collapse_ratio: { label: "نسبت ریزش حجم", effect: "افزایش: ریزش حجم زودتر رد می‌شود · کاهش: تحمل حجم پایین", kind: "number" },
  volume_weak_ratio: { label: "نسبت حجم ضعیف", effect: "افزایش: تأیید حجم سخت‌گیرانه‌تر", kind: "number" },
  slope_threshold: { label: "آستانه‌ی شیب", effect: "کاهش: روندهای ضعیف هم می‌پذیرد (نویز) · افزایش: فقط شیب قوی", kind: "number", bounds: [0, 1] },
  vol_threshold: { label: "آستانه‌ی نوسان", effect: "کاهش: ورود در بازار آرام · افزایش: فقط بازار پرنوسان", kind: "number", bounds: [0, 10] },
  atr_pct_threshold: { label: "آستانه‌ی درصد ATR", effect: "کاهش: آستانه‌های قیمت کوچک‌تر (حد ضرر نزدیک‌تر) · افزایش: فاصله‌ی بیشتر", kind: "number", bounds: [0.1, 10] },
};

/** Risk caps are owned by config.yaml — they are deliberately not grid targets. */
const RISK_FORBIDDEN = [
  "max_risk_per_trade_pct",
  "max_drawdown_pct",
  "allocated_capital",
  "max_position_pct_of_allocated_capital",
  "max_account_allocation_pct",
  "max_trades_per_day",
  "max_daily_loss_pct",
  "stop_on_consecutive_errors",
];

const GRID_CAP = 60;
const GRID_CAP_MESSAGE = `شبکه بیش از حد بزرگ است: سقف ۶۰ ارزیابی (پیام فارسیِ همان ۴۲۲ سرور).`;

/* ------------------------------------------------------------ grid editor */

interface GridRow {
  id: string;
  param: string;
  raw: string;
  trueOn: boolean;
  falseOn: boolean;
}

interface RowParse {
  values: (string | number | boolean)[];
  invalid: string[];
  outOfBounds: string[];
}

function parseRow(row: GridRow): RowParse {
  const meta = PARAM_META[row.param];
  if (!meta) return { values: [], invalid: ["پارامتر نامشخص"], outOfBounds: [] };
  if (meta.kind === "boolean") {
    const values = [row.trueOn ? true : null, row.falseOn ? false : null].filter((v): v is boolean => v !== null);
    return { values, invalid: values.length === 0 ? ["حداقل یک مقدار منطقی را انتخاب کنید"] : [], outOfBounds: [] };
  }
  const tokens = row.raw
    .split(/[,،\s]+/)
    .map((token) => token.trim())
    .filter((token) => token !== "");
  if (meta.kind === "text") {
    return { values: tokens, invalid: [], outOfBounds: [] };
  }
  const numbers: number[] = [];
  const invalid: string[] = [];
  const outOfBounds: string[] = [];
  for (const token of tokens) {
    const value = Number(token);
    if (!Number.isFinite(value)) {
      invalid.push(token);
      continue;
    }
    if (meta.bounds && (value < meta.bounds[0] || value > meta.bounds[1])) {
      outOfBounds.push(`${token} (خارج از [${String(meta.bounds[0])}, ${String(meta.bounds[1])}])`);
      continue;
    }
    numbers.push(value);
  }
  return { values: numbers, invalid, outOfBounds };
}

function productOf(counts: number[]): number {
  return counts.reduce((total, count) => total * Math.max(1, count), 1);
}

/* --------------------------------------------------------------- the page */

let rowSeq = 0;
function nextRowId(): string {
  rowSeq += 1;
  return `grid-row-${String(rowSeq)}`;
}

export default function StrategyLab() {
  const health = useAsync((signal) => getHealth(signal), []);
  const config = useAsync((signal) => getConfig(signal), []);
  const runs = useAsync((signal) => getRuns({ kind: "walk_forward", limit: 15 }, signal), []);
  const strategies = useAsync((signal) => getStrategies(signal), []);

  const [symbol, setSymbol] = useState<string>("");
  const [timeframe, setTimeframe] = useState<string>("");
  const [days, setDays] = useState<string>("365");
  const [folds, setFolds] = useState<string>("4");
  const [rows, setRows] = useState<GridRow[]>([{ id: nextRowId(), param: "ema_slow", raw: "40, 50, 60", trueOn: true, falseOn: false }]);
  const [result, setResult] = useState<ResearchView | null>(null);
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  const symbols = config.data?.symbols ?? [];
  const activeSymbol = symbol || symbols[0] || "";
  const resolvedTimeframe = timeframe || config.data?.timeframe || "";
  const timeframes = useMemo(() => {
    const fromConfig = [config.data?.timeframe, config.data?.confirm_timeframe].filter((v): v is string => Boolean(v));
    const base = ["15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"];
    return [...new Set([...fromConfig, ...base])];
  }, [config.data]);

  const paramOptions = useMemo(() => {
    const fromConfig = Object.keys(config.data?.strategy_params ?? {}).filter((key) => PARAM_META[key]);
    const keys = fromConfig.length > 0 ? fromConfig : Object.keys(PARAM_META);
    return keys.map((key) => ({ value: key, label: PARAM_META[key]?.label ?? key }));
  }, [config.data]);

  const parsed = useMemo(() => rows.map(parseRow), [rows]);
  const evaluations = productOf(parsed.map((item) => item.values.length));
  const valueIssues = parsed.flatMap((item, index) => [...item.invalid, ...item.outOfBounds].map((problem) => `ردیف ${String(index + 1)}: ${problem}`));
  const gridReady = parsed.some((item) => item.values.length > 0) && valueIssues.length === 0 && evaluations <= GRID_CAP;
  const daysNumber = Number(days);
  const foldsNumber = Number(folds);
  const duplicateParams = rows.map((row) => row.param).filter((param, index, all) => all.indexOf(param) !== index);
  const inputIssues = [
    activeSymbol ? null : "نماد از /config خوانده نشد؛ برای اجرای تحقیق یک نماد لازم است.",
    resolvedTimeframe ? null : "تایم‌فریم مشخص نیست.",
    duplicateParams.length === 0 ? null : `پارامتر تکراری در شبکه: ${duplicateParams.join("، ")} — هر فیلد فقط یک بار مجاز است.`,
    Number.isFinite(daysNumber) && daysNumber >= 1 && daysNumber <= 1500 ? null : "days باید عددی بین ۱ تا ۱۵۰۰ باشد (اعتبار سرور).",
    Number.isInteger(foldsNumber) && foldsNumber >= 2 && foldsNumber <= 12 ? null : "folds باید عددی بین ۲ و ۱۲ باشد (walk-forward با یک پنجره هیچ چیزی اثبات نمی‌کند).",
  ].filter((item): item is string => item !== null);

  const canSubmit = gridReady && inputIssues.length === 0 && !running;

  async function submit(): Promise<void> {
    if (!canSubmit) return;
    const grid: Record<string, unknown[]> = {};
    rows.forEach((row, index) => {
      const values = parsed[index]?.values ?? [];
      if (values.length > 0) grid[row.param] = values;
    });
    type Body = Parameters<typeof runWalkForward>[0] & { folds: number };
    const body: Body = { symbol: activeSymbol, timeframe: resolvedTimeframe, days: daysNumber, grid, folds: foldsNumber };
    setRunning(true);
    setRunError(null);
    try {
      const payload = await runWalkForward(body);
      setResult(toResearch(payload));
      runs.reload();
      strategies.reload();
    } catch (error: unknown) {
      setResult(null);
      setRunError(describeError(error));
    } finally {
      setRunning(false);
    }
  }

  /* ---- champion / historical edge evidence (independent of a fresh run) ---- */
  const champion = useMemo(() => {
    const rowsOfVersions = strategies.data ?? [];
    return rowsOfVersions.find((row) => row.accepted) ?? null;
  }, [strategies.data]);

  const championEvidence = useMemo(() => {
    if (!champion) return null;
    const forward = asRec(champion.walk_forward);
    const aggregate = asRec(forward["aggregate"]);
    return edgeState(Object.keys(aggregate).length > 0 ? aggregate : metricsOf(champion.validation));
  }, [champion]);

  const displayEdge = result !== null ? edgeState(result.aggregate) : championEvidence ?? { state: "no_data", net: null, profitFactor: null };
  const edgeCopy = EDGE_COPY[displayEdge.state];

  /* ------------------------------ result tables --------------------------- */
  interface CompareRow {
    key: string;
    label: string;
    train: number | null;
    validation: number | null;
    test: number | null;
    kind: "pct" | "num" | "count" | "money";
    note?: string;
  }

  const compareKeys: { key: string; label: string; kind: CompareRow["kind"]; note?: string }[] = [
    { key: "net_return_pct", label: "بازده خالص پس از کارمزد", kind: "pct" },
    { key: "profit_factor", label: "ضریب سود (profit factor)", kind: "num" },
    { key: "max_drawdown_pct", label: "بیشترین افت", kind: "pct" },
    { key: "trades", label: "تعداد معامله", kind: "count" },
    { key: "win_rate_pct", label: "نرخ برد", kind: "pct" },
    { key: "net_pnl", label: "خالص (USDT)", kind: "money" },
    { key: "fees_paid", label: "کارمزد (USDT)", kind: "money" },
    { key: "sharpe_annualised_crude", label: "شارپ (تحویلی و خام)", kind: "num", note: "بدون نرخ بدون‌ریسک؛ قابل مقایسه با یک بک‌تست دیگر، نه پیش‌بینی" },
  ];

  const compareRows: CompareRow[] = compareKeys.map((definition) => ({
    key: definition.key,
    label: definition.label,
    kind: definition.kind,
    note: definition.note,
    train: numAt(result?.train?.metrics, definition.key),
    validation: numAt(result?.validation?.metrics, definition.key),
    test: numAt(result?.test?.metrics, definition.key),
  }));

  function renderMetric(value: number | null, kind: CompareRow["kind"]): string {
    if (value === null) return NO_DATA;
    if (kind === "pct") return pct(value, { max: 2 });
    if (kind === "count") return num(value, { max: 0 });
    if (kind === "money") return signedMoney(value);
    return num(value, { max: 3 });
  }

  const compareColumns: Column<CompareRow>[] = [
    {
      key: "label",
      header: "شاخص",
      render: (row) => (
        <span>
          {row.label}
          {row.note ? <span className="mute"> · {row.note}</span> : null}
        </span>
      ),
      sortValue: (row) => row.label,
    },
    {
      key: "train",
      header: "آموزش (in-sample)",
      numeric: true,
      sortValue: (row) => row.train,
      render: (row) => <span className="num">{renderMetric(row.train, row.kind)}</span>,
    },
    {
      key: "validation",
      header: "اعتبارسنجی (برون‌نمونه‌ای)",
      numeric: true,
      sortValue: (row) => row.validation,
      render: (row) => <span className="num">{renderMetric(row.validation, row.kind)}</span>,
    },
    {
      key: "test",
      header: "آزمون (hold-out)",
      numeric: true,
      sortValue: (row) => row.test,
      render: (row) => <span className="num">{renderMetric(row.test, row.kind)}</span>,
    },
    {
      key: "gap",
      header: "فاصله‌ی آموزش ← آزمون",
      numeric: true,
      sortValue: (row) => (row.train !== null && row.test !== null ? row.test - row.train : null),
      render: (row) => {
        if (row.train === null || row.test === null || row.kind === "money" || row.kind === "count") return NO_DATA;
        const gap = row.test - row.train;
        return (
          <span className={`num tone-${Math.abs(gap) >= 1 ? "neg" : "neutral"}`} title="هرچه فاصله أكبر، نشانه‌ی قوی‌تر overfitting">
            {pct(gap, { max: 2 })}
          </span>
        );
      },
    },
  ];

  const foldColumns: Column<FoldView>[] = [
    { key: "label", header: "پنجره", render: (row) => <span className="num">{row.label}</span>, sortValue: (row) => row.label },
    { key: "window", header: "بازه‌ی آزمون", render: (row) => text(row.window) },
    {
      key: "net",
      header: "بازده خالص",
      numeric: true,
      sortValue: (row) => row.net,
      render: (row) => <span className={`tone-${tone(row.net)}`}>{pct(row.net, { max: 2 })}</span>,
    },
    { key: "pf", header: "ضریب سود", numeric: true, sortValue: (row) => row.profitFactor, render: (row) => num(row.profitFactor, { max: 3 }) },
    { key: "trades", header: "معامله", numeric: true, sortValue: (row) => row.trades, render: (row) => num(row.trades, { max: 0 }) },
    { key: "dd", header: "بیشترین افت", numeric: true, sortValue: (row) => row.drawdown, render: (row) => pct(row.drawdown, { max: 2 }) },
    { key: "bh", header: "خرید و نگه‌داشتن", numeric: true, sortValue: (row) => row.buyHold, render: (row) => pct(row.buyHold, { max: 2 }) },
    { key: "bars", header: "کندل آزمون", numeric: true, sortValue: (row) => row.testBars, render: (row) => num(row.testBars, { max: 0 }) },
  ];

  const candidateColumns: Column<GridCandidate>[] = [
    { key: "label", header: "نامزد (برچسب پارامتر)", render: (row) => <span className="num">{text(row.paramsLabel)}</span>, sortValue: (row) => row.paramsLabel },
    {
      key: "train",
      header: "بازده آموزش",
      numeric: true,
      sortValue: (row) => row.trainNet,
      render: (row) => <span className={`tone-${tone(row.trainNet)}`}>{pct(row.trainNet, { max: 2 })}</span>,
    },
    {
      key: "validation",
      header: "بازده اعتبارسنجی (برون‌نمونه‌ای)",
      numeric: true,
      sortValue: (row) => row.validationNet,
      render: (row) => <span className={`tone-${tone(row.validationNet)}`}>{pct(row.validationNet, { max: 2 })}</span>,
    },
    {
      key: "drop",
      header: "افت آموزش → اعتبارسنجی",
      numeric: true,
      sortValue: (row) => (row.trainNet !== null && row.validationNet !== null ? row.trainNet - row.validationNet : null),
      render: (row) => (row.trainNet === null || row.validationNet === null ? NO_DATA : pct(row.trainNet - row.validationNet, { max: 2 })),
    },
  ];

  const runColumns: Column<RunSummary>[] = [
    { key: "created", header: "زمان", sortValue: (row) => row.created_at, render: (row) => dateTime(row.created_at) },
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "tf", header: "تایم‌فریم", render: (row) => <span className="num">{text(row.timeframe)}</span>, sortValue: (row) => row.timeframe },
    { key: "label", header: "پارامتر", render: (row) => <span className="num">{text(row.params_label)}</span>, sortValue: (row) => row.params_label },
    { key: "status", header: "وضعیت", render: (row) => <span className="rtl-text">{text(row.status)}</span>, sortValue: (row) => row.status },
    {
      key: "median",
      header: "میانه‌ی بازده folds",
      numeric: true,
      sortValue: (row) => numAt(row.metrics, "net_return_pct_median"),
      render: (row) => <span className={`tone-${tone(numAt(row.metrics, "net_return_pct_median"))}`}>{pct(numAt(row.metrics, "net_return_pct_median"), { max: 2 })}</span>,
    },
    {
      key: "pf",
      header: "میانگین profit factor",
      numeric: true,
      sortValue: (row) => numAt(row.metrics, "profit_factor_mean"),
      render: (row) => num(numAt(row.metrics, "profit_factor_mean"), { max: 3 }),
    },
    {
      key: "trades",
      header: "معامله (جمع folds)",
      numeric: true,
      sortValue: (row) => numAt(row.metrics, "trades"),
      render: (row) => num(numAt(row.metrics, "trades"), { max: 0 }),
    },
    { key: "error", header: "دلیل رد ثبت‌شده", render: (row) => <span className="rtl-text mute">{text(row.error)}</span>, sortValue: (row) => row.error },
  ];

  /* ------------------------------------------------------------- render ---- */
  return (
    <>
      <div className="page-head">
        <div>
          <h1>آزمایشگاه استراتژی</h1>
          <p className="mute">
            ساخت نسخه‌ی جدید فقط با شواهد برون‌نمونه‌ای: شبکه‌ی محدود روی پارامترهای استراتژی ⇒ برازش روی train ⇒ امتیاز روی
            validation ⇒ walk-forward ⇒ مقایسه با champion ⇒ پذیرش یا ردِ ثبت‌شده. این صفحه هیچ سفارشی ایجاد نمی‌کند.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode} />
          <Badge kind="simulated" label="پژوهشی — بدون حق سفارش" />
        </div>
      </div>

      <div className={`state ${displayEdge.state === "not_profitable" ? "error" : displayEdge.state === "measured_positive" ? "success" : "warn"}`} role="status">
        <h3>{edgeCopy.title}</h3>
        <p>{edgeCopy.body}</p>
        <p className="stat-meta">
          <span>
            میانه‌ی بازده خالص برون‌نمونه‌ای: <span className={`num tone-${tone(displayEdge.net)}`}>{pct(displayEdge.net, { max: 3 })}</span>
          </span>
          <span>
            میانگین profit factor: <span className="num">{num(displayEdge.profitFactor, { max: 3 })}</span>
          </span>
          {result ? <span>منبع: پاسخ <code>/runs/walk-forward</code></span> : <span>منبع: آخرین نسخه‌ی accepted در /strategies</span>}
        </p>
      </div>

      <p className="notice">
        هیچ «هدف سود» یا عدد وعده‌داری در این صفحه نمایش داده نمی‌شود. اعداد فقط نتایج اندازه‌گیری‌شده‌ی بک‌تست/walk-forward با کارمزد
        و slippage‌اند؛ ضریب هدف قیمت (ATR) یک ضریب فاصله‌ی قیمتی است، نه درصد سود مورد انتظار.
      </p>

      <Filters
        fields={[
          {
            key: "symbol",
            label: "نماد",
            type: "select",
            value: activeSymbol,
            onChange: setSymbol,
            options: symbols.length > 0 ? symbols.map((item) => ({ value: item, label: item })) : [{ value: "", label: NO_DATA }],
            hint: "از config.symbols",
          },
          {
            key: "timeframe",
            label: "تایم‌فریم",
            type: "select",
            value: resolvedTimeframe,
            onChange: setTimeframe,
            options: timeframes.map((item) => ({ value: item, label: item })),
            hint: "پیش‌فرض = تایم‌فریم پیکربندی",
          },
          { key: "days", label: "روزهای تاریخچه", type: "number", value: days, onChange: setDays, hint: "۱ تا ۱۵۰۰ روز (اعتبار سرور)" },
          { key: "folds", label: "تعداد پنجره (folds)", type: "number", value: folds, onChange: setFolds, hint: "۲ تا ۱۲؛ حداقل ۲ پنجره" },
        ]}
        resultLabel={`ارزیابی‌های شبکه: ${num(evaluations, { max: 0 })} از سقف ${String(GRID_CAP)}`}
        actions={
          <>
            <button type="button" className="btn primary" onClick={() => void submit()} disabled={!canSubmit}>
              {running ? "در حال اجرای تحقیق…" : "اجرای walk-forward روی شبکه"}
            </button>
            {inputIssues.length > 0 || !gridReady ? (
              <ToneBadge tone="warn" title="تا رفع این موارد، اجرا مسدود است">
                ورودی کامل نیست
              </ToneBadge>
            ) : null}
          </>
        }
      />

      {inputIssues.length > 0 ? (
        <ul className="notice warn">
          {inputIssues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      ) : null}

      <section className="card" aria-label="ساخت شبکه‌ی پارامتر">
        <header>
          <h2>شبکه‌ی پارامتر (فقط فیلدهای استراتژی)</h2>
          <ToneBadge tone={evaluations > GRID_CAP ? "neg" : "neutral"}>{`${num(evaluations, { max: 0 })} ارزیابی از سقف ${String(GRID_CAP)}`}</ToneBadge>
        </header>
        <p className="mute">
          برای هر پارامتر، مقدارهای کاندید را با کاما جدا کنید. تعداد کل ارزیابی‌ها = حاصل‌ضرب طول فهرست مقدارها؛ سرور بالای
          {" "}
          <code>{String(GRID_CAP)}</code> ارزیابی را با <code>422</code> و پیام فارسی رد می‌کند و اینجا هم پیش از ارسال جلوی آن گرفته
          می‌شود.
        </p>

        <div className="grid">
          {rows.map((row, index) => {
            const meta = PARAM_META[row.param];
            const item = parsed[index];
            return (
              <div className="panel" key={row.id}>
                <div className="field">
                  <label htmlFor={`${row.id}-param`}>پارامتر</label>
                  <select
                    id={`${row.id}-param`}
                    value={row.param}
                    onChange={(event) =>
                      setRows((current) => current.map((each) => (each.id === row.id ? { ...each, param: event.target.value } : each)))
                    }
                  >
                    {paramOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <span className="mute">{meta?.effect}</span>
                  {meta?.bounds ? (
                    <span className="mute num">
                      کران مستند: [{String(meta.bounds[0])}, {String(meta.bounds[1])}]
                    </span>
                  ) : meta && meta.kind === "number" ? (
                    <span className="mute">کران مستند در PARAM_SPACE_HINTS ثبت نشده است؛ سرور فقط نوع و وجود مقدار را بررسی می‌کند.</span>
                  ) : null}
                  {meta && meta.kind !== "boolean" ? (
                    <input
                      id={`${row.id}-values`}
                      value={row.raw}
                      onChange={(event) => setRows((current) => current.map((each) => (each.id === row.id ? { ...each, raw: event.target.value } : each)))}
                      placeholder={meta.kind === "text" ? (meta.options ?? []).join(", ") : "مثلاً 30, 40, 50"}
                      aria-label={`مقدارهای ${meta.label}`}
                    />
                  ) : null}
                  {meta && meta.kind === "boolean" ? (
                    <div className="btn-row">
                      <label className="chip">
                        <input
                          type="checkbox"
                          checked={row.trueOn}
                          onChange={(event) => setRows((current) => current.map((each) => (each.id === row.id ? { ...each, trueOn: event.target.checked } : each)))}
                        />
                        <span className="num">true</span>
                      </label>
                      <label className="chip">
                        <input
                          type="checkbox"
                          checked={row.falseOn}
                          onChange={(event) => setRows((current) => current.map((each) => (each.id === row.id ? { ...each, falseOn: event.target.checked } : each)))}
                        />
                        <span className="num">false</span>
                      </label>
                    </div>
                  ) : null}
                  <span className="stat-meta">
                    <span>مقدارهای معتبر: {num(item?.values.length ?? 0, { max: 0 })}</span>
                    {item && item.invalid.length > 0 ? <span className="tone-neg">عدد نامعتبر: {item.invalid.join("، ")}</span> : null}
                    {item && item.outOfBounds.length > 0 ? <span className="tone-neg">خارج از کران: {item.outOfBounds.join("، ")}</span> : null}
                  </span>
                </div>
                {rows.length > 1 ? (
                  <div className="btn-row">
                    <button
                      type="button"
                      className="btn ghost small"
                      onClick={() => setRows((current) => current.filter((each) => each.id !== row.id))}
                      aria-label={`حذف پارامتر ${meta?.label ?? row.param}`}
                    >
                      حذف از شبکه
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>

        <div className="btn-row">
          <button
            type="button"
            className="btn small"
            onClick={() =>
              setRows((current) => [
                ...current,
                { id: nextRowId(), param: Object.keys(PARAM_META).find((key) => !current.some((row) => row.param === key)) ?? "rsi_period", raw: "", trueOn: true, falseOn: false },
              ])
            }
          >
            افزودن پارامتر به شبکه
          </button>
          <span className="mute">
            هر پارامتر فقط یک بار می‌تواند در شبکه باشد؛ اگر کلید ناشناخته یا تکراری فرستاده شود، سرور با <code>422</code> رد می‌کند.
          </span>
        </div>

        {evaluations > GRID_CAP ? (
          <div className="notice danger" role="alert">
            <strong>شبکه بیش از حد بزرگ است.</strong>
            <span>{GRID_CAP_MESSAGE} شبکه‌ی بزرگ‌تر یعنی جست‌وجوی بیشتر روی همان داده و شانس بالاتر overfitting؛ آن را کوچک کنید.</span>
          </div>
        ) : null}

        <div className="notice warn">
          <strong>سقف‌های ریسک هرگز هدف جست‌وجو نیستند.</strong>
          <span>
            فقط فیلدهای استراتژی در شبکه قابل انتخاب‌اند؛ این سقف‌ها هرگز در شبکه ظاهر نمی‌شوند:{" "}
            <code>{RISK_FORBIDDEN.join("، ")}</code> (مالکیت <code>config.yaml</code> و لایه‌ی ریسک). افزایش اندازه‌ی موقعیت برای جبران
            زیان (Martingale) هم در این سامانه وجود ندارد و ساخته نخواهد شد.
          </span>
        </div>

        {runError ? (
          <div className="notice danger" role="alert">
            <strong>اجرا ناموفق بود.</strong>
            <span>{runError}</span>
          </div>
        ) : null}
        {running ? <LoadingBlock label="در حال اجرای split → grid → validation → walk-forward…" /> : null}
      </section>

      {result ? (
        <>
          <section className="card" aria-label="حکم پذیرش">
            <header>
              <h2>حکم دروازه‌ی پذیرش</h2>
              <ToneBadge tone={result.accepted === true ? "pos" : result.accepted === false ? "neg" : "neutral"}>
                {result.accepted === true ? "پذیرفته شد — کاندید جای champion را می‌گیرد" : result.accepted === false ? "رد شد — champion باقی می‌ماند" : "حکم در پاسخ نیست"}
              </ToneBadge>
            </header>
            <dl className="kv">
              <dt>کاندید</dt>
              <dd className="rtl-text">{text(result.candidateLabel)}</dd>
              <dt>champion فعلی</dt>
              <dd className="rtl-text">{text(result.championLabel)}</dd>
              <dt>نماد / تایم‌فریم</dt>
              <dd className="rtl-text">{`${text(result.symbol)} · ${text(result.timeframe)}`}</dd>
              <dt>مجموعه‌داده</dt>
              <dd className="rtl-text">{text(result.datasetId)}</dd>
              <dt>تولید پاسخ</dt>
              <dd className="rtl-text">{dateTime(result.generatedAt)}</dd>
              <dt>حداقل معامله‌ی لازم</dt>
              <dd className="rtl-text">{num(result.minTrades, { max: 0 })}</dd>
              <dt>تصمیم ثبت‌شده</dt>
              <dd className="rtl-text">{text(result.decision)}</dd>
            </dl>
            {result.reasons.length > 0 ? (
              <>
                <h3>دلیل‌های رد (همان‌ها در strategy_versions.decision ذخیره می‌شوند)</h3>
                <ul>
                  {result.reasons.map((reason, index) => (
                    <li key={`${reason}-${String(index)}`} className="tone-neg">
                      {reason}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="stat-note">هیچ دلیل ردی در پاسخ نیست؛ یعنی همه‌ی شرط‌های دروازه راضی شده‌اند.</p>
            )}
            {result.riskChecks.length > 0 ? (
              <>
                <h3>شاهد ریسک (سقف‌ها دست‌نخورده ماندند؟)</h3>
                <ul>
                  {result.riskChecks.map((check) => (
                    <li key={check.key}>
                      <span className="num">{check.key}</span>{" "}
                      <ToneBadge tone={check.passed === true ? "pos" : check.passed === false ? "neg" : "warn"}>
                        {check.passed === true ? "برقرار" : check.passed === false ? "نقض" : NO_DATA}
                      </ToneBadge>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
            <ProvenanceNote provenance={result.provenance} fallbackKind="simulated" extra="POST /runs/walk-forward" />
          </section>

          <section className="card" aria-label="مقایسه‌ی سه بخش داده">
            <header>
              <h2>آموزش در برابر اعتبارسنجی در برابر آزمون</h2>
              <Badge kind="simulated" label="شبیه‌سازی با کارمزد و slippage" />
            </header>
            <Table
              rows={compareRows}
              getRowId={(row) => row.key}
              columns={compareColumns}
              caption="شاخص‌های همان اجرا به تفکیک سه برش زمانی"
              emptyLabel={NO_DATA}
              emptyHint="پاسخ سرور هیچ یک از بلوک‌های train/validation/test را کامل برنگرداند؛ عددی ساخته نمی‌شود."
            />
            <p className="stat-note">
              ستون آزمون یک hold-out است که فقط برای گزارش‌دهی اندازه گرفته می‌شود و دروازه‌ی پذیرش هرگز روی آن تصمیم نمی‌گیرد:{" "}
              {text(result.testNote)}
            </p>
            {result.segments.map((segment) => (
              <p className="stat-meta" key={segment.name}>
                <Badge kind="history" label={segment.label} />
                <span>کندل: {num(segment.bars, { max: 0 })}</span>
                <span>پنجره: {text(segment.window)}</span>
                <span>بدون شکاف: {segment.gapFree === null ? NO_DATA : segment.gapFree ? "بله" : "خیر"}</span>
                <span>پیوستگی با برش بعد: {segment.contiguous === null ? NO_DATA : segment.contiguous ? "بله" : "خیر"}</span>
              </p>
            ))}
            {result.segments.some((segment) => segment.bars !== null && segment.bars < 300) ? (
              <div className="notice warn">
                <strong>پنجره‌ی کوچک.</strong>
                <span>
                  یک یا چند برش کمتر از ۳۰۰ کندل دارند. با این حجم داده، تفاوت‌های چند دهم درصدی معنادار نیستند و نتیجه قابل تعمیم
                  نمی‌شود؛ نسخه‌ی جدید به همین دلیل نباید قهرمان شود.
                </span>
              </div>
            ) : null}
          </section>

          <section className="grid two" aria-label="پنجره‌های walk-forward">
            <div className="card">
              <header>
                <h2>بازده هر پنجره‌ی برون‌نمونه‌ای</h2>
                <ToneBadge tone="neutral">{`${num(result.positiveFolds, { max: 0 })} پنجره‌ی سودآور از ${num(result.totalFolds, { max: 0 })}`}</ToneBadge>
              </header>
              <MetricBars
                items={result.folds.map((fold) => ({ label: fold.label, value: fold.net, window: fold.window }))}
                windowLabel={result.provenance?.window ?? null}
                sourceLabel={result.provenance?.source ?? "engine"}
              />
              <ProvenanceNote provenance={result.provenance} fallbackKind="simulated" extra="walk_forward.folds" />
            </div>
            <div className="card">
              <header>
                <h2>تجمیع پنجره‌ها (کاندید در برابر champion)</h2>
                <Badge kind="simulated" />
              </header>
              <dl className="kv">
                <dt>تعداد پنجره‌ها</dt>
                <dd className="rtl-text">{num(numAt(result.aggregate, "folds"), { max: 0 })}</dd>
                <dt>میانه‌ی بازده</dt>
                <dd className={`rtl-text tone-${tone(numAt(result.aggregate, "net_return_pct_median", "net_return_pct"))}`}>
                  {pct(numAt(result.aggregate, "net_return_pct_median", "net_return_pct"), { max: 3 })}
                </dd>
                <dt>کمترین / بیشترین بازده پنجره</dt>
                <dd className="rtl-text">
                  {`${pct(numAt(result.aggregate, "net_return_pct_min"), { max: 2 })} ← ${pct(numAt(result.aggregate, "net_return_pct_max"), { max: 2 })}`}
                </dd>
                <dt>میانگین profit factor</dt>
                <dd className="rtl-text">{num(numAt(result.aggregate, "profit_factor_mean", "profit_factor"), { max: 3 })}</dd>
                <dt>جمع معاملات</dt>
                <dd className="rtl-text">{num(numAt(result.aggregate, "trades"), { max: 0 })}</dd>
                <dt>بیشترین افت (بدترین پنجره)</dt>
                <dd className="rtl-text">{pct(numAt(result.aggregate, "max_drawdown_pct"), { max: 2 })}</dd>
                <dt>خالص جمع پنجره‌ها</dt>
                <dd className="rtl-text">{signedMoney(numAt(result.aggregate, "net_pnl"))}</dd>
                <dt>champion (میانگین پنجره‌ها) — میانه‌ی بازده</dt>
                <dd className={`rtl-text tone-${tone(numAt(result.championAggregate, "net_return_pct_median", "net_return_pct"))}`}>
                  {pct(numAt(result.championAggregate, "net_return_pct_median", "net_return_pct"), { max: 3 })}
                </dd>
                <dt>champion — profit factor</dt>
                <dd className="rtl-text">{num(numAt(result.championAggregate, "profit_factor_mean", "profit_factor"), { max: 3 })}</dd>
                <dt>champion — معاملات</dt>
                <dd className="rtl-text">{num(numAt(result.championAggregate, "trades"), { max: 0 })}</dd>
              </dl>
              <p className="stat-note">{text(result.oosNote)}</p>
              {result.minTrades !== null && (numAt(result.aggregate, "trades") ?? 0) < result.minTrades ? (
                <div className="notice warn">
                  <strong>تعداد معامله‌ی برون‌نمونه‌ای کمتر از حداقل است.</strong>
                  <span>
                    بدون حداقل داده آماری، حتی یک بازده مثبت تصادفی را نمی‌توان لبه‌ی آماری دانست؛ دروازه به همین دلیل رد می‌کند.
                  </span>
                </div>
              ) : null}
            </div>
          </section>

          <section className="card" aria-label="جزئیات پنجره‌های والک‌فورورد">
            <header>
              <h2>جزئیات هر پنجره‌ی آزمون</h2>
              <Badge kind="simulated" label="برون‌نمونه‌ای" />
            </header>
            <Table
              rows={result.folds}
              getRowId={(row, index) => `${row.label}-${String(index)}`}
              columns={foldColumns}
              caption="شاخص‌های آزمون هر fold از والک‌فورورد"
              emptyLabel={NO_DATA}
              emptyHint="فهرست folds در پاسخ وجود ندارد یا خالی است؛ هیچ پنجره‌ای ساخته نمی‌شود."
            />
            <p className="stat-note">
              پنجره‌های آزمون هرگز با هم هم‌پوشانی ندارند و warmup فقط برای seed کردن اندیکاتورها از انتهای پنجره‌ی آموزش گرفته می‌شود؛
              بنابراین هیچ معامله‌ی یک fold پیش از شروع بازه‌ی آزمون همان fold اجرا نمی‌شود (بدون look-ahead).
            </p>
          </section>

          <section className="card" aria-label="نامزدهای شبکه">
            <header>
              <h2>نامزدهای بازگردانده‌شده‌ی شبکه</h2>
              <ToneBadge tone="warn" title="پاسخ فقط نامزدهای برتر را برمی‌گرداند، نه همه‌ی ترکیب‌ها">
                لیست ≠ کل ارزیابی‌ها
              </ToneBadge>
            </header>
            <Table
              rows={result.candidates}
              getRowId={(row, index) => `${String(index)}-${row.paramsLabel ?? "candidate"}`}
              columns={candidateColumns}
              caption="برچسب پارامتر با بازده آموزش و اعتبارسنجی"
              emptyLabel={NO_DATA}
              emptyHint="فهرست grid در پاسخ خالی بود یا کلیدش تغییر کرده است."
            />
            <p className="stat-note">
              تعداد ارزیابی‌های درخواستی از حاصل‌ضرب مقدارهای شبکه = {num(evaluations, { max: 0 })}. فاصله‌ی زیاد بین ستون آموزش و
              اعتبارسنجی نشانه‌ی overfitting است، نه بهبود.
            </p>
            {result.train && result.train.rejections.length > 0 ? (
              <>
                <h3>دلیل‌های رد سیگنال در برش آموزش</h3>
                <ul>
                  {result.train.rejections.map((entry) => (
                    <li key={entry.reason}>
                      <span className="num">{num(entry.count, { max: 0 })}</span> × {entry.reason}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </section>

          {result.disclaimer ? <p className="notice">{result.disclaimer}</p> : null}
        </>
      ) : (
        <section className="card" aria-label="پیش از اولین اجرا">
          <header>
            <h2>هنوز اجرای تازه‌ای در این نشست انجام نشده است</h2>
            <Badge kind="simulated" label="پژوهشی" />
          </header>
          <EmptyBlock
            label={NO_DATA}
            hint="شبکه را بسازید و «اجرای walk-forward» را بزنید. تا آن زمان فقط اجراهای پیشین از /runs و نسخه‌های /strategies دیده می‌شوند — و هیچ عددی جای آن‌ها نمی‌آید."
          />
          <p className="stat-note">
            نسخه‌ی champion فعلی از /strategies: {text(champion ? `${champion.name} ${champion.version ?? ""} @ ${champion.digest}` : null)} · پذیرش:{" "}
            {champion ? (champion.accepted ? "پذیرفته‌شده" : "پذیرفته‌نشده") : NO_DATA}
          </p>
        </section>
      )}

      <section className="card" aria-label="اجرای‌های walk-forward پیشین">
        <header>
          <h2>آخرین اجراهای walk_forward (از /runs)</h2>
          <Badge kind="simulated" />
        </header>
        <StateBlocks state={runs} emptyLabel={NO_DATA} emptyHint="هنوز اجرای walk_forward در runs ثبت نشده است.">
          {(data) => (
            <Table
              rows={data}
              getRowId={(row) => row.id}
              columns={runColumns}
              caption="اجراهای walk_forward ثبت‌شده با aggregate هر اجرا"
              emptyLabel={NO_DATA}
              defaultSort={{ key: "created", dir: "desc" }}
            />
          )}
        </StateBlocks>
        {runs.data && runs.data.length > 0 ? (
          <p className="stat-note">
            ستون «دلیل رد ثبت‌شده» همان پیامی است که موتور تحقیق در رکورد walk_forward ذخیره کرده است؛ رد شدن حالت عادی این چرخه است.
          </p>
        ) : null}
      </section>

      <section className="card" aria-label="قوانین دروازه">
        <header>
          <h2>شرط‌هایی که حکم از روی آن‌ها ساخته می‌شود</h2>
          <ToneBadge tone="neutral" title="در research.acceptance enforce می‌شود، نه در prompt مدل">
            قاعده‌ی سرور
          </ToneBadge>
        </header>
        <ul>
          <li>نتیجه باید روی داده‌ی خارج از نمونه اندازه گرفته شده باشد (validation یا aggregate والک‌فورورد؛ نه train و نه test).</li>
          <li>هزینه‌ی رفت‌وبرگشت (کارمزد + slippage) در همان عدد لحاظ شده باشد.</li>
          <li>بازده خالص و profit factor هر دو روی داده‌ی برون‌نمونه‌ای بهتر از champion.</li>
          <li>max drawdown کاندید بیش از ۱.۰ واحد درصد بدتر از champion نباشد.</li>
          <li>حداقل تعداد معامله‌ی برون‌نمونه‌ای (min_trades) انجام شده باشد.</li>
          <li>در حداقل نیمی از پنجره‌ها سودآور باشد؛ یک دوره‌ی خوش‌شانس نسخه نمی‌سازد.</li>
          <li>هیچ سقف ریسکی افزایش نیافته باشد.</li>
        </ul>
        <p className="stat-note">
          اگر حتی یک شرط نقض شود، champion می‌ماند و دلیل رد در <code>strategy_versions.decision</code> ثبت می‌شود — جزئیات را در صفحه‌ی
          «نسخه‌های استراتژی» ببینید.
        </p>
      </section>
    </>
  );
}
