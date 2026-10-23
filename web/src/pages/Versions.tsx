import { useMemo, useState } from "react";
import { getHealth, getStrategies, useAsync, type StrategyVersion } from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks from "../components/StateBlocks";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import MetricBars from "../charts/MetricBars";
import { NO_DATA, dateTime, num, pct, plainPct, signedMoney, text, tone } from "../format";

/* -------------------------------------------------------------------------- */
/* Local helpers.                                                             */
/* `api/client.ts` is shared with other agents and untouched, so the defensive  */
/* readers for the stored version payload live in this page file. Stored        */
/* train/validation/test blocks are `evaluate()` dicts (numbers under           */
/* `metrics`), while the TS contract types them as flat metrics — both shapes   */
/* are read here, and anything absent renders «داده‌ای موجود نیست».             */
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

function metricsOf(block: unknown): Rec {
  const source = asRec(block);
  const nested = asRec(source["metrics"]);
  return Object.keys(nested).length > 0 ? nested : source;
}

function hasNumbers(block: Rec): boolean {
  return Object.keys(block).length > 0 && numAt(block, "trades", "net_return_pct", "net_pnl") !== null;
}

interface FoldView {
  label: string;
  window: string | null;
  net: number | null;
  profitFactor: number | null;
  trades: number | null;
  drawdown: number | null;
}

function foldsOf(version: StrategyVersion): FoldView[] {
  const forward = asRec(version.walk_forward);
  const list = Array.isArray(forward["folds"]) ? forward["folds"] : [];
  return list.map((raw, index) => {
    const fold = asRec(raw);
    const metrics = metricsOf(fold["test_metrics"]);
    const number = numAt(fold, "fold");
    const testStart = strAt(fold, "test_start");
    const testEnd = strAt(fold, "test_end");
    return {
      label: `فولد ${String(number ?? index + 1)}`,
      window: testStart && testEnd ? `${testStart} ← ${testEnd}` : null,
      net: numAt(metrics, "net_return_pct"),
      profitFactor: numAt(metrics, "profit_factor"),
      trades: numAt(metrics, "trades"),
      drawdown: numAt(metrics, "max_drawdown_pct"),
    };
  });
}

/* ----------------------------------------------------------- version view */

interface VersionView {
  raw: StrategyVersion;
  train: Rec;
  validation: Rec;
  test: Rec;
  aggregate: Rec;
  folds: FoldView[];
  label: string;
  accepted: boolean;
  reasonLines: string[];
  gridEvaluations: number | null;
  outOfSampleNet: number | null;
  outOfSampleFactor: number | null;
  outOfSampleTrades: number | null;
  outOfSampleDrawdown: number | null;
  hasOutOfSample: boolean;
  trainToTestGap: number | null;
  positiveFolds: number | null;
  totalFolds: number | null;
}

function toVersionView(raw: StrategyVersion): VersionView {
  const train = metricsOf(raw.train);
  const validation = metricsOf(raw.validation);
  const test = metricsOf(raw.test);
  const forward = asRec(raw.walk_forward);
  const aggregate = asRec(forward["aggregate"]);
  const consistency = asRec(forward["consistency"]);
  const decision = raw.decision ?? null;
  const reasonLines = decision && decision.toLowerCase().startsWith("rejected")
    ? decision.replace(/^rejected:\s*champion kept\s*->\s*/i, "").split(/;\s*/)
    : [];
  const outOfSampleNet = numAt(aggregate, "net_return_pct_median", "net_return_pct") ?? numAt(validation, "net_return_pct");
  const outOfSampleFactor = numAt(aggregate, "profit_factor_mean", "profit_factor") ?? numAt(validation, "profit_factor");
  const trainNet = numAt(train, "net_return_pct");
  const testNet = numAt(test, "net_return_pct");
  return {
    raw,
    train,
    validation,
    test,
    aggregate,
    folds: foldsOf(raw),
    label: `${raw.name ?? "بدون نام"} ${raw.version ?? ""}`.trim(),
    accepted: Boolean(raw.accepted),
    reasonLines,
    gridEvaluations: numAt(asRec(raw.params), "grid_evaluations") ?? numAt(forward, "grid_evaluations", "evaluations"),
    outOfSampleNet,
    outOfSampleFactor,
    outOfSampleTrades: numAt(aggregate, "trades") ?? numAt(validation, "trades"),
    outOfSampleDrawdown: numAt(aggregate, "max_drawdown_pct") ?? numAt(validation, "max_drawdown_pct"),
    hasOutOfSample: hasNumbers(aggregate) || hasNumbers(validation),
    trainToTestGap: trainNet !== null && testNet !== null ? testNet - trainNet : null,
    positiveFolds: numAt(consistency, "positive_folds") ?? numAt(aggregate, "positive_folds"),
    totalFolds: numAt(consistency, "total_folds") ?? numAt(aggregate, "folds"),
  };
}

