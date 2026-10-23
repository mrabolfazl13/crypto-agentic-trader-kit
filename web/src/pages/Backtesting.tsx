import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  describeError,
  getDatasets,
  getConfig,
  getRun,
  getRuns,
  runBacktest,
  useAsync,
  type AsyncState,
  type Metrics,
  type Provenance,
  type RejectionRow,
  type RunDetail,
  type RunSummary,
  type RunTradeRow,
} from "../api/client";
import Badge, { ToneBadge } from "../components/Badge";
import Filters from "../components/Filters";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import Stat from "../components/Stat";
import StateBlocks, { EmptyBlock, ErrorBlock, LoadingBlock, SuccessBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import DrawdownChart from "../charts/DrawdownChart";
import LineChart from "../charts/LineChart";
import type { XY } from "../charts/scale";
import { NO_DATA, dateTime, money, num, pct, plainPct, signedMoney, text, tone } from "../format";

/**
 * `/runs/{id}` answers `{run, provenance}` while `getRun()` in client.ts is typed as the
 * flat run; `/runs/backtest` answers only `{run_id, metrics, params, provenance}` (no
 * trades/curve) so the detail is re-read from `/runs/{id}`. Both shapes are handled here
 * because a page may not edit the shared client.
 */
interface RunEnvelope {
  run?: RunDetail | null;
  provenance?: Provenance | null;
}

interface BacktestAck {
  run_id?: string | null;
  symbol?: string | null;
  timeframe?: string | null;
  metrics?: Metrics | null;
  params?: Record<string, unknown> | null;
  provenance?: Provenance | null;
}

/** Reads either `{run, provenance}` (the real route) or a flat run (the shared type). */
function unwrapRun(payload: unknown): { run: RunDetail | null; provenance: Provenance | null } | null {
  if (!payload || typeof payload !== "object") return null;
  const envelope = payload as RunEnvelope;
  if (envelope.run && typeof envelope.run === "object") {
    return { run: envelope.run, provenance: envelope.provenance ?? envelope.run.provenance ?? null };
  }
  const flat = payload as RunDetail;
  const isDetail = Array.isArray(flat.trades) || Array.isArray(flat.equity_curve) || Array.isArray(flat.rejections);
  return { run: isDetail ? flat : null, provenance: envelope.provenance ?? flat.provenance ?? null };
}

const TIMEFRAMES = ["15m", "1h", "4h", "1d"];

/** Strategy fields the backtest Modal may override; unknown keys are ignored by the API. */
const PARAM_FIELDS: { key: string; label: string; hint: string }[] = [
  { key: "ema_fast", label: "EMA سریع", hint: "عدد صحیح" },
  { key: "ema_slow", label: "EMA کند", hint: "عدد صحیح" },
  { key: "rsi_period", label: "دوره RSI", hint: "عدد صحیح" },
  { key: "atr_period", label: "دوره ATR", hint: "عدد صحیح" },
  { key: "atr_stop_mult", label: "ضریب حد ضرر (ATR)", hint: "عدد اعشاری" },
  { key: "atr_target_mult", label: "ضریب هدف (ATR)", hint: "عدد اعشاری" },
];

const METRIC_DEFS: { key: keyof Metrics; label: string; kind: "count" | "money" | "pct" | "plain"; tooltip?: string }[] = [
  { key: "trades", label: "تعداد معامله‌ی شبیه‌سازی‌شده", kind: "count" },
  { key: "win_rate_pct", label: "نرخ برد", kind: "pct", tooltip: "مبنای برد: خالص پس از کارمزد > ۰ در همان اجرا." },
  { key: "profit_factor", label: "ضریب سود", kind: "plain", tooltip: "مجموع سود تقسیم بر مجموع زیان؛ اگر زیانی نباشد API مقدار null می‌فرستد." },
  { key: "avg_win", label: "میانگین سود هر برد", kind: "money" },
  { key: "avg_loss", label: "میانگین زیان هر باخت", kind: "money" },
  { key: "net_pnl", label: "خالص پس از کارمزد", kind: "money" },
  { key: "net_return_pct", label: "بازده خالص نسبت به سرمایه‌ی تخصیصی", kind: "pct" },
  { key: "fees_paid", label: "کارمزد پرداختی در شبیه‌سازی", kind: "money" },
  { key: "max_drawdown_pct", label: "بیشترین افت سرمایه", kind: "pct" },
  { key: "sharpe_annualised_crude", label: "شارپ (تحویلی و خام)", kind: "plain" },
  { key: "sortino_annualised_crude", label: "سورتینو (تحویلی و خام)", kind: "plain" },
  { key: "bars", label: "کندل‌های منحنی سرمایه", kind: "count" },
];

function kindFa(kind: string | null | undefined): string {
  if (!kind) return NO_DATA;
  if (kind === "backtest") return "بک‌تست";
  if (kind === "walk_forward") return "والک‌فورورد";
  if (kind === "grid") return "شبکه‌ی پارامتر";
  return kind;
}

function metricValue(metrics: Partial<Metrics> | null | undefined, key: keyof Metrics): number | null {
  const raw = metrics?.[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

function formatMetric(value: number | null, kind: "count" | "money" | "pct" | "plain"): string {
  if (value === null) return NO_DATA;
  if (kind === "money") return signedMoney(value);
  if (kind === "pct") return pct(value, { max: 2 });
  if (kind === "count") return num(value, { max: 0 });
  return num(value, { max: 3 });
}

/**
 * Drawdown derived arithmetically from the run's own stored equity points (same formula the
 * engine uses for `max_drawdown_pct`). It is labelled as a local derivation on the page.
 */
function drawdownFromEquity(points: readonly (readonly [number, number])[]): XY[] {
  let peak = Number.NEGATIVE_INFINITY;
  return points.map(([ts, equity]) => {
    if (!Number.isFinite(equity)) return [ts, null] as XY;
    peak = Math.max(peak, equity);
    if (!(peak > 0)) return [ts, null] as XY;
    return [ts, -Math.max(0, ((peak - equity) / peak) * 100)] as XY;
  });
}

function FormRow({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? <span className="mute">{hint}</span> : null}
    </div>
  );
}

interface RunAck {
  runId: string | null;
  metrics: Metrics | null;
  window: string | null;
  spread: number | null;
}

export default function Backtesting() {
  const [kindFilter, setKindFilter] = useState("");
  const [symbolFilter, setSymbolFilter] = useState("");
  const runsState = useAsync((signal) => getRuns({ limit: 50 }, signal), []);

  const config = useAsync((signal) => getConfig(signal), []);
  const datasets = useAsync((signal) => getDatasets(signal), []);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [ack, setAck] = useState<RunAck | null>(null);
  const [spreadByRun, setSpreadByRun] = useState<Record<string, number>>({});
  const [form, setForm] = useState({
    symbol: "",
    timeframe: "",
    source: "dataset",
    datasetId: "",
    days: "365",
    warmup: "80",
    spread: "0.05",
  });
  const [paramForm, setParamForm] = useState<Record<string, string>>({});

  const allRuns = runsState.data ?? [];
  const runs = useMemo(
    () =>
      allRuns.filter(
        (row) => (kindFilter === "" || row.kind === kindFilter) && (symbolFilter === "" || row.symbol === symbolFilter),
      ),
    [allRuns, kindFilter, symbolFilter],
  );
  const selected = runs.find((row) => row.id === selectedId) ?? runs[0] ?? null;
  const selectedKey = selected?.id ?? "";

  const detailState = useAsync(async (signal) => {
    if (selectedKey === "") return null;
    return getRun(selectedKey, signal);
  }, [selectedKey]);
  const loaded = unwrapRun(detailState.data);
  // `useAsync` keeps the previous payload while the next request is in flight, so the
  // detail is only trusted when it belongs to the run that is currently selected.
  const run: RunDetail | null = loaded?.run && loaded.run.id === selectedKey ? loaded.run : null;
  const provenance: Provenance | null = run ? loaded?.provenance ?? run.provenance ?? null : null;
  // `/runs` rows already carry the stored metrics, so a freshly built run shows its numbers
  // even before the detail request lands.
  const metrics: Partial<Metrics> = run?.metrics ?? selected?.metrics ?? {};

  const symbolOptions = useMemo(() => {
    const set = new Set<string>();
    for (const symbol of config.data?.symbols ?? []) set.add(symbol);
    for (const row of allRuns) set.add(row.symbol);
    return [...set].sort((a, b) => a.localeCompare(b, "en"));
  }, [config.data, allRuns]);

  const datasetOptions = useMemo(() => {
    const list = datasets.data ?? [];
    const symbol = form.symbol;
    return (symbol === "" ? list : list.filter((row) => row.symbol === symbol)).slice(0, 40);
  }, [datasets.data, form.symbol]);

  const windowText =
    selected && (selected.window_start || selected.window_end)
      ? `${text(selected.window_start)} → ${text(selected.window_end)}`
      : null;
  const paramEntries = Object.entries(run?.params ?? selected?.params ?? {});
  const paramsSummary = paramEntries.length === 0 ? NO_DATA : paramEntries.map(([key, value]) => `${key}=${String(value)}`).join(" · ");
  const assumedSpread = selectedKey === "" ? null : spreadByRun[selectedKey] ?? null;
  const equityPoints: XY[] = (run?.equity_curve ?? []).map((point) => [point[0], point[1]] as XY);
  const drawdownPoints = drawdownFromEquity(run?.equity_curve ?? []);
  const trades = run?.trades ?? [];
  const rejections = run?.rejections ?? [];
  const rejectionTotal = rejections.reduce((sum, row) => sum + (typeof row.count === "number" ? row.count : 0), 0);
  const sharpeNote = typeof metrics.sharpe_note === "string" && metrics.sharpe_note !== "" ? metrics.sharpe_note : null;
  const extraMetrics = Object.entries(metrics).filter(([key]) => !(METRIC_DEFS as { key: string }[]).some((def) => def.key === key));

  const runColumns: Column<RunSummary>[] = [
    { key: "id", header: "شناسه‌ی اجرا", render: (row) => <span className="num">{row.id}</span>, sortValue: (row) => row.id },
    { key: "kind", header: "نوع", render: (row) => <span title={text(row.kind)}>{kindFa(row.kind)}</span>, sortValue: (row) => row.kind },
    {
      key: "status",
      header: "وضعیت",
      render: (row) =>
        row.status === "completed" ? (
          <ToneBadge tone="pos">تکمیل</ToneBadge>
        ) : row.status === "failed" ? (
          <ToneBadge tone="neg" title={text(row.error)}>شکست</ToneBadge>
        ) : (
          <ToneBadge tone="neutral">{text(row.status)}</ToneBadge>
        ),
      sortValue: (row) => row.status,
    },
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "timeframe", header: "تایم‌فریم", render: (row) => <span className="num">{text(row.timeframe)}</span>, sortValue: (row) => row.timeframe },
    { key: "params", header: "پارامترها", render: (row) => <span className="num">{text(row.params_label)}</span>, sortValue: (row) => row.params_label },
    { key: "trades", header: "معامله", numeric: true, sortValue: (row) => metricValue(row.metrics, "trades"), render: (row) => num(metricValue(row.metrics, "trades"), { max: 0 }) },
    {
      key: "ret",
      header: "بازده خالص",
      numeric: true,
      sortValue: (row) => metricValue(row.metrics, "net_return_pct"),
      render: (row) => {
        const value = metricValue(row.metrics, "net_return_pct");
        return <span className={`tone-${tone(value)}`}>{pct(value, { max: 2 })}</span>;
      },
    },
    { key: "win", header: "نرخ برد", numeric: true, sortValue: (row) => metricValue(row.metrics, "win_rate_pct"), render: (row) => plainPct(metricValue(row.metrics, "win_rate_pct"), { max: 1 }) },
    { key: "pf", header: "ضریب سود", numeric: true, sortValue: (row) => metricValue(row.metrics, "profit_factor"), render: (row) => num(metricValue(row.metrics, "profit_factor"), { max: 2 }) },
    { key: "dd", header: "بیشترین افت", numeric: true, sortValue: (row) => metricValue(row.metrics, "max_drawdown_pct"), render: (row) => pct(metricValue(row.metrics, "max_drawdown_pct"), { max: 2 }) },
    { key: "window", header: "پنجره", render: (row) => `${text(row.window_start)} → ${text(row.window_end)}` },
    { key: "created", header: "زمان اجرا", sortValue: (row) => row.created_at, render: (row) => dateTime(row.created_at) },
  ];

  const tradeColumns: Column<RunTradeRow>[] = [
    { key: "entry_time", header: "ورود (UTC)", sortValue: (row) => row.entry_time, render: (row) => dateTime(row.entry_time) },
    { key: "exit_time", header: "خروج (UTC)", sortValue: (row) => row.exit_time, render: (row) => dateTime(row.exit_time) },
    { key: "entry", header: "قیمت ورود", numeric: true, sortValue: (row) => row.entry, render: (row) => num(row.entry, { max: 6 }) },
    { key: "exit", header: "قیمت خروج", numeric: true, sortValue: (row) => row.exit, render: (row) => num(row.exit, { max: 6 }) },
    { key: "qty", header: "مقدار", numeric: true, sortValue: (row) => row.qty, render: (row) => num(row.qty, { max: 6 }) },
    {
      key: "pnl",
      header: "خالص پس از کارمزد",
      numeric: true,
      sortValue: (row) => row.pnl,
      render: (row) => <span className={`tone-${tone(row.pnl)}`}>{signedMoney(row.pnl)}</span>,
    },
    { key: "fees", header: "کارمزد", numeric: true, sortValue: (row) => row.fees, render: (row) => money(row.fees) },
    { key: "reason", header: "دلیل خروج", render: (row) => text(row.reason) },
  ];

  const rejectionColumns: Column<RejectionRow>[] = [
    { key: "reason", header: "دلیل رد (عین مقدار ثبت‌شده)", render: (row) => <span className="num">{text(row.reason)}</span>, sortValue: (row) => row.reason },
    { key: "count", header: "تعداد کندل", numeric: true, sortValue: (row) => row.count, render: (row) => num(row.count, { max: 0 }) },
  ];

  async function submit() {
    const symbol = (form.symbol || config.data?.symbols?.[0] || "").toUpperCase().replace(/[-/]/g, "_");
    if (symbol === "") {
      setFormError("نماد را انتخاب کنید؛ فهرست نمادهای مجاز از /config خوانده می‌شود.");
      return;
    }
    const warmup = Number(form.warmup);
    if (!Number.isInteger(warmup) || warmup < 1 || warmup > 2000) {
      setFormError("warmup باید عددی صحیح بین ۱ تا ۲۰۰۰ باشد.");
      return;
    }
    const spread = Number(form.spread);
    if (!Number.isFinite(spread) || spread < 0 || spread > 5) {
      setFormError("اسپرد فرضی باید عددی بین ۰ تا ۵ درصد باشد.");
      return;
    }
    const body: {
      symbol: string;
      timeframe: string;
      days?: number;
      dataset_id?: string;
      params?: Record<string, number>;
      warmup: number;
      assumed_spread_pct: number;
    } = { symbol, timeframe: form.timeframe || config.data?.timeframe || "", warmup, assumed_spread_pct: spread };

    if (form.source === "dataset") {
      if (form.datasetId === "") {
        setFormError("برای اجرای روی داده‌ی ذخیره‌شده، یک مجموعه‌داده انتخاب کنید.");
        return;
      }
      body.dataset_id = form.datasetId;
    } else {
      const days = Number(form.days);
      if (!Number.isInteger(days) || days < 1 || days > 1500) {
        setFormError("«روز» باید عددی صحیح بین ۱ تا ۱۵۰۰ باشد (سقف قرارداد API).");
        return;
      }
      body.days = days;
    }

    const params: Record<string, number> = {};
    for (const field of PARAM_FIELDS) {
      const raw = (paramForm[field.key] ?? "").trim();
      if (raw === "") continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        setFormError(`پارامتر ${field.key} عدد نیست.`);
        return;
      }
      params[field.key] = value;
    }
    if (Object.keys(params).length > 0) body.params = params;

    setBusy(true);
    setFormError(null);
    try {
      const response = (await runBacktest(body)) as unknown as BacktestAck;
      const runId = response.run_id ?? null;
      if (runId && Number.isFinite(spread)) setSpreadByRun((current) => ({ ...current, [runId]: spread }));
      if (runId) setSelectedId(runId);
      setAck({ runId, metrics: response.metrics ?? null, window: response.provenance?.window ?? null, spread });
      setOpen(false);
      runsState.reload();
    } catch (error) {
      setFormError(describeError(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>بک‌تست</h1>
          <p className="mute">
            اجراها از <code>/runs</code> و جزئیات هر اجرا از <code>/runs/&#123;id&#125;</code>. همه‌ی اعداد این صفحه تاریخچه‌ی شبیه‌سازی‌شده‌ی روی
            داده‌ی گذشته است — نه معامله‌ی واقعی، نه سود ثبت‌شده در حساب.
          </p>
        </div>
        <div className="btn-row">
          <Badge kind="simulated" label="شبیه‌سازی — سود واقعی نیست" />
          <button
            type="button"
            className="btn primary small"
            onClick={() => {
              setFormError(null);
              setForm({ ...form, symbol: form.symbol || config.data?.symbols?.[0] || "" });
              setOpen(true);
            }}
          >
            اجرای بک‌تست جدید
          </button>
        </div>
      </div>

      {ack ? (
        <SuccessBlock title="اجرای شبیه‌سازی تکمیل شد و در فهرست اجراها ثبت گردید">
          <dl className="kv">
            <dt>شناسه‌ی اجرا</dt>
            <dd>{text(ack.runId)}</dd>
            <dt>پنجره‌ی ارزیابی</dt>
            <dd>{text(ack.window)}</dd>
            <dt>معامله</dt>
            <dd>{num(metricValue(ack.metrics, "trades"), { max: 0 })}</dd>
            <dt>بازده خالص</dt>
            <dd className={`rtl-text tone-${tone(metricValue(ack.metrics, "net_return_pct"))}`}>{pct(metricValue(ack.metrics, "net_return_pct"), { max: 2 })}</dd>
            <dt>کارمزد</dt>
            <dd>{money(metricValue(ack.metrics, "fees_paid"))}</dd>
            <dt>اسپرد فرضی این درخواست</dt>
            <dd>{plainPct(ack.spread, { max: 3 })}</dd>
          </dl>
          <p className="mute">
            مقدار «اسپرد فرضی» فقط در نشست همین مرورگر نگه داشته می‌شود؛ رکورد اجرا در API آن را ذخیره نمی‌کند، پس برای اجرای‌های پیشین «داده‌ای موجود
            نیست» نمایش داده می‌شود.
          </p>
        </SuccessBlock>
      ) : null}

      <Filters
        fields={[
          {
            key: "kind",
            label: "نوع اجرا",
            type: "select",
            value: kindFilter,
            onChange: setKindFilter,
            options: [
              { value: "", label: "همه" },
              { value: "backtest", label: "بک‌تست" },
              { value: "walk_forward", label: "والک‌فورورد" },
              { value: "grid", label: "شبکه‌ی پارامتر" },
            ],
          },
          {
            key: "symbol",
            label: "نماد",
            type: "select",
            value: symbolFilter,
            onChange: setSymbolFilter,
            options: [{ value: "", label: "همه" }, ...symbolOptions.map((symbol) => ({ value: symbol, label: symbol }))],
          },
        ]}
        actions={
          <button type="button" className="btn small" onClick={runsState.reload} aria-label="خواندن دوباره‌ی فهرست اجراها">
            خواندن دوباره
          </button>
        }
        onReset={() => {
          setKindFilter("");
          setSymbolFilter("");
        }}
        resultLabel={`نمایش ${num(runs.length, { max: 0 })} از ${num(allRuns.length, { max: 0 })} اجرای اخیر (فیلتر محلی — /runs ۵۰ ردیف برمی‌گرداند)`}
      />

      <section className="card" aria-label="فهرست اجراها">
        <header>
          <h2>اجرای ثبت‌شده</h2>
          <Badge kind="simulated" label="شبیه‌سازی" />
        </header>
        <StateBlocks state={runsState} emptyLabel={NO_DATA} emptyHint="هنوز هیچ اجراهی (بک‌تست/والک‌فورورد/شبکه) ثبت نشده است.">
          {(rows) => (
            <>
              <Table
                rows={runs}
                getRowId={(row) => row.id}
                caption="اجراهای شبیه‌سازی ثبت‌شده در جدول runs"
                emptyLabel={NO_DATA}
                emptyHint="با این فیلترها اجرایی پیدا نشد."
                onRowClick={(row) => setSelectedId(row.id)}
                defaultSort={{ key: "created", dir: "desc" }}
                columns={runColumns}
              />
              <ProvenanceNote provenance={null} fallbackKind="simulated" extra={`فهرست از /runs · ${num(rows.length, { max: 0 })} ردیف`} />
            </>
          )}
        </StateBlocks>
      </section>

      {selected === null ? (
        <section className="card" aria-label="جزئیات اجرا">
          <header>
            <h2>جزئیات اجرا</h2>
            <Badge kind="simulated" />
          </header>
          <EmptyBlock label={NO_DATA} hint="برای دیدن شاخص‌ها، منحنی سرمایه و فهرست معامله‌ها یک اجرا انتخاب یا بسازید." />
        </section>
      ) : (
        <>
          <section className="card" aria-label="شناسه‌ی اجرا">
            <header>
              <h2>
                {text(selected.id)} — {kindFa(selected.kind)}
              </h2>
              <Badge kind="simulated" label="شبیه‌سازی" />
            </header>
            <dl className="kv">
              <dt>نماد / تایم‌فریم</dt>
              <dd>{`${text(selected.symbol)} · ${text(selected.timeframe)}`}</dd>
              <dt>وضعیت</dt>
              <dd className="rtl-text">{text(run?.status ?? selected.status)}</dd>
              <dt>نوع اجرا</dt>
              <dd className="rtl-text">{kindFa(run?.kind ?? selected.kind)}</dd>
              <dt>پارامترها (برچسب)</dt>
              <dd>{text(run?.params_label ?? selected.params_label)}</dd>
              <dt>مجموعه‌داده</dt>
              <dd>{text(run?.dataset_id ?? selected.dataset_id)}</dd>
              <dt>پنجره‌ی ارزیابی</dt>
              <dd>{windowText ?? NO_DATA}</dd>
              <dt>زمان اجرا</dt>
              <dd>{dateTime(run?.created_at ?? selected.created_at)}</dd>
              <dt>مدت (ms)</dt>
              <dd>{num(run?.duration_ms ?? selected.duration_ms, { max: 0 })}</dd>
              <dt>اسپرد فرضی</dt>
              <dd>{assumedSpread === null ? NO_DATA : plainPct(assumedSpread, { max: 3 })}</dd>
              {run?.error || selected.error ? (
                <>
                  <dt>خطای ثبت‌شده</dt>
                  <dd className="rtl-text tone-neg">{text(run?.error || selected.error)}</dd>
                </>
              ) : null}
            </dl>
            <p className="mute">
              پارامترهای تنظیمی ثبت‌شده در این اجرا: <span className="num">{paramsSummary}</span>
            </p>
            <ProvenanceNote
              provenance={provenance ?? null}
              fallbackKind="simulated"
              extra={`/runs/${text(selected.id)} — خروجی شبیه‌سازی، ثبت‌شده در جدول runs`}
            />
          </section>

          <section aria-label="شاخص‌های اجرا">
            <h2>شاخص‌های این اجرا</h2>
            {detailState.loading ? (
              <p className="notice">
                <strong>در حال خواندن جزئیات اجرا…</strong>
                <span>
                  تا رسیدن پاسخ <code>/runs/&#123;id&#125;</code> فقط شاخص‌های ذخیره‌شده در فهرست اجرا نمایش داده می‌شود؛ منحنی سرمایه، فهرست
                  معامله‌ها و شمارش ردّ‌ها خالی می‌مانند و برایشان عددی ساخته نمی‌شود.
                </span>
              </p>
            ) : null}
            {detailState.error ? (
              <div className="notice danger" role="alert">
                <strong>جزئیات این اجرا خوانده نشد.</strong>
                <span>{detailState.error}</span>
              </div>
            ) : null}
            <div className="grid stats">
              {METRIC_DEFS.map((def) => {
                const value = metricValue(metrics, def.key);
                const signedFigure = def.kind === "money" || def.kind === "pct";
                return (
                  <Stat
                    key={def.key}
                    label={def.label}
                    value={formatMetric(value, def.kind)}
                    tone={signedFigure ? tone(value) : "neutral"}
                    window={windowText}
                    source={provenance?.source ?? "engine (شبیه‌سازی)"}
                    endpoint={`/runs/${text(selected.id)}`}
                    provenance={{ kind: "simulated", source: provenance?.source ?? "engine", window: provenance?.window ?? windowText, generated_at: provenance?.generated_at ?? null }}
                    tooltip={
                      def.key === "sharpe_annualised_crude" || def.key === "sortino_annualised_crude"
                        ? `${sharpeNote ?? "موتور برای این اجرا متن ملاحظه‌ای نفرستاده؛ عدد تحویلی و خام است."} — این عدد به‌تنهایی معیار پذیرش نیست.`
                        : def.tooltip
                    }
                    note={def.key === "max_drawdown_pct" ? "بیشترین افت در همین پنجره‌ی شبیه‌سازی؛ سقف drawdown حساب از پیکربندی جدا اعمال می‌شود." : undefined}
                  />
                );
              })}
              <Stat
                label="اسپرد فرضی"
                value={assumedSpread === null ? null : plainPct(assumedSpread, { max: 3 })}
                window={windowText}
                source="درخواست همین نشست"
                endpoint="/runs/backtest"
                note="اگر API آن را در رکورد اجرا نگه ندارد، «داده‌ای موجود نیست» می‌ماند."
              />
              <Stat
                label="مجموع رد سیگنال"
                value={run === null ? null : num(rejectionTotal, { max: 0 })}
                window={windowText}
                source="run_rejections"
                endpoint={`/runs/${text(selected.id)}`}
                note="جمع شمارش‌های ثبت‌شده در جدول ردِّ اجرا؛ این‌ها فیلترهای سیگنال/ریسک هستند، نه معامله‌ی انجام‌شده."
              />
            </div>
            <p className="notice warn">
              <strong>ملاحظه‌ی شارپ (عین متن API):</strong>
              <span>{sharpeNote ?? "متن ملاحظه در پاسخ /runs نبود؛ با این حال شارپ و سورتینوی این اجرا تحویلی و خام‌اند و پیش‌بینی نمی‌کنند."}</span>
            </p>
          </section>

          <section className="grid two" aria-label="منحنی سرمایه و افت">
            <div className="card">
              <header>
                <h2>منحنی سرمایه — شبیه‌سازی</h2>
                <Badge kind="simulated" label="شبیه‌سازی" />
              </header>
              <DetailGate state={detailState} run={run} emptyHint="پاسخ /runs/{id} نقاط equity_curve نداشت؛ منحنی‌ای ساخته نمی‌شود.">
                {equityPoints.length === 0 ? (
                  <EmptyBlock label={NO_DATA} hint="هیچ نقطه‌ای در equity_curve این اجرا ثبت نشده است." />
                ) : (
                  <>
                    <LineChart
                      series={[{ id: "equity", label: "Equity شبیه‌سازی‌شده (بک‌تست)", points: equityPoints, color: "var(--accent)" }]}
                      windowLabel={windowText}
                      sourceLabel={provenance?.source ?? "engine"}
                      unit="USDT"
                      ariaLabel="منحنی سرمایه‌ی شبیه‌سازی"
                    />
                    <p className="notice warn">
                      این منحنی تاریخچه‌ی شبیه‌سازی است: با کارمزد و اسپرد فرضی روی داده‌ی گذشته محاسبه شده و سود واقعی یا وجه قابل‌برداشت نیست.
                    </p>
                    <ProvenanceNote provenance={provenance} fallbackKind="simulated" extra="equity_curve از /runs/{id}" />
                  </>
                )}
              </DetailGate>
            </div>
            <div className="card">
              <header>
                <h2>افت سرمایه از قله</h2>
                <ToneBadge tone="warn" title="این سری در API فیلد جداگانه ندارد و از همان equity_curve ساخته می‌شود">
                  محاسبه‌ی محلی از equity_curve
                </ToneBadge>
              </header>
              <DetailGate state={detailState} run={run} emptyHint="پاسخ /runs/{id} نقاط equity_curve نداشت؛ منحنی افت رسم نمی‌شود.">
                {drawdownPoints.length < 2 ? (
                  <EmptyBlock label={NO_DATA} hint="برای رسم افت به دست‌کم دو نقطه‌ی واقعی در equity_curve نیاز است." />
                ) : (
                  <>
                    <DrawdownChart points={drawdownPoints} windowLabel={windowText} sourceLabel={provenance?.source ?? "engine"} />
                    <p className="mute">
                      افت از قله‌ی همین منحنی کم می‌شود (محاسبه‌ی محلی از نقاط واقعیِ ذخیره‌شده‌ی همین اجرا، با همان فرمولی که موتور برای
                      max_drawdown_pct استفاده می‌کند)؛ مقدار ثبت‌شده در API: {pct(metricValue(metrics, "max_drawdown_pct"), { max: 2 })}
                    </p>
                    <ProvenanceNote provenance={provenance} fallbackKind="simulated" extra="محاسبه‌ی محلی از equity_curve — نه فیلد جداگانه‌ی API" />
                  </>
                )}
              </DetailGate>
            </div>
          </section>

          <section className="card" aria-label="معامله‌های شبیه‌سازی‌شده">
            <header>
              <h2>معامله‌های این اجرا</h2>
              <Badge kind="simulated" label="شبیه‌سازی" />
            </header>
            <DetailGate state={detailState} run={run} emptyHint="جزئیات این اجرا خوانده نشد؛ فهرست معامله‌ها خالی نمایش داده نمی‌شود.">
              <Table
                rows={trades}
                getRowId={(row, index) => `trade-${String(row.entry_time)}-${String(index)}`}
                caption="معامله‌های شبیه‌سازی‌شده‌ی این اجرا"
                emptyLabel="بدون معامله"
                emptyHint="در این بازه هیچ معامله‌ای باز و بسته نشده است؛ شاخص‌های مبتنی بر معامله هم null می‌مانند و عددی ساخته نمی‌شود."
                defaultSort={{ key: "exit_time", dir: "asc" }}
                columns={tradeColumns}
              />
            </DetailGate>
            <p className="mute">
              قیمت ورود/خروج همان قیمت‌های شبیه‌سازی با اسلیپیج پیکربندی است. اگر لیست خالی است، یعنی استراتژی در این پنجره وارد نشده — نه اینکه صفر
              سود «واقعی» داشته‌ایم.
            </p>
            <ProvenanceNote provenance={provenance} fallbackKind="simulated" extra="/runs/{id} → trades" />
          </section>

          <section className="grid two" aria-label="دلایل رد و همه‌ی شاخص‌ها">
            <div className="card">
              <header>
                <h2>دلایل رد سیگنال</h2>
                <ToneBadge tone={rejections.length > 0 ? "warn" : "neutral"}>
                  {rejections.length > 0 ? `${num(rejectionTotal, { max: 0 })} رد` : "هیچ ردی ثبت نشده"}
                </ToneBadge>
              </header>
              <DetailGate state={detailState} run={run} emptyHint="جزئیات این اجرا خوانده نشد؛ شمارش ردّ‌ها نمایش داده نمی‌شود.">
                <Table
                  rows={rejections}
                  getRowId={(row) => `rej-${row.reason}`}
                  caption="شمارش دلایل رد در کندل‌های بررسی‌شده"
                  emptyLabel="هیچ ردی ثبت نشده"
                  emptyHint="در این اجرا هیچ سیگنالی رد نشده است — با این حال این به‌معنی ورودهای زیاد نیست."
                  columns={rejectionColumns}
                  defaultSort={{ key: "count", dir: "desc" }}
                />
              </DetailGate>
              <p className="mute">
                هر دلیل رد شمارش کندل‌هایی است که در آن سیگنال/ریسک‌گیت اجازه‌ی ورود نداده است (مثلاً <code>insufficient_history</code> یا سقف
                ریسک). متن دلیل عیناً از پاسخ API می‌آید.
              </p>
              <ProvenanceNote provenance={provenance} fallbackKind="simulated" extra="/runs/{id} → rejections" />
            </div>

            <div className="card">
              <header>
                <h2>همه‌ی شاخص‌های ذخیره‌شده</h2>
                <Badge kind="simulated" />
              </header>
              {extraMetrics.length === 0 ? (
                <EmptyBlock label={NO_DATA} hint="فیلد دیگری در metrics این اجرا ثبت نشده است." />
              ) : (
                <dl className="kv">
                  {extraMetrics.map(([key, value]) => (
                    <MetricPair key={`mx-${key}`} label={key} value={value} />
                  ))}
                </dl>
              )}
              <p className="mute">
                برای اجراهای kind=walk_forward این بلوک aggregate پنجره‌های برون‌نمونه‌ای را نشان می‌دهد (مثلاً <code>positive_folds</code>)؛ پذیرش
                نسخه‌ی استراتژی در صفحه‌ی «نسخه‌های استراتژی» انجام می‌شود، نه از این جدول.
              </p>
              <ProvenanceNote provenance={provenance} fallbackKind="simulated" extra="/runs/{id} → metrics" />
            </div>
          </section>
        </>
      )}

      <Modal
        open={open}
        title="اجرای بک‌تست جدید"
        description="شبیه‌سازی روی داده‌ی گذشته در حالت پژوهش؛ هیچ سفارشی به صرافی فرستاده نمی‌شود و حساب paper تغییر نمی‌کند."
        onClose={() => setOpen(false)}
        width={760}
        footer={
          <>
            <button type="button" className="btn primary" onClick={submit} disabled={busy}>
              {busy ? "در حال اجرای شبیه‌سازی…" : "اجرای بک‌تست"}
            </button>
            <button type="button" className="btn ghost" onClick={() => setOpen(false)} disabled={busy}>
              انصراف
            </button>
          </>
        }
      >
        <div className="grid two">
          <FormRow id="bt-symbol" label="نماد" hint="فقط نمادهای مجازِ /config پذیرفته می‌شود.">
            <select id="bt-symbol" value={form.symbol} onChange={(event) => setForm({ ...form, symbol: event.target.value, datasetId: "" })}>
              <option value="">پیکربندی فعال</option>
              {(config.data?.symbols ?? []).map((symbol) => (
                <option key={symbol} value={symbol}>
                  {symbol}
                </option>
              ))}
            </select>
          </FormRow>
          <FormRow id="bt-tf" label="تایم‌فریم">
            <select id="bt-tf" value={form.timeframe} onChange={(event) => setForm({ ...form, timeframe: event.target.value })}>
              <option value="">پیکربندی فعال</option>
              {TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </FormRow>
          <FormRow id="bt-source" label="منبع داده">
            <select id="bt-source" value={form.source} onChange={(event) => setForm({ ...form, source: event.target.value })}>
              <option value="dataset">مجموعه‌داده‌ی ذخیره‌شده</option>
              <option value="days">دریافت بازه‌ی روز از صرافی</option>
            </select>
          </FormRow>
          {form.source === "dataset" ? (
            <FormRow id="bt-dataset" label="مجموعه‌داده" hint="فقط داده‌ای که گزارش کیفیت دارد؛ اگر ناقص/کهنه باشد همان هشدار در صفحه‌ی داده می‌آید.">
              <select id="bt-dataset" value={form.datasetId} onChange={(event) => setForm({ ...form, datasetId: event.target.value })}>
                <option value="">انتخاب کنید</option>
                {datasetOptions.map((row) => (
                  <option key={row.id} value={row.id}>
                    {`${row.id} · ${row.symbol} ${row.timeframe} · ${num(row.bars, { max: 0 })} کندل`}
                  </option>
                ))}
              </select>
            </FormRow>
          ) : (
            <FormRow id="bt-days" label="روز" hint="۱ تا ۱۵۰۰ — سرویس برای همین بازه تاریخچه می‌گیرد و dataset می‌سازد.">
              <input id="bt-days" type="number" min={1} max={1500} value={form.days} onChange={(event) => setForm({ ...form, days: event.target.value })} />
            </FormRow>
          )}
          <FormRow id="bt-warmup" label="کندل گرم‌کردن (warmup)" hint="حداقل تعداد کندل قبل از اولین تصمیم.">
            <input id="bt-warmup" type="number" min={1} max={2000} value={form.warmup} onChange={(event) => setForm({ ...form, warmup: event.target.value })} />
          </FormRow>
          <FormRow id="bt-spread" label="اسپرد فرضی (٪)" hint="روی هر ورود/خروج اعمال می‌شود و در همین صفحه اعلام می‌گردد. سقف قرارداد: ۵">
            <input id="bt-spread" type="number" min={0} max={5} step="0.01" value={form.spread} onChange={(event) => setForm({ ...form, spread: event.target.value })} />
          </FormRow>
        </div>

        <div className="panel">
          <h3>بازنویسی پارامترها (اختیاری)</h3>
          <div className="grid stats">
            {PARAM_FIELDS.map((field) => (
              <FormRow key={field.key} id={`bt-param-${field.key}`} label={`${field.label} — ${field.key}`} hint={field.hint}>
                <input
                  id={`bt-param-${field.key}`}
                  type="number"
                  step="any"
                  value={paramForm[field.key] ?? ""}
                  placeholder="پیش‌فرض پیکربندی"
                  onChange={(event) => setParamForm({ ...paramForm, [field.key]: event.target.value })}
                />
              </FormRow>
            ))}
          </div>
          <p className="mute">
            فیلدهای خالی از <code>strategy_params</code> پیکربندی خوانده می‌شوند. سقف‌های ریسک (بودجه، زیان روزانه، اهرم) پارامتر بک‌تست نیستند و از
            این‌جا قابل تغییر نمی‌مانند.
          </p>
        </div>

        {busy ? <LoadingBlock label="اجرای شبیه‌سازی و ثبت run…" /> : null}
        {formError ? (
          <div className="notice danger" role="alert">
            <strong>اجرا انجام نشد.</strong>
            <span>{formError}</span>
          </div>
        ) : null}
        <p className="mute">
          داده‌ی ناکافی یا قطع اتصال با پیام فارسی (۴۲۲/۵۰۳) رد می‌شود و سیستم در حالت امن می‌ماند؛ در آن حالت هیچ عدد عملکردی نمایش داده نمی‌شود.
        </p>
      </Modal>

      <p className="notice warn">
        نتیجه‌ی بک‌تست تنها یک ارزیابی درون‌نمونه‌ای است. نرخ برد، ضریب سود و شارپ بدون آزمون برون‌نمونه‌ای (walk-forward) و بدون در نظر گرفتن تغییر
        رژیم بازار، لبه‌ی آماری محسوب نمی‌شوند — و هیچ‌کدام وعده‌ی سود آینده نیستند. بازده گذشته تضمین‌کننده‌ی آینده نیست.
      </p>
    </>
  );
}

/** One row of the raw `metrics` mapping (keeps every stored key visible, including strings). */
function MetricPair({ label, value }: { label: string; value: unknown }) {
  const rendered =
    typeof value === "number" ? num(value, { max: 4 }) : value === null || value === undefined ? NO_DATA : typeof value === "string" ? value : JSON.stringify(value);
  const numeric = typeof value === "number";
  return (
    <>
      <dt className="num">{label}</dt>
      <dd className={numeric ? undefined : "rtl-text"}>{rendered}</dd>
    </>
  );
}

/**
 * Exactly one of loading / error / no-detail / content for `GET /runs/{id}`.
 * The content branch only ever sees the detail of the run that is selected now.
 */
function DetailGate({
  state,
  run,
  emptyHint,
  children,
}: {
  state: AsyncState<RunDetail | null>;
  run: RunDetail | null;
  emptyHint: string;
  children: ReactNode;
}) {
  if (state.loading) return <LoadingBlock label="خواندن جزئیات اجرا از /runs/{id}…" />;
  if (state.error !== null) return <ErrorBlock message={state.error} onRetry={state.reload} />;
  if (run === null) return <EmptyBlock label={NO_DATA} hint={emptyHint} />;
  return <>{children}</>;
}
