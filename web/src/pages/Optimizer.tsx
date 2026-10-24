import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { describeError, getConfig, getDatasets, useAsync, type DatasetInfo } from "../api/client";
import {
  getOptimizerRun,
  listOptimizerRuns,
  optimizerExportUrl,
  resumeOptimizer,
  startOptimizer,
  stopOptimizer,
  type OptimizerExperiment,
  type OptimizerRunSummary,
  type OptimizerState,
} from "../api/optimizerClient";
import { ToneBadge } from "../components/Badge";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import Stat from "../components/Stat";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import { NO_DATA, dateTime, num, pct, signed, text } from "../format";

/**
 * `/optimizer` — the autonomous research loop. It starts from the same store
 * candles every other research page uses, measures one pre-registered
 * hypothesis per experiment, and only keeps a version that passed the
 * out-of-sample acceptance gate. Nothing on this page can place an order,
 * change the mode or touch a risk limit.
 */

const TIMEFRAMES = ["15m", "1h", "4h", "1d"];
const STATUS_LABEL: Record<string, string> = {
  running: "در حال اجرا",
  stopping: "در حال توقف",
  stopped: "متوقف",
  done: "تمام‌شده",
  failed: "ناموفق",
};
const STATUS_TONE: Record<string, "pos" | "neg" | "warn" | "neutral" | "info"> = {
  running: "info",
  stopping: "warn",
  stopped: "neutral",
  done: "pos",
  failed: "neg",
};

function statusLabel(status: string | null | undefined): string {
  return status ? (STATUS_LABEL[status] ?? status) : NO_DATA;
}

function statusBadge(status: string | null | undefined): ReactNode {
  return (
    <ToneBadge tone={STATUS_TONE[status ?? ""] ?? "neutral"}>{statusLabel(status)}</ToneBadge>
  );
}

function gridLabel(grid: Record<string, unknown[]>): string {
  const parts = Object.entries(grid).map(([key, values]) => `${key}=${values.map(String).join(" / ")}`);
  return parts.length ? parts.join(" · ") : NO_DATA;
}

function datasetLabel(row: DatasetInfo): string {
  const span =
    row.start_ts && row.end_ts ? `${dateTime(row.start_ts)} → ${dateTime(row.end_ts)}` : NO_DATA;
  return `${row.symbol} ${row.timeframe} · ${num(row.bars, { max: 0 })} کندل · ${span}`;
}