/* ------------------------------------------------------- parameter labels */

const PARAM_LABELS: Record<string, string> = {
  name: "نام",
  version: "نسخه",
  note: "یادداشت",
  digest: "هش پارامترها",
  label: "برچسب",
  ema_fast: "EMA سریع",
  ema_slow: "EMA کند",
  rsi_period: "دوره‌ی RSI",
  atr_period: "دوره‌ی ATR",
  atr_stop_mult: "ضریب حد ضرر (ATR)",
  atr_target_mult: "ضریب هدف قیمت (ATR)",
  min_bars: "حداقل کندل تاریخچه",
  vol_lookback: "دوره‌ی نوسان",
  slope_lookback: "دوره‌ی شیب",
  volume_avg_bars: "دوره‌ی میانگین حجم",
  regime_gate: "دروازه‌ی رژیم بازار",
  require_multiple_signals: "نیاز به چند سیگنال",
  no_trade_if_signals_conflict: "رد سیگنال در تضاد",
  rsi_bullish_low: "کف بازه‌ی RSI صعودی",
  rsi_bullish_high: "سقف بازه‌ی RSI صعودی",
  rsi_stretched: "آستانه‌ی RSI خیلی گرم",
  rsi_weak: "آستانه‌ی RSI ضعیف",
  volume_collapse_ratio: "نسبت ریزش حجم",
  volume_weak_ratio: "نسبت حجم ضعیف",
  slope_threshold: "آستانه‌ی شیب",
  vol_threshold: "آستانه‌ی نوسان",
  atr_pct_threshold: "آستانه‌ی درصد ATR",
};

/** Only the metric keys that carry information for the anti-overfitting view. */
const SPLIT_METRICS: { key: string; label: string; kind: "pct" | "num" | "count" | "money" }[] = [
  { key: "net_return_pct", label: "بازده خالص پس از کارمزد", kind: "pct" },
  { key: "profit_factor", label: "ضریب سود", kind: "num" },
  { key: "max_drawdown_pct", label: "بیشترین افت", kind: "pct" },
  { key: "trades", label: "تعداد معامله", kind: "count" },
  { key: "win_rate_pct", label: "نرخ برد", kind: "pct" },
  { key: "net_pnl", label: "خالص (USDT)", kind: "money" },
  { key: "fees_paid", label: "کارمزد ثبت‌شده (USDT)", kind: "money" },
];

function renderMetric(value: number | null, kind: "pct" | "num" | "count" | "money"): string {
  if (value === null) return NO_DATA;
  if (kind === "pct") return pct(value, { max: 2 });
  if (kind === "count") return num(value, { max: 0 });
  if (kind === "money") return signedMoney(value);
  return num(value, { max: 3 });
}

/* ---------------------------------------------------- acceptance rule gate */

interface Rule {
  label: string;
  detail: string;
  status: "pass" | "fail" | "unknown";
}

const DD_TOLERANCE_PP = 1.0;
const ENGINE_MIN_TRADES = 8;

/**
 * Per-version view of the documented `acceptance()` gate. A rule is only marked
 * pass/fail when the stored record actually carries the numbers; otherwise it
 * stays `unknown` and says so — the gate itself is enforced in the backend.
 */
