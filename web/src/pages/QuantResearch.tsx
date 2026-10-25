import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { describeError } from "../api/client";
import {
  getQuantExperiments,
  getQuantStatus,
  startQuantLoop,
  stopQuantLoop,
  type QuantExperiment,
  type QuantStatus,
} from "../api/quantClient";
import { ToneBadge } from "../components/Badge";
import Stat from "../components/Stat";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/StateBlocks";
import { NO_DATA, dateTime, num, pct, plainPct, text, tone } from "../format";

/**
 * `/quant` — autonomous research engine panel.
 *
 * Live values only: data coverage, storage budget, the loop process, the last
 * checkpoint and every experiment with its acceptance verdict. Start/stop act
 * on a real subprocess; a stopped run resumes from the SQLite checkpoint.
 * Nothing on this page can place an order or change a risk limit.
 */

const POLL_MS = 5000;
const PAGE_SIZE = 10;

const STRATEGY_LABEL: Record<string, string> = {
  buy_hold: "خرید و نگهداری",
  ema_trend: "روند EMA",
  trend_pullback: "پول‌بک روند",
  momentum: "مومنتوم",
  mean_reversion: "بازگشت به میانگین",
  breakout: "شکست محدوده",
};

const STATUS_LABEL: Record<string, string> = {
  running: "در حال اجرا",
  completed: "کامل‌شده",
  stopped: "متوقف",
  failed: "ناموفق",
  partial: "نیمه‌کامل",
  interrupted: "قطع‌شده",
};

function strategyLabel(strategy: string | null, timeframe: string | null): string {
  if (!strategy) return NO_DATA;
  const name = STRATEGY_LABEL[strategy] ?? strategy;
  return timeframe ? `${name} · ${timeframe}` : name;
}

function loopLabel(status: QuantStatus | null): string {
  const loop = status?.loop;
  if (!loop) return NO_DATA;
  if (loop.running) return STATUS_LABEL["running"] ?? "در حال اجرا";
  const latest = loop.latest?.status;
  if (!latest) return "بدون اجرا";
  if (latest === "running") return "متوقف — قابل ادامه";
  return STATUS_LABEL[latest] ?? latest;
}

function loopTone(status: QuantStatus | null): "pos" | "neg" | "warn" | "neutral" | "info" {
  const loop = status?.loop;
  if (!loop) return "neutral";
  if (loop.running) return "info";
  const latest = loop.latest?.status;
  if (latest === "completed") return "pos";
  if (latest === "failed") return "neg";
  if (latest === "running") return "warn";
  return "neutral";
}

const GATE_LABEL: Record<string, string> = {
  min_trades: "حداقل تعداد معامله",
  profit_factor: "ضریب سود",
  max_drawdown_pct: "حداکثر افت سرمایه",
  positive_folds_pct: "درصد فولدهای مثبت",
  min_positive_fold_pct: "حداقل فولد مثبت",
  positive_validation_net: "بازده خالص اعتبارسنجی",
  beats_champion: "بهتر از قهرمان",
};