export default function Optimizer() {
  const [tick, setTick] = useState<number>(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [open, setOpen] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const config = useAsync((signal) => getConfig(signal), []);
  const datasets = useAsync((signal) => getDatasets(signal), []);
  const runs = useAsync((signal) => listOptimizerRuns(20, signal), [tick]);
  const detail = useAsync(
    (signal) => (selectedId ? getOptimizerRun(selectedId, signal) : Promise.resolve(null)),
    [selectedId, tick],
  );

  const [form, setForm] = useState({
    symbol: "",
    timeframe: "4h",
    source: "days" as "days" | "dataset",
    days: "180",
    datasetId: "",
    maxExperiments: "3",
    folds: "3",
    warmup: "80",
    spread: "0.05",
  });

  useEffect(() => {
    if (form.symbol) return;
    const first = (config.data?.symbols ?? [])[0] ?? "";
    if (first) setForm((value) => ({ ...value, symbol: first }));
  }, [config.data, form.symbol]);

  useEffect(() => {
    const tf = config.data?.timeframe;
    if (tf && TIMEFRAMES.includes(tf)) setForm((value) => ({ ...value, timeframe: tf }));
  }, [config.data?.timeframe]);

  const runRows = runs.data?.runs ?? [];
  const anyActive = runRows.some((run) => run.status === "running" || run.status === "stopping");
  useEffect(() => {
    if (!anyActive) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 3000);
    return () => window.clearInterval(timer);
  }, [anyActive]);

  useEffect(() => {
    const newest = runRows[0]?.run_id;
    if (selectedId || !newest) return;
    setSelectedId(newest);
  }, [runRows, selectedId]);

  const matchingDatasets = useMemo(() => {
    const symbol = form.symbol.toUpperCase().replace(/\//g, "_");
    return (datasets.data ?? []).filter(
      (row) => row.symbol.toUpperCase().replace(/\//g, "_") === symbol && row.timeframe === form.timeframe,
    );
  }, [datasets.data, form.symbol, form.timeframe]);

  const selectedSummary: OptimizerRunSummary | null =
    runRows.find((run) => run.run_id === selectedId) ?? null;
  const state: OptimizerState | null = detail.data ?? null;

  async function withBusy(action: () => Promise<void>) {
    setBusy(true);
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setBusy(false);
    }
  }

  async function submitStart() {
    setFormError(null);
    const days = Number.parseInt(form.days, 10);
    if (form.source === "dataset" && !form.datasetId) {
      setFormError("یک مجموعه‌داده‌ی ذخیره‌شده انتخاب کنید یا منبع را روی «دریافت روزها» بگذارید.");
      return;
    }
    if (form.source === "days" && (!Number.isFinite(days) || days < 120)) {
      setFormError("حداقل ۱۲۰ روز داده لازم است تا تقسیم آموزش/اعتبارسنجی صادقانه باشد.");
      return;
    }
    await withBusy(async () => {
      const started = await startOptimizer({
        symbol: form.symbol,
        timeframe: form.timeframe,
        days: form.source === "days" ? days : undefined,
        dataset_id: form.source === "dataset" ? form.datasetId : undefined,
        max_experiments: Number.parseInt(form.maxExperiments, 10),
        folds: Number.parseInt(form.folds, 10),
        warmup: Number.parseInt(form.warmup, 10),
        assumed_spread_pct: Number.parseFloat(form.spread),
      });
      setOpen(false);
      setSelectedId(started.run_id);
      setTick((value) => value + 1);
    });
  }

  const experimentColumns: Column<OptimizerExperiment>[] = [
    {
      key: "index",
      header: "#",
      numeric: true,
      sortValue: (row) => row.index,
      render: (row) => num(row.index, { max: 0 }),
    },
    {
      key: "hypothesis",
      header: "فرضیه",
      sortValue: (row) => row.hypothesis_id,
      render: (row) => (
        <div>
          <code>{row.hypothesis_id}</code>
          <div className="mute">{row.question}</div>
          <div className="mute" dir="ltr">{gridLabel(row.grid)}</div>
        </div>
      ),
    },
    {
      key: "verdict",
      header: "داوری",
      render: (row) =>
        row.verdict === "accepted" ? (
          <ToneBadge tone="pos">پذیرفته</ToneBadge>
        ) : row.verdict === "rejected" ? (
          <ToneBadge tone="neg">رد</ToneBadge>
        ) : (
          <ToneBadge tone="neutral">{text(row.verdict)}</ToneBadge>
        ),
    },
    {
      key: "net",
      header: "خالص خارج از نمونه",
      numeric: true,
      sortValue: (row) => row.net_return_pct,
      render: (row) => pct(row.net_return_pct),
    },
    {
      key: "drawdown",
      header: "افت سرمایه",
      numeric: true,
      sortValue: (row) => row.max_drawdown_pct,
      render: (row) => pct(row.max_drawdown_pct),
    },
    {
      key: "pf",
      header: "ضریب سود",
      numeric: true,
      sortValue: (row) => row.profit_factor,
      render: (row) => num(row.profit_factor, { max: 2 }),
    },
    {
      key: "trades",
      header: "معامله",
      numeric: true,
      sortValue: (row) => row.trades,
      render: (row) => num(row.trades, { max: 0 }),
    },
    {
      key: "reasons",
      header: "دلیل رد",
      render: (row) => (
        <div>
          {row.reasons.length ? (
            <ul className="mute" style={{ margin: 0, paddingInlineStart: 18 }}>
              {row.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : (
            <span className="mute">—</span>
          )}
        </div>
      ),
    },
    {
      key: "learning",
      header: "درس ثبت‌شده",
      render: (row) => <div>{row.learning}</div>,
    },
    {
      key: "decided_at",
      header: "زمان تصمیم",
      sortValue: (row) => row.decided_at,
      render: (row) => <span title={text(row.decided_at)}>{dateTime(row.decided_at)}</span>,
    },
  ];

  const runColumns: Column<OptimizerRunSummary>[] = [
    {
      key: "run_id",
      header: "شناسه",
      sortValue: (row) => row.run_id,
      render: (row) => <code>{row.run_id}</code>,
    },
    {
      key: "status",
      header: "وضعیت",
      sortValue: (row) => row.status,
      render: (row) => statusBadge(row.status),
    },
    {
      key: "market",
      header: "نماد / تایم‌فریم",
      sortValue: (row) => `${row.symbol ?? ""}${row.timeframe ?? ""}`,
      render: (row) => `${text(row.symbol)} · ${text(row.timeframe)}`,
    },
    {
      key: "progress",
      header: "پیشرفت",
      numeric: true,
      sortValue: (row) => row.completed,
      render: (row) => `${num(row.completed, { max: 0 })} / ${num(row.completed + row.planned, { max: 0 })}`,
    },
    {
      key: "accepted",
      header: "پذیرفته",
      numeric: true,
      sortValue: (row) => row.accepted,
      render: (row) => num(row.accepted, { max: 0 }),
    },
    {
      key: "rejected",
      header: "رد",
      numeric: true,
      sortValue: (row) => row.rejected,
      render: (row) => num(row.rejected, { max: 0 }),
    },
    {
      key: "best",
      header: "بهترین خالص",
      numeric: true,
      sortValue: (row) => row.best_net_return_pct,
      render: (row) => signed(row.best_net_return_pct, { max: 2 }),
    },
    {
      key: "champion",
      header: "قهرمان فعلی",
      render: (row) => <span dir="ltr">{text(row.champion_label, "—")}</span>,
    },
    {
      key: "updated",
      header: "آخرین به‌روزرسانی",
      sortValue: (row) => row.updated_at,
      render: (row) => <span title={text(row.updated_at)}>{dateTime(row.updated_at)}</span>,
    },
  ];

  const canStop = state?.status === "running" || selectedSummary?.status === "running";
  const canResume =
    selectedSummary?.status === "stopped" ||
    selectedSummary?.status === "failed" ||
    state?.status === "stopped" ||
    state?.status === "failed";

  return (
    <>
      <section className="card">
        <header>
          <div>
            <h2>حلقه‌ی بهینه‌سازی خودکار</h2>
            <p className="mute">
              هر آزمایش یک فرضیه‌ی از پیش ثبت‌شده را روی کندل‌های بسته می‌سنجد: انتخاب پارامتر فقط روی نیمه‌ی آموزش،
              داوری فقط با داده‌ی خارج از نمونه. فرضیه‌ی ردشده معامله نمی‌کند؛ فقط درسش ثبت می‌شود و آزمایش بعدی را
              جهت می‌دهد.
            </p>
          </div>
          <div className="btn-row">
            <button type="button" className="btn primary" onClick={() => setOpen(true)}>
              اجرای جدید
            </button>
          </div>
        </header>
        <ProvenanceNote
          provenance={{
            kind: "simulated",
            source: "engine / store",
            window: state ? `${text(state.symbol)} ${text(state.timeframe)} · dataset ${text(state.dataset_id)}` : null,
            generated_at: state?.updated_at ?? null,
          }}
          extra="همه‌ی اعداد این صفحه بک‌تست شبیه‌سازی‌شده روی داده‌ی ذخیره‌شده‌اند؛ نتایج گذشته تضمین سود آینده نیست."
        />
      </section>

      {actionError ? (
        <div className="notice danger" role="alert">
          {actionError}
        </div>
      ) : null}

      <section className="grid stats" aria-label="شاخص‌های حلقه‌ی بهینه‌سازی">
        <Stat
          compact
          label="آزمایش‌های انجام‌شده"
          value={selectedSummary ? `${num(selectedSummary.completed, { max: 0 })} از ${num(selectedSummary.completed + selectedSummary.planned, { max: 0 })}` : null}
          endpoint="/optimizer"
          note="تعداد آزمایش‌های ثبت‌شده در برابر کل صف پیش‌ثبت‌شده."
        />
        <Stat
          compact
          label="نسخه‌های پذیرفته‌شده"
          value={selectedSummary ? num(selectedSummary.accepted, { max: 0 }) : null}
          tone={selectedSummary && selectedSummary.accepted > 0 ? "pos" : "neutral"}
          endpoint="/optimizer"
        />
        <Stat
          compact
          label="فرضیه‌های ردشده"
          value={selectedSummary ? num(selectedSummary.rejected, { max: 0 }) : null}
          endpoint="/optimizer"
          note="رد شدن نتیجه‌ی طبیعی دروازه‌ی ضدبیش‌برازش است؛ قهرمان قبلی حفظ می‌شود."
        />
        <Stat
          compact
          label="بهترین بازده خالص خارج از نمونه"
          value={selectedSummary ? pct(selectedSummary.best_net_return_pct) : null}
          tone={selectedSummary && (selectedSummary.best_net_return_pct ?? 0) > 0 ? "pos" : "neutral"}
          endpoint="/optimizer"
          tooltip="بیشترین بازده خالص ثبت‌شده میان آزمایش‌های همین اجرا، پس از کارمزد و اسلیپیج."
        />
        <Stat
          compact
          label="وضعیت"
          value={selectedSummary ? statusLabel(selectedSummary.status) : null}
          endpoint="/optimizer"
        />
      </section>

      <section className="card">
        <header>
          <h2>اجراهای بهینه‌سازی</h2>
          <span className="mute">برای دیدن جزئیات، روی یک ردیف بزنید.</span>
        </header>
        {runs.loading ? (
          <LoadingBlock label="در حال خواندن اجراهای بهینه‌سازی…" />
        ) : runs.error ? (
          <ErrorBlock message={runs.error} onRetry={runs.reload} />
        ) : runRows.length === 0 ? (
          <EmptyBlock
            label="هنوز حلقه‌ای اجرا نشده"
            hint="یک اجرای جدید بسازید؛ هر اجرا با یک مجموعه‌داده‌ی ذخیره‌شده یا دریافت روزهای مشخص شروع می‌شود."
            actions={
              <button type="button" className="btn primary" onClick={() => setOpen(true)}>
                اجرای جدید
              </button>
            }
          />
        ) : (
          <Table
            columns={runColumns}
            rows={runRows}
            getRowId={(row) => row.run_id}
            caption="اجراهای حلقه‌ی بهینه‌سازی از جدید به قدیم"
            onRowClick={(row) => setSelectedId(row.run_id)}
          />
        )}
      </section>

      {selectedId ? (
        <section className="card">
          <header>
            <div>
              <h2>جزئیات اجرا</h2>
              <p className="mute">
                {state
                  ? `${text(state.symbol)} · ${text(state.timeframe)} · ${num(state.experiments.length, { max: 0 })} آزمایش ثبت‌شده · اسلیپیج فرضی ${num(state.spread_pct, { max: 2 })}%`
                  : "در حال خواندن جزئیات…"}
              </p>
            </div>
            <div className="btn-row">
              <button
                type="button"
                className="btn small"
                disabled={busy || !canStop}
                onClick={() => void withBusy(async () => {
                  if (!selectedId) return;
                  await stopOptimizer(selectedId);
                  setTick((value) => value + 1);
                })}
              >
                توقف
              </button>
              <button
                type="button"
                className="btn small"
                disabled={busy || !canResume}
                onClick={() => void withBusy(async () => {
                  if (!selectedId) return;
                  await resumeOptimizer(selectedId);
                  setTick((value) => value + 1);
                })}
              >
                ادامه
              </button>
              <a className="btn small" href={optimizerExportUrl(selectedId, "csv")}>
                خروجی CSV
              </a>
              <a className="btn small" href={optimizerExportUrl(selectedId, "json")}>
                خروجی JSON
              </a>
            </div>
          </header>

          {detail.loading ? (
            <LoadingBlock label="در حال خواندن تاریخچه‌ی آزمایش‌ها…" />
          ) : detail.error ? (
            <ErrorBlock message={detail.error} onRetry={detail.reload} />
          ) : !state ? (
            <EmptyBlock label="جزئیاتی برای این اجرا ثبت نشده" />
          ) : (
            <>
              {state.error ? (
                <div className="notice danger" role="alert">
                  {state.error}
                </div>
              ) : null}
              <div className="grid two">
                <div className="panel">
                  <h3>چرا آزمایش بعدی این است؟</h3>
                  <p>{state.next_reason}</p>
                  <p className="mute">
                    در صف: {state.queue.length ? state.queue.join(" ← ") : "صف خالی است"}
                    {state.current ? ` · در حال اجرا: ${state.current}` : ""}
                  </p>
                </div>
                <div className="panel">
                  <h3>قهرمان فعلی</h3>
                  <p dir="ltr">{state.champion?.params_label ?? "هنوز نسخه‌ای پذیرفته نشده"}</p>
                  <p className="mute">
                    قهرمان فقط وقتی عوض می‌شود که نامزد در نیمه‌ی اعتبارسنجی هم مثبت بماند؛ در غیر این صورت نسخه‌ی
                    قبلی حفظ می‌شود.
                  </p>
                </div>
              </div>

              <Table
                columns={experimentColumns}
                rows={state.experiments}
                getRowId={(row) => `${state.run_id}-${String(row.index)}-${row.hypothesis_id}`}
                caption="آزمایش‌ها، داوری خارج از نمونه و درس ثبت‌شده"
                emptyLabel="هنوز آزمایشی کامل نشده"
                emptyHint="پس از پایان اولین آزمایش، نتیجه و دلیل پذیرش/رد همین‌جا ثبت می‌شود."
              />

              <p className="mute">
                منحنی سرمایه‌ی هر آزمایش در این نسخه ذخیره نمی‌شود؛ افت سرمایه‌ی تجمیعی و بازده خالص همان اعدادی
                هستند که موتور والک‌فورورد گزارش کرده است. {state.disclaimer}
              </p>
            </>
          )}
        </section>
      ) : null}

      <Modal
        open={open}
        title="اجرای جدید حلقه‌ی بهینه‌سازی"
        description="هر اجرا فقط روی کندل‌های ذخیره‌شده کار می‌کند؛ هیچ سفارشی ساخته نمی‌شود و حدود ریسک تغییر نمی‌کند."
        onClose={() => {
          if (!busy) setOpen(false);
        }}
        footer={
          <>
            <button type="button" className="btn primary" disabled={busy} onClick={() => void submitStart()}>
              {busy ? "در حال شروع…" : "شروع پژوهش"}
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setOpen(false)}>
              انصراف
            </button>
          </>
        }
      >
        <div className="filters">
          <div className="field">
            <label htmlFor="opt-symbol">نماد</label>
            <select
              id="opt-symbol"
              value={form.symbol}
              onChange={(event) => setForm({ ...form, symbol: event.target.value, datasetId: "" })}
            >
              {(config.data?.symbols ?? []).map((symbol) => (
                <option key={symbol} value={symbol}>
                  {symbol}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="opt-timeframe">تایم‌فریم</label>
            <select
              id="opt-timeframe"
              value={form.timeframe}
              onChange={(event) => setForm({ ...form, timeframe: event.target.value, datasetId: "" })}
            >
              {TIMEFRAMES.map((timeframe) => (
                <option key={timeframe} value={timeframe}>
                  {timeframe}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="opt-source">منبع داده</label>
            <select
              id="opt-source"
              value={form.source}
              onChange={(event) => setForm({ ...form, source: event.target.value === "dataset" ? "dataset" : "days" })}
            >
              <option value="days">دریافت روزهای مشخص از صرافی</option>
              <option value="dataset">مجموعه‌داده‌ی ذخیره‌شده</option>
            </select>
          </div>
          {form.source === "days" ? (
            <div className="field">
              <label htmlFor="opt-days">روزها</label>
              <input
                id="opt-days"
                type="number"
                min={120}
                max={1500}
                value={form.days}
                onChange={(event) => setForm({ ...form, days: event.target.value })}
              />
            </div>
          ) : (
            <div className="field">
              <label htmlFor="opt-dataset">مجموعه‌داده</label>
              <select
                id="opt-dataset"
                value={form.datasetId}
                onChange={(event) => setForm({ ...form, datasetId: event.target.value })}
              >
                <option value="">— انتخاب کنید —</option>
                {matchingDatasets.map((row) => (
                  <option key={row.id} value={row.id}>
                    {datasetLabel(row)}
                  </option>
                ))}
              </select>
              {matchingDatasets.length === 0 ? (
                <span className="mute">برای این نماد و تایم‌فریم مجموعه‌داده‌ای ذخیره نشده است.</span>
              ) : null}
            </div>
          )}
          <div className="field">
            <label htmlFor="opt-max">حداکثر آزمایش</label>
            <select
              id="opt-max"
              value={form.maxExperiments}
              onChange={(event) => setForm({ ...form, maxExperiments: event.target.value })}
            >
              {[1, 2, 3, 4, 5, 6].map((count) => (
                <option key={count} value={count}>
                  {count}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="opt-folds">تعداد فولد والک‌فورورد</label>
            <select
              id="opt-folds"
              value={form.folds}
              onChange={(event) => setForm({ ...form, folds: event.target.value })}
            >
              {[2, 3, 4, 5, 6].map((count) => (
                <option key={count} value={count}>
                  {count}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="opt-warmup">کندل گرم‌کردن</label>
            <input
              id="opt-warmup"
              type="number"
              min={1}
              max={500}
              value={form.warmup}
              onChange={(event) => setForm({ ...form, warmup: event.target.value })}
            />
          </div>
          <div className="field">
            <label htmlFor="opt-spread">اسلیپیج فرضی (%)</label>
            <input
              id="opt-spread"
              type="number"
              min={0}
              max={5}
              step={0.01}
              value={form.spread}
              onChange={(event) => setForm({ ...form, spread: event.target.value })}
            />
          </div>
        </div>
        {formError ? (
          <div className="notice danger" role="alert">
            {formError}
          </div>
        ) : null}
        <p className="mute">
          داده‌ی تازه از صرافی فقط برای ساخت مجموعه‌داده استفاده می‌شود؛ اگر صرافی در دسترس نباشد، درخواست با خطای
          امن رد می‌شود و هیچ عددی جایگزین نمی‌شود.
        </p>
      </Modal>
    </>
  );
}