function evaluateRules(version: VersionView, baseline: VersionView | null): Rule[] {
  const rules: Rule[] = [];
  rules.push({
    label: "نتیجه روی داده‌ی خارج از نمونه اندازه گرفته شده باشد",
    detail: version.hasOutOfSample
      ? "aggregate والک‌فورورد یا بلوک validation در رکورد این نسخه موجود است."
      : "نه aggregate والک‌فورورد و نه validation در رکورد این نسخه عددی ندارند؛ این نسخه هرگز نباید جانشین champion شود.",
    status: version.hasOutOfSample ? "pass" : "fail",
  });

  const fees = numAt(version.aggregate, "fees_paid") ?? numAt(version.validation, "fees_paid") ?? numAt(version.test, "fees_paid");
  rules.push({
    label: "هزینه‌ی رفت‌وبرگشت در نتایج لحاظ شده باشد",
    detail: fees === null ? "کارمزد ثبت‌شده در این رکورد نیست (شبیه‌ساز کارمزد و slippage را اعمال می‌کند، ولی عدد در نسخه ذخیره نشده است)." : `کارمزد ثبت‌شده: ${signedMoney(fees)}`,
    status: fees === null ? "unknown" : fees > 0 ? "pass" : "fail",
  });

  const baseNet = baseline ? baseline.outOfSampleNet : null;
  rules.push({
    label: "بازده خالص برون‌نمونه‌ای بهتر از champion",
    detail:
      version.outOfSampleNet === null
        ? "بازده برون‌نمونه‌ای این نسخه موجود نیست."
        : baseNet === null
          ? "مبنای champion در این فهرست در دسترس نیست؛ مقایسه انجام نشده است."
          : `این نسخه ${pct(version.outOfSampleNet, { max: 3 })} در برابر champion ${pct(baseNet, { max: 3 })} — توجه: پنجره‌ی اندازه‌گیری ممکن است یکی نباشد.`,
    status: version.outOfSampleNet === null || baseNet === null ? "unknown" : version.outOfSampleNet > baseNet ? "pass" : "fail",
  });

  const baseFactor = baseline ? baseline.outOfSampleFactor : null;
  rules.push({
    label: "profit factor برون‌نمونه‌ای بهتر از champion",
    detail:
      version.outOfSampleFactor === null
        ? "profit factor برون‌نمونه‌ای موجود نیست (اگر هیچ زیانی ثبت نشده باشد سرور null می‌فرستد و عددی ساخته نمی‌شود)."
        : baseFactor === null
          ? "مبنای champion در دسترس نیست."
          : `${num(version.outOfSampleFactor, { max: 3 })} در برابر ${num(baseFactor, { max: 3 })}`,
    status: version.outOfSampleFactor === null || baseFactor === null ? "unknown" : version.outOfSampleFactor > baseFactor ? "pass" : "fail",
  });

  const baseDrawdown = baseline ? baseline.outOfSampleDrawdown : null;
  rules.push({
    label: `بیشترین افت بیش از ${String(DD_TOLERANCE_PP)} واحد درصد بدتر از champion نباشد`,
    detail:
      version.outOfSampleDrawdown === null || baseDrawdown === null
        ? "عدد drawdown برون‌نمونه‌ای در یک یا دو رکورد موجود نیست."
        : `${pct(version.outOfSampleDrawdown, { max: 2 })} در برابر ${pct(baseDrawdown, { max: 2 })}`,
    status:
      version.outOfSampleDrawdown === null || baseDrawdown === null
        ? "unknown"
        : version.outOfSampleDrawdown <= baseDrawdown + DD_TOLERANCE_PP
          ? "pass"
          : "fail",
  });

  rules.push({
    label: `حداقل ${String(ENGINE_MIN_TRADES)} معامله‌ی برون‌نمونه‌ای (آستانه‌ی پیش‌فرض در کد)`,
    detail: version.outOfSampleTrades === null ? "تعداد معامله‌ی برون‌نمونه‌ای در رکورد نیست." : `ثبت‌شده: ${num(version.outOfSampleTrades, { max: 0 })}`,
    status: version.outOfSampleTrades === null ? "unknown" : version.outOfSampleTrades >= ENGINE_MIN_TRADES ? "pass" : "fail",
  });

  rules.push({
    label: "در حداقل نیمی از پنجره‌ها سودآور باشد",
    detail:
      version.positiveFolds === null || version.totalFolds === null || version.totalFolds === 0
        ? "شمارش پنجره‌های سودآور در رکورد نیست."
        : `${num(version.positiveFolds, { max: 0 })} از ${num(version.totalFolds, { max: 0 })}`,
    status:
      version.positiveFolds === null || version.totalFolds === null || version.totalFolds === 0
        ? "unknown"
        : version.positiveFolds * 2 >= version.totalFolds
          ? "pass"
          : "fail",
  });

  rules.push({
    label: "هیچ سقف ریسکی افزایش نیافته باشد",
    detail:
      "در زمان اجرا بررسی می‌شود (سقف‌ها در config.yaml مالک‌اند و شبکه نمی‌تواند آن‌ها را هدف بگیرد). این بایت در رکورد نسخه ذخیره نشده است؛ برای همانندسازی، digest پیکربندی در صفحه‌ی تنظیمات را ببینید.",
    status: "unknown",
  });
  return rules;
}

/* --------------------------------------------------------------- the page */