/** Engine reasons are English gate keys; label them so a reader can act on them. */
function reasonLabel(reason: string | null): string | null {
  if (!reason) return null;
  if (reason.includes("all pre-registered gates passed")) {
    return "همه‌ی گیت‌های از پیش ثبت‌شده پاس شد و از قهرمان بهتر بود.";
  }
  return reason
    .replace(/^failed:\s*/, "رد شد — ")
    .replace(/\b([a-z_]+)\b/g, (token) => GATE_LABEL[token] ?? token)
    .replace(/\(need > 0, got /g, "(لازم: بیش از ۰، مقدار ")
    .replace(/\(need /g, "(لازم ")
    .replace(/, got /g, "، مقدار ")
    .replace(/;\s*/g, " · ");
}

function verdictNode(experiment: QuantExperiment): ReactNode {
  if (experiment.status === "failed") return <ToneBadge tone="neg">ناموفق</ToneBadge>;
  if (experiment.status === "running") return <ToneBadge tone="info">در حال اجرا</ToneBadge>;
  if (experiment.status === "interrupted") return <ToneBadge tone="warn">قطع‌شده — ادامه یافت</ToneBadge>;
  if (experiment.acceptance === "accepted") return <ToneBadge tone="pos">پذیرفته</ToneBadge>;
  if (experiment.acceptance === "rejected") return <ToneBadge tone="warn">ردشده</ToneBadge>;
  return <ToneBadge tone="neutral">ثبت نشده</ToneBadge>;
}

function ExperimentItem({ experiment }: { experiment: QuantExperiment }) {
  const validation = experiment.validation;
  const reason = experiment.reason ?? experiment.error ?? null;
  const params = experiment.params && Object.keys(experiment.params).length > 0 ? JSON.stringify(experiment.params) : null;
  return (
    <details className="quant-exp">
      <summary>
        <span className="quant-exp-head">
          <span className="quant-exp-title">
            <b>{experiment.seq === null ? NO_DATA : `#${num(experiment.seq + 1, { max: 0 })}`}</b>
            <strong>{strategyLabel(experiment.strategy, experiment.timeframe)}</strong>
            {verdictNode(experiment)}
          </span>
          <span className="mute quant-exp-time">{experiment.created_at ? dateTime(experiment.created_at) : ""}</span>
        </span>
        <span className="quant-exp-metrics">
          <span>
            معامله‌ی اعتبارسنجی<b>{num(validation.trades, { max: 0 })}</b>
          </span>
          <span>
            بازده خالص<b className={`tone-${tone(validation.net_return_pct)}`}>{pct(validation.net_return_pct)}</b>
          </span>
          <span>
            ضریب سود<b>{num(validation.profit_factor, { max: 2 })}</b>
          </span>
          <span>
            افت موثر<b>{plainPct(validation.effective_drawdown_pct, { max: 1 })}</b>
          </span>
          <span>
            فولد مثبت<b>{plainPct(experiment.walk_forward?.positive_fold_pct ?? null, { max: 0 })}</b>
          </span>
          <span>
            آزمون نهایی<b className={`tone-${tone(experiment.metrics.net_return_pct)}`}>{pct(experiment.metrics.net_return_pct)}</b>
          </span>
        </span>
        {reason ? (
          <span className="quant-exp-reason mute" dir="rtl">
            {reasonLabel(reason)}
          </span>
        ) : null}
      </summary>
      <dl className="kv quant-exp-body">
        <dt>فرضیه</dt>
        <dd className="rtl-text">{text(experiment.hypothesis, NO_DATA)}</dd>
        <dt>پارامترها</dt>
        <dd>{params ?? NO_DATA}</dd>
        <dt>نسخه‌ی کد / داده</dt>
        <dd>{`${text(experiment.code_version)} / ${text(experiment.data_version)}`}</dd>
        <dt>آزمایش بهینه‌ساز</dt>
        <dd>{num(experiment.trials, { max: 0 })}</dd>
        <dt>مدت اجرا</dt>
        <dd>{experiment.duration_s === null ? NO_DATA : `${num(experiment.duration_s, { max: 1 })} s`}</dd>
        <dt>دلیل کامل تصمیم</dt>
        <dd className="rtl-text">{text(reasonLabel(reason), NO_DATA)}</dd>
      </dl>
    </details>
  );
}

export default function QuantResearch() {
  const [status, setStatus] = useState<QuantStatus | null>(null);
  const [experiments, setExperiments] = useState<QuantExperiment[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"start" | "stop" | null>(null);
  const [showAll, setShowAll] = useState<boolean>(false);
  const [tick, setTick] = useState<number>(0);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    Promise.all([getQuantStatus(controller.signal), getQuantExperiments(60, null, controller.signal)])
      .then(([nextStatus, page]) => {
        if (!alive) return;
        setStatus(nextStatus);
        setExperiments(page.items);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!alive || controller.signal.aborted) return;
        setError(describeError(err));
      })
      .finally(() => {
        if (alive && !controller.signal.aborted) setLoading(false);
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [tick]);

  useEffect(() => {
    const timer = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const onStart = async () => {
    setBusy("start");
    setActionError(null);
    try {
      await startQuantLoop({
        resume: true,
        search_symbols: 30,
        full_symbols: 100,
        max_experiments: status?.gates?.max_experiments ?? 8,
        max_minutes: status?.gates?.max_minutes ?? 180,
      });
      reload();
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setBusy(null);
    }
  };

  const onStop = async () => {
    setBusy("stop");
    setActionError(null);
    try {
      await stopQuantLoop();
      reload();
    } catch (err) {
      setActionError(describeError(err));
    } finally {
      setBusy(null);
    }
  };

  if (loading && !status) {
    return (
      <>
        <div className="page-head">
          <div>
            <h1>پژوهش کوانت خودکار</h1>
            <p className="mute">وضعیت واقعی داده، بودجه‌ی ذخیره‌سازی و حلقه‌ی آزمایش از همین API خوانده می‌شود.</p>
          </div>
        </div>
        <LoadingBlock label="در حال خواندن وضعیت موتور پژوهش…" />
      </>
    );
  }

  if (!status) {
    return (
      <>
        <div className="page-head">
          <div>
            <h1>پژوهش کوانت خودکار</h1>
            <p className="mute">هیچ عددی بدون پاسخ سرور نمایش داده نمی‌شود.</p>
          </div>
        </div>
        <ErrorBlock message={error ?? "پاسخی از سرور نرسید"} onRetry={reload} />
      </>
    );
  }

  const data = status.data;
  const storage = status.storage;
  const loop = status.loop;
  const running = loop?.running ?? false;
  const missing = Object.entries(status.dependencies)
    .filter(([, present]) => !present)
    .map(([name]) => name);
  const probe = status.interpreter?.probe ?? null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>پژوهش کوانت خودکار</h1>
          <p className="mute">
            موتور پژوهشی روی داده‌ی محلی Binance Vision (کندل ۱ دقیقه) کار می‌کند. هر آزمایش فقط پارامترهای مجاز را
            تغییر می‌دهد، روی پنجره‌ی اعتبارسنجی سنجیده می‌شود و برای ماندن باید گیت‌های از پیش ثبت‌شده و مقایسه با
            قهرمان را رد کند. این صفحه هیچ سفارش واقعی نمی‌سازد.
          </p>
        </div>
        <div className="btn-row">
          <ToneBadge tone="info">PAPER — بدون سفارش زنده</ToneBadge>
          <button type="button" className="btn small" onClick={reload} disabled={loading}>
            تازه‌سازی
          </button>
          <button type="button" className="btn primary small" onClick={onStart} disabled={busy !== null || running}>
            {busy === "start" ? "در حال شروع…" : "شروع / ادامه‌ی حلقه"}
          </button>
          <button type="button" className="btn danger small" onClick={onStop} disabled={busy !== null || !running}>
            {busy === "stop" ? "در حال توقف…" : "توقف حلقه"}
          </button>
        </div>
      </div>

      {actionError ? (
        <div className="notice danger" role="alert">
          <strong>عملیات انجام نشد</strong>
          <span>{actionError}</span>
        </div>
      ) : null}

      {error ? (
        <div className="notice warn" role="status">
          <strong>آخرین تازه‌سازی ناموفق بود</strong>
          <span>{error}</span>
        </div>
      ) : null}

      {!status.enabled ? (
        <div className="notice warn" role="status">
          <strong>موتور پژوهش خاموش است</strong>
          <span>{status.reason ?? "quant.enabled در config.yaml فعال نیست."}</span>
        </div>
      ) : null}

      {missing.length > 0 ? (
        <div className="notice warn" role="status">
          <strong>وابستگی‌های اختیاری در مفسر API نصب نیستند: {missing.join("، ")}</strong>
          <span>
            وضعیت، داده و جدول آزمایش‌ها بدون این وابستگی‌ها کار می‌کنند. اجرای بک‌تست و حلقه با مفسر پژوهش انجام
            می‌شود: <code>{text(status.interpreter?.python, NO_DATA)}</code>
            {probe ? (probe.ok ? " — مفسر پژوهش آماده است." : ` — این مفسر هم آماده نیست: ${probe.error ?? probe.missing.join("، ")}`) : ""}
          </span>
        </div>
      ) : null}

      <div className="grid stats">
        <Stat
          compact
          label="پوشش داده"
          value={plainPct(data?.coverage_pct ?? null, { max: 2 })}
          source="manifest"
          endpoint="/quant/status"
          window={data?.months_stored?.length ? data.months_stored.join("، ") : null}
          tooltip="درصد کندل‌های موجود نسبت به کندل‌های انتظار ماه‌های کامل ثبت‌شده."
        />
        <Stat
          compact
          label="کندل ۱ دقیقه"
          value={num(data?.rows ?? null, { max: 0 })}
          source="parquet/manifest"
          endpoint="/quant/status"
        />
        <Stat
          compact
          label="نمادهای کامل"
          value={num(data?.symbols_complete ?? null, { max: 0 })}
          source="universe+manifest"
          endpoint="/quant/status"
          note={`از ${num(data?.symbols_stored ?? null, { max: 0 })} نماد ذخیره‌شده`}
        />
        <Stat
          compact
          label="مصرف بودجه‌ی ذخیره‌سازی"
          value={plainPct(storage?.used_pct ?? null, { max: 1 })}
          tone={storage?.within_budget === false ? "neg" : "neutral"}
          source="storage budget"
          endpoint="/quant/status"
          tooltip={storage?.policy}
        />
        <Stat
          compact
          label="فضای آزاد دیسک"
          value={storage?.disk_free_gib === null || storage?.disk_free_gib === undefined ? null : `${num(storage.disk_free_gib, { max: 2 })} GiB`}
          source="filesystem"
          endpoint="/quant/status"
        />
        <Stat
          compact
          label="آزمایش‌های ثبت‌شده"
          value={num(status.counts?.experiments ?? null, { max: 0 })}
          source="research.sqlite3"
          endpoint="/quant/status"
        />
        <Stat
          compact
          label="قهرمان فعلی (اعتبارسنجی)"
          value={status.champion ? pct(status.champion.validation_net_pct ?? null) : null}
          tone={tone(status.champion?.validation_net_pct ?? null)}
          source="checkpoint: champion"
          endpoint="/quant/status"
          note={status.champion ? strategyLabel(status.champion.strategy ?? null, status.champion.timeframe ?? null) : "هنوز قهرمانی پذیرفته نشده"}
        />
      </div>

      <div className="grid two">
        <section className="card">
          <header>
            <h2>حلقه‌ی پژوهش</h2>
            <ToneBadge tone={loopTone(status)}>{loopLabel(status)}</ToneBadge>
          </header>
          <dl className="kv" style={{ overflowWrap: "anywhere" }}>
            <dt>شناسه‌ی حلقه</dt>
            <dd>{text(loop?.latest?.id, "بدون اجرا")}</dd>
            <dt>فرایند</dt>
            <dd>{loop?.process.alive ? `فعال · PID ${num(loop.process.pid ?? null, { max: 0 })}` : "خاموش"}</dd>
            <dt>آخرین پیام</dt>
            <dd className="rtl-text">{text(loop?.latest?.message, NO_DATA)}</dd>
            <dt>آزمایش تکمیل‌شده</dt>
            <dd>{num(loop?.latest?.experiments ?? null, { max: 0 })}</dd>
            <dt>قابل ادامه پس از توقف</dt>
            <dd className="rtl-text">{loop?.latest?.resumable ? "بله — از آخرین checkpoint" : "—"}</dd>
            <dt>مفسر پژوهش</dt>
            <dd>{text(status.interpreter?.python, NO_DATA)}</dd>
            <dt>پایگاه پژوهش</dt>
            <dd>{text(status.research_db, NO_DATA)}</dd>
          </dl>
          {loop?.log_tail?.length ? (
            <pre
              aria-label="آخرین خطوط گزارش حلقه"
              style={{
                maxHeight: 180,
                maxWidth: "100%",
                overflow: "auto",
                direction: "ltr",
                textAlign: "left",
                fontSize: 11,
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                marginInline: 0,
              }}
            >
              {loop.log_tail.join("\n")}
            </pre>
          ) : (
            <p className="mute">هنوز خطی در گزارش حلقه ثبت نشده است.</p>
          )}
        </section>

        <section className="card">
          <header>
            <h2>منابع و گیت‌های پذیرش</h2>
            <span className="mute">نمونه‌برداری هر ۱۰ ثانیه</span>
          </header>
          <dl className="kv">
            <dt>CPU</dt>
            <dd>{plainPct(loop?.resources?.cpu_pct ?? null, { max: 1 })}</dd>
            <dt>RAM سیستم</dt>
            <dd>
              {num(loop?.resources?.ram_used_gb ?? null, { max: 2 })} /{" "}
              {num(loop?.resources?.ram_total_gb ?? null, { max: 2 })} GiB
            </dd>
            <dt>RAM این فرایند</dt>
            <dd>{num(loop?.resources?.process_ram_mb ?? null, { max: 0 })} MB</dd>
            <dt>حداقل معامله</dt>
            <dd>{num(status.gates?.min_trades ?? null, { max: 0 })}</dd>
            <dt>حداقل ضریب سود</dt>
            <dd>{num(status.gates?.min_profit_factor ?? null, { max: 2 })}</dd>
            <dt>حداکثر افت سرمایه</dt>
            <dd>{plainPct(status.gates?.max_drawdown_pct ?? null, { max: 1 })}</dd>
            <dt>حداقل فولد مثبت</dt>
            <dd>{plainPct(status.gates?.min_positive_fold_pct ?? null, { max: 0 })}</dd>
            <dt>سقف آزمایش هر حلقه</dt>
            <dd>{num(status.gates?.max_experiments ?? null, { max: 0 })}</dd>
            <dt>آخرین نمونه</dt>
            <dd>{loop?.last_resources?.ts ? dateTime(loop.last_resources.ts) : NO_DATA}</dd>
          </dl>
        </section>
      </div>

      <section className="card" style={{ marginTop: "var(--s4)" }}>
        <header>
          <h2>آزمایش‌ها</h2>
          <span className="mute">
            {num(experiments.length, { max: 0 })} رکورد از research.sqlite3 · هر رکورد نسخه‌ی کد و داده را نگه می‌دارد
          </span>
        </header>
        {experiments.length === 0 ? (
          <EmptyBlock
            label="هنوز آزمایشی ثبت نشده"
            hint="با دکمه‌ی «شروع / ادامه‌ی حلقه» اولین آزمایش واقعی روی داده‌ی محلی اجرا می‌شود."
          />
        ) : (
          <>
            <div className="quant-exp-list">
              {(showAll ? experiments : experiments.slice(0, PAGE_SIZE)).map((experiment) => (
                <ExperimentItem key={experiment.id} experiment={experiment} />
              ))}
            </div>
            {experiments.length > PAGE_SIZE ? (
              <div className="btn-row" style={{ marginTop: "var(--s3)" }}>
                <button type="button" className="btn ghost small" onClick={() => setShowAll((value) => !value)}>
                  {showAll ? "فقط ۱۰ مورد آخر" : `نمایش همه (${num(experiments.length, { max: 0 })})`}
                </button>
              </div>
            ) : null}
            <p className="mute" style={{ fontSize: 12, marginBlock: "var(--s3) 0" }}>
              روی هر ردیف بزنید تا فرضیه، پارامترهای ثبت‌شده و نسخه‌ی کد/داده باز شود.
            </p>
          </>
        )}
      </section>

      <div className="grid two" style={{ marginTop: "var(--s4)" }}>
        <section className="card">
          <header>
            <h2>داده‌ی محلی</h2>
            <ToneBadge tone={data?.gaps ? "warn" : "neutral"}>
              {data?.gaps ? `${num(data.gaps, { max: 0 })} شکاف` : "بدون شکاف"}
            </ToneBadge>
          </header>
          <dl className="kv">
            <dt>ماه‌های انتظار</dt>
            <dd>{data?.months_expected?.length ? data.months_expected.join("، ") : NO_DATA}</dd>
            <dt>ماه‌های ذخیره‌شده</dt>
            <dd>{data?.months_stored?.length ? data.months_stored.join("، ") : NO_DATA}</dd>
            <dt>حجم داده</dt>
            <dd>{num(data?.bytes === null || data?.bytes === undefined ? null : data.bytes / (1024 * 1024), { max: 1 })} MiB</dd>
            <dt>دقیقه‌های گم‌شده</dt>
            <dd>{num(data?.missing_minutes ?? null, { max: 0 })}</dd>
            <dt>نسخه‌ی داده</dt>
            <dd>{text(data?.data_version, NO_DATA)}</dd>
            <dt>ساخته‌شده در</dt>
            <dd>{data?.manifest_generated_at ? dateTime(data.manifest_generated_at) : NO_DATA}</dd>
          </dl>
          {data?.errors?.length ? (
            <div className="notice warn" role="status">
              <strong>{num(data.errors.length, { max: 0 })} خطای حل‌نشده در manifest</strong>
              <span>{data.errors.slice(0, 3).map((row) => `${row.symbol ?? "?"} ${row.month ?? ""}: ${row.error ?? ""}`).join(" | ")}</span>
            </div>
          ) : (
            <p className="mute">manifest هیچ خطای حل‌نشده‌ای ندارد.</p>
          )}
        </section>

        <section className="card">
          <header>
            <h2>محدودیت‌ها و صداقت نتایج</h2>
          </header>
          <ul className="mute" style={{ margin: 0, paddingInlineStart: "1.1em", lineHeight: 1.9 }}>
            <li>یونیورس بر اساس رتبه‌بندی فعلی بازار انتخاب شده است؛ نمادهای حذف‌شده در گذشته حاضر نیستند (بقای انتخاب).</li>
            <li>چهار ماه داده برای اثبات پایداری کافی نیست؛ نتیجه تاریخچه‌ی شبیه‌سازی‌شده است، نه پیش‌بینی.</li>
            <li>همه‌ی اعداد خالص از کارمزد و اسلیپیج‌اند و در حالت شبیه‌سازی کاغذی محاسبه شده‌اند.</li>
            <li>آزمون نهایی هر آزمایش فقط یک‌بار خوانده می‌شود و در انتخاب برنده استفاده نمی‌شود.</li>
            <li>هیچ مسیری برای معامله‌ی زنده یا افزایش سقف ریسک از این صفحه وجود ندارد.</li>
          </ul>
        </section>
      </div>
    </>
  );
}