export default function Versions() {
  const health = useAsync((signal) => getHealth(signal), []);
  const strategies = useAsync((signal) => getStrategies(signal), []);

  const [selectedId, setSelectedId] = useState<string | null>(null);

  const versions = useMemo<VersionView[]>(() => (strategies.data ?? []).map(toVersionView), [strategies.data]);

  const champion = useMemo(() => {
    const accepted = versions.filter((row) => row.accepted);
    if (accepted.length === 0) return null;
    return accepted.reduce((best, row) => {
      const a = best.raw.created_at ?? "";
      const b = row.raw.created_at ?? "";
      return b > a ? row : best;
    });
  }, [versions]);

  const rejectedCount = versions.filter((row) => row.accepted === false).length;
  const unvalidated = versions.filter((row) => !row.hasOutOfSample).length;
  const notProfitable = versions.filter((row) => row.outOfSampleNet !== null && row.outOfSampleNet <= 0);

  const selected = versions.find((row) => row.raw.digest === selectedId) ?? null;

  const columns: Column<VersionView>[] = [
    { key: "created", header: "زمان ثبت", sortValue: (row) => row.raw.created_at, render: (row) => dateTime(row.raw.created_at) },
    {
      key: "label",
      header: "نسخه",
      sortValue: (row) => row.label,
      render: (row) => (
        <span>
          <span className="num">{row.label}</span>
          {champion && row.raw.digest === champion.raw.digest ? (
            <ToneBadge tone="pos" title="جدیدترین نسخه‌ی accepted در همین فهرست">
              {"قهرمان"}
            </ToneBadge>
          ) : null}
        </span>
      ),
    },
    { key: "digest", header: "digest", sortValue: (row) => row.raw.digest, render: (row) => <span className="num">{text(row.raw.digest)}</span> },
    { key: "source", header: "منبع", sortValue: (row) => row.raw.source, render: (row) => <span className="num">{text(row.raw.source)}</span> },
    { key: "parent", header: "والد", sortValue: (row) => row.raw.parent, render: (row) => <span className="num">{text(row.raw.parent)}</span> },
    {
      key: "accepted",
      header: "حکم دروازه",
      sortValue: (row) => (row.accepted ? 1 : 0),
      render: (row) => (
        <ToneBadge tone={row.accepted ? "pos" : "neg"} title="فیلد accepted از سرور — قضاوت UI نیست">
          {row.accepted ? "پذیرفته‌شده" : "رد شده"}
        </ToneBadge>
      ),
    },
    {
      key: "train",
      header: "بازده آموزش",
      numeric: true,
      sortValue: (row) => numAt(row.train, "net_return_pct"),
      render: (row) => <span className={`tone-${tone(numAt(row.train, "net_return_pct"))}`}>{pct(numAt(row.train, "net_return_pct"), { max: 2 })}</span>,
    },
    {
      key: "validation",
      header: "بازده اعتبارسنجی",
      numeric: true,
      sortValue: (row) => numAt(row.validation, "net_return_pct"),
      render: (row) => <span className={`tone-${tone(numAt(row.validation, "net_return_pct"))}`}>{pct(numAt(row.validation, "net_return_pct"), { max: 2 })}</span>,
    },
    {
      key: "test",
      header: "بازده آزمون",
      numeric: true,
      sortValue: (row) => numAt(row.test, "net_return_pct"),
      render: (row) => <span className={`tone-${tone(numAt(row.test, "net_return_pct"))}`}>{pct(numAt(row.test, "net_return_pct"), { max: 2 })}</span>,
    },
    {
      key: "gap",
      header: "فاصله‌ی آموزش ← آزمون",
      numeric: true,
      sortValue: (row) => row.trainToTestGap,
      render: (row) => (row.trainToTestGap === null ? NO_DATA : <span className={`tone-${Math.abs(row.trainToTestGap) >= 1 ? "neg" : "neutral"}`}>{pct(row.trainToTestGap, { max: 2 })}</span>),
    },
    {
      key: "pf",
      header: "ضریب سود (برون‌نمونه‌ای)",
      numeric: true,
      sortValue: (row) => row.outOfSampleFactor,
      render: (row) => num(row.outOfSampleFactor, { max: 3 }),
    },
    {
      key: "folds",
      header: "پنجره‌ی سودآور",
      numeric: true,
      sortValue: (row) => row.positiveFolds,
      render: (row) =>
        row.positiveFolds === null || row.totalFolds === null ? NO_DATA : `${num(row.positiveFolds, { max: 0 })} / ${num(row.totalFolds, { max: 0 })}`,
    },
    {
      key: "grid",
      header: "ارزیابی‌های شبکه",
      numeric: true,
      sortValue: (row) => row.gridEvaluations,
      render: (row) => num(row.gridEvaluations, { max: 0 }),
    },
    {
      key: "decision",
      header: "دلیل ثبت‌شده",
      sortValue: (row) => row.raw.decision,
      render: (row) => (
        <span className="rtl-text mute" title={row.raw.decision ?? undefined}>
          {row.raw.decision ? `${row.raw.decision.slice(0, 90)}${row.raw.decision.length > 90 ? "…" : ""}` : NO_DATA}
        </span>
      ),
    },
  ];

  const foldColumns: Column<FoldView>[] = [
    { key: "label", header: "پنجره", render: (row) => <span className="num">{row.label}</span>, sortValue: (row) => row.label },
    { key: "window", header: "بازه", render: (row) => text(row.window) },
    {
      key: "net",
      header: "بازده",
      numeric: true,
      sortValue: (row) => row.net,
      render: (row) => <span className={`tone-${tone(row.net)}`}>{pct(row.net, { max: 2 })}</span>,
    },
    { key: "pf", header: "ضریب سود", numeric: true, sortValue: (row) => row.profitFactor, render: (row) => num(row.profitFactor, { max: 3 }) },
    { key: "trades", header: "معامله", numeric: true, sortValue: (row) => row.trades, render: (row) => num(row.trades, { max: 0 }) },
    { key: "dd", header: "بیشترین افت", numeric: true, sortValue: (row) => row.drawdown, render: (row) => pct(row.drawdown, { max: 2 }) },
  ];

  const rulesForSelected = selected ? evaluateRules(selected, champion && champion.raw.digest !== selected.raw.digest ? champion : null) : [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>نسخه‌های استراتژی</h1>
          <p className="mute">
            هر نسخه‌ی ساخته‌شده — پذیرفته‌شده یا ردشده — با digest، نتایج آموزش/اعتبارسنجی/آزمون و دلیل ثبت‌شده در{" "}
            <code>strategy_versions</code> می‌ماند. ستون پذیرش فقط از فیلد <code>accepted</code> سرور خوانده می‌شود.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode} />
          <Badge kind="simulated" label="شاهد تاریخی — نه نتیجه‌ی زنده" />
        </div>
      </div>

      <div className="notice">
        <strong>سابقه حذف‌شدنی نیست.</strong>
        <span>
          هیچ endpoint حذف یا بازنویسی نسخه در API وجود ندارد و این صفحه هم هیچ دکمه‌ی حذف ندارد. دلیل رد هم مثل پذیرش ثبت می‌شود؛
          شکست‌ها بخشی از داده‌ی همین جدول‌اند، نه خطای نمایش.
        </span>
      </div>

      {notProfitable.length > 0 || unvalidated > 0 || champion === null ? (
        <div className="state error" role="status">
          <h3>هنوز استراتژی سودآور تأیید نشده است</h3>
          <p>
            {champion === null
              ? "در فهرست نسخه‌ها هیچ رکورد accepted وجود ندارد؛ champion همان ساختار پایه است و هنوز با داده‌ی برون‌نمونه‌ای جایگزین نشده است."
              : `champion ثبت‌شده: ${champion.label} @ ${text(champion.raw.digest)} — بازده برون‌نمونه‌ای اندازه‌گیری‌شده: ${pct(champion.outOfSampleNet, { max: 3 })} با profit factor ${num(champion.outOfSampleFactor, { max: 3 })}.`}{" "}
            بک‌تست‌های پیشین این ساختار حدود ۰٪ تا −۲٪ خالص با profit factor کوچک‌تر از ۱ بوده‌اند؛ profit factor زیر ۱ یعنی زیان.
          </p>
        </div>
      ) : null}

      <div className="grid stats" aria-label="خلاصه‌ی نسخه‌ها">
        <Stat
          label="تعداد نسخه‌های ثبت‌شده"
          value={num(versions.length, { max: 0 })}
          window={strategies.data && strategies.data.length > 0 ? dateTime(strategies.data[strategies.data.length - 1]?.created_at) : null}
          source="strategy_versions"
          endpoint="/strategies"
          note="شمارش ردیف‌های بازگردانده‌شده؛ سرور همه‌ی نسخه‌ها را برمی‌گرداند."
        />
        <Stat
          label="پذیرفته‌شده"
          value={num(versions.length - rejectedCount, { max: 0 })}
          source="accepted = true"
          endpoint="/strategies"
          note="پذیرش فقط وقتی رخ می‌دهد که همه‌ی شرط‌های ضد-overfitting روی داده‌ی برون‌نمونه‌ای برقرار شود."
        />
        <Stat
          label="رد شده (با دلیل ثبت‌شده)"
          value={num(rejectedCount, { max: 0 })}
          tone={rejectedCount > 0 ? "neutral" : "neutral"}
          source="accepted = false"
          endpoint="/strategies"
          note="رد شدن حالت عادی این چرخه است؛ champion در این حالت باقی می‌ماند."
        />
        <Stat
          label="champion فعلی"
          value={champion ? champion.label : NO_DATA}
          source={champion ? `digest ${champion.raw.digest}` : null}
          endpoint="/strategies"
          note={champion ? `والد: ${text(champion.raw.parent)} · منبع: ${text(champion.raw.source)}` : "هیچ نسخه‌ی accepted ثبت نشده است."}
        />
        <Stat
          label="نسخه بدون اعتبارسنجی برون‌نمونه‌ای"
          value={num(unvalidated, { max: 0 })}
          tone={unvalidated > 0 ? "neg" : "neutral"}
          source="validation / walk_forward خالی"
          endpoint="/strategies"
          note="برای این نسخه‌ها ستون‌های اعتبارسنجی و آزمون عمداً خالی‌اند و از آموزش کپی نشده‌اند."
        />
        <Stat
          label="نسخه‌های با بازده برون‌نمونه‌ای ≤ ۰"
          value={num(notProfitable.length, { max: 0 })}
          tone={notProfitable.length > 0 ? "neg" : "neutral"}
          source="net_return_pct (aggregate/validation)"
          endpoint="/strategies"
          note="عدد منفی یعنی همین داده، نه پیش‌بینی آینده."
        />
      </div>

      <section className="card" aria-label="فهرست نسخه‌ها">
        <header>
          <h2>همه‌ی نسخه‌ها (برای دیدن جزئیات، یک ردیف را انتخاب کنید)</h2>
          <ToneBadge tone="warn" title="این جدول فقط خواندنی است">
            بدون حذف · بدون بازنویسی
          </ToneBadge>
        </header>
        <StateBlocks state={strategies} emptyLabel={NO_DATA} emptyHint="هنوز هیچ نسخه‌ای در strategy_versions ثبت نشده است؛ اولین اجرای walk-forward در آزمایشگاه، دو رکورد (کاندید و champion) می‌سازد.">
          {() => (
            <Table
              rows={versions}
              getRowId={(row, index) => `${row.raw.digest}-${String(index)}`}
              columns={columns}
              caption="نسخه‌های استراتژی با نتایج سه برش زمانی و دلیل پذیرش/رد"
              onRowClick={(row) => setSelectedId(row.raw.digest)}
              defaultSort={{ key: "created", dir: "desc" }}
              emptyLabel={NO_DATA}
              emptyHint="فهرست نسخه‌ها خالی است."
            />
          )}
        </StateBlocks>
        <ProvenanceNote provenance={strategies.data?.[0]?.provenance ?? null} fallbackKind="simulated" extra="/strategies" />
      </section>

      {champion ? (
        <section className="card" aria-label="شاهد قهرمان">
          <header>
            <h2>{`قهرمان فعلی: ${champion.label}`}</h2>
            <ToneBadge tone="pos">آخرین نسخه‌ی accepted</ToneBadge>
          </header>
          <div className="grid two">
            <div className="panel">
              <h3>سه برش زمانی، کنار هم</h3>
              <SplitTable view={champion} />
              <p className="stat-note">
                فاصله‌ی آموزش ← آزمون: {champion.trainToTestGap === null ? NO_DATA : pct(champion.trainToTestGap, { max: 2 })} — هرچه
                بزرگ‌تر، نشانه‌ی قوی‌تر overfitting.
              </p>
            </div>
            <div className="panel">
              <h3>پنجره‌های برون‌نمونه‌ای</h3>
              <MetricBars
                items={champion.folds.map((fold) => ({ label: fold.label, value: fold.net, window: fold.window }))}
                windowLabel={champion.raw.provenance?.window ?? null}
                sourceLabel={champion.raw.provenance?.source ?? "engine"}
              />
              <Table
                rows={champion.folds}
                getRowId={(row, index) => `${champion.raw.digest}-${row.label}-${String(index)}`}
                columns={foldColumns}
                caption="شاخص‌های آزمون هر fold"
                emptyLabel={NO_DATA}
                emptyHint="فolds در رکورد این نسخه ذخیره نشده‌اند."
              />
            </div>
          </div>
          {champion.folds.some((fold) => fold.trades !== null && fold.trades < 8) ? (
            <div className="notice warn">
              <strong>حجم داده‌ی کم در یک یا چند پنجره.</strong>
              <span>
                وقتی تعداد معامله‌ی یک fold از آستانه‌ی موتور (۸) کمتر باشد، نتیجه‌ی آن پنجره قابل اتکا نیست و نباید ملاک پذیرش قرار
                بگیرد.
              </span>
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="card" aria-label="قوانین پذیرش">
        <header>
          <h2>قوانینی که حکم پذیرش از آن‌ها می‌آید</h2>
          <ToneBadge tone="neutral" title="در research.acceptance enforce می‌شود، نه در prompt مدل">
            قاعده‌ی سرور
          </ToneBadge>
        </header>
        <ol>
          <li>نتیجه روی داده‌ی خارج از نمونه اندازه گرفته شده باشد (validation یا aggregate والک‌فورورد — نه آموزش، نه آزمون).</li>
          <li>هزینه‌ی رفت‌وبرگشت (کارمزد و slippage) در همان نتایج لحاظ شده باشد.</li>
          <li>بازده خالص و profit factor هر دو روی داده‌ی برون‌نمونه‌ای بهتر از champion.</li>
          <li>
            max drawdown کاندید بیش از {String(DD_TOLERANCE_PP)} واحد درصد بدتر از champion نباشد (تلورانس در کد:{" "}
            <code>DD_TOLERANCE_PP = 1.0</code>).
          </li>
          <li>حداقل تعداد معامله‌ی برون‌نمونه‌ای انجام شده باشد تا نتیجه از نظر آماری معنا داشته باشد.</li>
          <li>در حداقل نیمی از پنجره‌ها سودآور باشد؛ یک دوره‌ی خوش‌شانس نمی‌تواند نسخه بسازد.</li>
          <li>هیچ سقف ریسکی افزایش نیافته باشد.</li>
        </ol>
        <p className="stat-note">
          اگر حتی یک شرط نقض شود، champion می‌ماند و دلیل رد در <code>decision</code> ذخیره می‌شود. ردیف‌های زیر برای نسخه‌ی
          انتخاب‌شده محاسبه می‌شوند؛ هر جا عدد در رکورد نباشد، وضعیت «نامشخص» می‌ماند و حدسی زده نمی‌شود.
        </p>
      </section>

      <Modal
        open={selected !== null}
        title={selected ? `نسخه‌ی ${selected.label} @ ${text(selected.raw.digest)}` : "نسخه"}
        description={selected ? `منبع: ${text(selected.raw.source)} · والد: ${text(selected.raw.parent)} · ثبت: ${dateTime(selected.raw.created_at)}` : undefined}
        onClose={() => setSelectedId(null)}
        width={920}
        footer={
          <button type="button" className="btn small" onClick={() => setSelectedId(null)}>
            بستن
          </button>
        }
      >
        {selected ? (
          <>
            <div className="btn-row">
              <ToneBadge tone={selected.accepted ? "pos" : "neg"}>
                {selected.accepted ? "پذیرفته‌شده — جایگزین champion شد" : "رد شده — champion باقی ماند"}
              </ToneBadge>
              <Badge kind={selected.raw.provenance?.kind ?? "simulated"} label="شبیه‌سازی" />
            </div>
            <h3>تصمیم و دلیل ثبت‌شده (متن سرور)</h3>
            <p className="rtl-text">{text(selected.raw.decision)}</p>
            {selected.reasonLines.length > 0 ? (
              <ul>
                {selected.reasonLines.map((line, index) => (
                  <li key={`${line}-${String(index)}`} className="tone-neg">
                    {line}
                  </li>
                ))}
              </ul>
            ) : null}

            <h3>آموزش / اعتبارسنجی / آزمون</h3>
            <SplitTable view={selected} />
            <p className="stat-note">
              اعتبارسنجی برون‌نمونه‌ای این نسخه: {selected.hasOutOfSample ? "در رکورد موجود است" : "در رکورد موجود نیست — ستون‌های آن عمداً خالی‌اند و از آموزش کپی نشده‌اند"}
              {selected.hasOutOfSample
                ? ` · بازده برون‌نمونه‌ای: ${pct(selected.outOfSampleNet, { max: 3 })} · profit factor: ${num(selected.outOfSampleFactor, { max: 3 })} · معامله: ${num(selected.outOfSampleTrades, { max: 0 })}`
                : ""}
            </p>

            <h3>بررسی موردیِ شرط‌های پذیرش برای این نسخه</h3>
            <ul>
              {rulesForSelected.map((rule) => (
                <li key={rule.label}>
                  <ToneBadge tone={rule.status === "pass" ? "pos" : rule.status === "fail" ? "neg" : "warn"}>
                    {rule.status === "pass" ? "برقرار" : rule.status === "fail" ? "نقض" : "نامشخص از رکورد"}
                  </ToneBadge>{" "}
                  <strong>{rule.label}</strong>
                  <span className="mute"> — {rule.detail}</span>
                </li>
              ))}
            </ul>

            {selected.folds.length > 0 ? (
              <>
                <h3>پنجره‌های والک‌فورورد این نسخه</h3>
                <Table rows={selected.folds} getRowId={(row, index) => `${row.label}-${String(index)}`} columns={foldColumns} caption="شاخص‌های آزمون هر fold" />
              </>
            ) : null}

            <h3>پارامترهای همین نسخه</h3>
            <dl className="kv">
              {Object.entries(selected.raw.params).map(([key, value]) => (
                <div key={key} style={{ display: "contents" }}>
                  <dt>{PARAM_LABELS[key] ? `${PARAM_LABELS[key]} (${key})` : key}</dt>
                  <dd className={typeof value === "string" ? "rtl-text" : undefined}>{typeof value === "boolean" ? (value ? "true" : "false") : String(value)}</dd>
                </div>
              ))}
            </dl>
            <ProvenanceNote provenance={selected.raw.provenance ?? null} fallbackKind="simulated" extra="/strategies" />
          </>
        ) : null}
      </Modal>

      <p className="notice warn">
        این‌ها شاهد تاریخی‌اند، نه پیش‌بینی. بازده گذشته تضمین‌کننده‌ی آینده نیست و ممکن است تمام سرمایه‌ی اختصاص‌یافته از دست برود.
        نرخ برد یا profit factor بدون آزمون برون‌نمونه‌ای کافی، لبه‌ی آماری محسوب نمی‌شود.
      </p>
    </>
  );
}

/* ------------------------------------------------------------ split table */

/** Train / validation / test side by side, with an explicit copy-prevention note. */
function SplitTable({ view }: { view: VersionView }) {
  const rows = SPLIT_METRICS.map((definition) => ({
    key: definition.key,
    label: definition.label,
    kind: definition.kind,
    train: numAt(view.train, definition.key),
    validation: numAt(view.validation, definition.key),
    test: numAt(view.test, definition.key),
  }));
  const columns: Column<(typeof rows)[number]>[] = [
    { key: "label", header: "شاخص", render: (row) => row.label, sortValue: (row) => row.label },
    { key: "train", header: "آموزش (in-sample)", numeric: true, sortValue: (row) => row.train, render: (row) => renderMetric(row.train, row.kind) },
    {
      key: "validation",
      header: "اعتبارسنجی (برون‌نمونه‌ای)",
      numeric: true,
      sortValue: (row) => row.validation,
      render: (row) => (hasNumbers(view.validation) ? renderMetric(row.validation, row.kind) : <span className="mute">{NO_DATA}</span>),
    },
    {
      key: "test",
      header: "آزمون (hold-out)",
      numeric: true,
      sortValue: (row) => row.test,
      render: (row) => (hasNumbers(view.test) ? renderMetric(row.test, row.kind) : <span className="mute">{NO_DATA}</span>),
    },
    {
      key: "gap",
      header: "آموزش ← آزمون",
      numeric: true,
      sortValue: (row) => (row.train !== null && row.test !== null ? row.test - row.train : null),
      render: (row) =>
        row.train === null || row.test === null || row.kind === "money" || row.kind === "count"
          ? NO_DATA
          : <span className={`tone-${Math.abs(row.test - row.train) >= 1 ? "neg" : "neutral"}`}>{pct(row.test - row.train, { max: 2 })}</span>,
    },
  ];
  return (
    <>
      <Table rows={rows} getRowId={(row) => row.key} columns={columns} caption="مقایسه‌ی سه برش زمانی برای یک نسخه" />
      <p className="stat-meta">
        <Badge kind="simulated" />
        <span>
          مقیاس‌ها: {plainPct(numAt(view.train, "net_return_pct"), { max: 2 })} آموزش، {plainPct(numAt(view.validation, "net_return_pct"), { max: 2 })} اعتبارسنجی،{" "}
          {plainPct(numAt(view.test, "net_return_pct"), { max: 2 })} آزمون
        </span>
      </p>
    </>
  );
}
