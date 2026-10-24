import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FlaskConical, PlayCircle, RefreshCw } from "lucide-react";
import { getDatasetBars, useAsync, describeError, type Bar } from "../api/client";
import { startOptimizer } from "../api/optimizerClient";
import {
  buildTradeAnalysisReport,
  getTradeAnalysisReport,
  listTradeAnalysisReports,
  type GroupStat,
  type TradeAnalysisReport,
  type TradeExplanation,
  type TradeHypothesis,
} from "../api/tradeAnalysisClient";
import Badge, { ToneBadge } from "../components/Badge";
import CandleChart, { type Level } from "../charts/CandleChart";
import Filters from "../components/Filters";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import { Button } from "../components/ui/button";
import { NO_DATA, dateTime, money, num, plainPct, signedMoney, text, tone } from "../format";

const GROUP_DIMS: { key: string; label: string }[] = [
  { key: "symbol", label: "نماد" },
  { key: "regime", label: "رژیم بازار" },
  { key: "exit_reason", label: "دلیل خروج" },
  { key: "entry_reason", label: "دلیل ورود" },
  { key: "side", label: "جهت معامله" },
  { key: "hold_bucket", label: "طول نگهداری" },
  { key: "weekday", label: "روز هفته" },
  { key: "hour", label: "ساعت خروج" },
];

const SAMPLE_LABEL: Record<string, { label: string; tone: "info" | "warn" | "neutral" }> = {
  sufficient: { label: "نمونه کافی", tone: "info" },
  weak: { label: "نمونه کم", tone: "warn" },
  insufficient: { label: "نمونه ناکافی", tone: "neutral" },
};

const REGIME_FA: Record<string, string> = {
  TRENDING_UP: "روند صعودی",
  TRENDING_DOWN: "روند نزولی",
  RANGING: "ردیفی",
  HIGH_VOLATILITY: "نوسان بالا",
  UNKNOWN: "نامشخص",
};

function sampleBadge(quality: string | null | undefined) {
  const known = quality ? SAMPLE_LABEL[quality] : undefined;
  if (!known) return <span className="mute">{text(quality ?? null, "نامشخص")}</span>;
  return <ToneBadge tone={known.tone} title="کیفیت نمونه: آستانه‌های ۲۰ و ۵ معامله">{known.label}</ToneBadge>;
}

function GroupTable({ groups }: { groups: GroupStat[] }) {
  const columns: Column<GroupStat>[] = [
    { key: "label", header: "گروه", render: (g) => <span className="rtl-text">{text(g.label)}</span>, sortValue: (g) => g.label },
    { key: "n", header: "تعداد", numeric: true, sortValue: (g) => g.n, render: (g) => num(g.n, { max: 0 }) },
    { key: "quality", header: "کیفیت نمونه", render: (g) => sampleBadge(g.sample_quality) },
    { key: "win", header: "نرخ برد", numeric: true, sortValue: (g) => g.win_rate_pct, render: (g) => plainPct(g.win_rate_pct, { max: 1 }) },
    { key: "expectancy", header: "انتظار (USDT)", numeric: true, sortValue: (g) => g.expectancy, render: (g) => <span className={`tone-${tone(g.expectancy)}`}>{signedMoney(g.expectancy)}</span> },
    { key: "expectancy_pct", header: "انتظار (%)", numeric: true, sortValue: (g) => g.expectancy_pct, render: (g) => <span className={`tone-${tone(g.expectancy_pct)}`}>{plainPct(g.expectancy_pct, { max: 2 })}</span> },
    { key: "pf", header: "ضریب سود", numeric: true, sortValue: (g) => g.profit_factor, render: (g) => num(g.profit_factor, { max: 2 }) },
    { key: "net", header: "خالص گروه", numeric: true, sortValue: (g) => g.net_pnl, render: (g) => <span className={`tone-${tone(g.net_pnl)}`}>{signedMoney(g.net_pnl)}</span> },
    { key: "dd", header: "بیشترین افت پشت‌سرهم", numeric: true, sortValue: (g) => g.max_drawdown_pnl, render: (g) => money(g.max_drawdown_pnl) },
    { key: "streak", header: "باخت متوالی", numeric: true, sortValue: (g) => g.max_consecutive_losses, render: (g) => num(g.max_consecutive_losses, { max: 0 }) },
    { key: "share", header: "سهم بهترین معامله", numeric: true, sortValue: (g) => g.best_trade_share_pct, render: (g) => plainPct(g.best_trade_share_pct, { max: 1 }) },
  ];
  return (
    <>
      <Table
        rows={groups}
        getRowId={(g) => `${g.group_by}-${g.key}`}
        caption={`گروه‌بندی بر اساس ${text(GROUP_DIMS.find((d) => d.key === groups[0]?.group_by)?.label ?? groups[0]?.group_by, "نامشخص")}`}
        columns={columns}
        emptyLabel="گروهی برای این بُعد وجود ندارد"
        defaultSort={{ key: "net", dir: "asc" }}
      />
      <ul className="stat-note">
        {groups.slice(0, 6).map((g) => (
          <li key={`note-${g.key}`}>
            <strong>{text(g.label)}:</strong> {g.guidance}
          </li>
        ))}
      </ul>
    </>
  );
}

function HypothesisCard({ h, onStart, starting, disabled }: {
  h: TradeHypothesis;
  onStart: (h: TradeHypothesis) => void;
  starting: boolean;
  disabled: boolean;
}) {
  return (
    <div className="card">
      <header>
        <h3 className="rtl-text">{text(h.question)}</h3>
        <div className="btn-row">
          <ToneBadge tone="neutral" title="این فرض پس از واقعه ساخته شده و هنوز آزمایش نشده است">{text(h.status, "untested")}</ToneBadge>
        </div>
      </header>
      <dl className="kv">
        <dt>تغییر پیشنهادی</dt>
        <dd className="rtl-text">{text(h.proposed_change)}</dd>
        <dt>آزمایش لازم</dt>
        <dd className="rtl-text">{text(h.required_experiment)}</dd>
        <dt>معیار موفقیت (پیش از آزمایش)</dt>
        <dd className="rtl-text">{text(h.success_criterion)}</dd>
        <dt>معیار رد</dt>
        <dd className="rtl-text">{text(h.failure_criterion)}</dd>
        <dt>شواهد موافق</dt>
        <dd className="rtl-text">{h.supporting_evidence.length > 0 ? h.supporting_evidence.join("؛ ") : "ثبت نشده"}</dd>
        <dt>شواهد مخالف</dt>
        <dd className="rtl-text">{h.opposing_evidence.length > 0 ? h.opposing_evidence.join("؛ ") : "ثبت نشده"}</dd>
        <dt>شبکه‌ی جست‌وجو</dt>
        <dd className="num">{Object.entries(h.grid ?? {}).map(([key, values]) => `${key}: [${values.map(String).join(", ")}]`).join(" · ") || "خالی"}</dd>
        <dt>معاملات مرتبط</dt>
        <dd>{num(h.related_trades, { max: 0 })}</dd>
      </dl>
      <p className="stat-note rtl-text">{text(h.mechanism)}</p>
      <div className="btn-row">
        <Button size="sm" onClick={() => onStart(h)} disabled={starting || disabled} aria-label={`شروع آزمایش برای فرضیه ${text(h.id)}`}>
          <PlayCircle size={14} aria-hidden="true" />
          {starting ? "در حال شروع…" : "شروع آزمایش در حلقه"}
        </Button>
        <Link to="/optimizer" className="btn ghost small">مشاهده‌ی حلقه‌ی بهینه‌سازی</Link>
      </div>
    </div>
  );
}

/** Replay window around the trade plus the entry/stop/target reference lines. */
function ReplayChart({ exp, report }: { exp: TradeExplanation; report: TradeAnalysisReport }) {
  const runId = exp.ledger.run_id;
  const label = useMemo(
    () => report.run_labels.find((r) => r.run_id === runId) ?? report.run_labels.find((r) => r.dataset_id) ?? null,
    [report.run_labels, runId],
  );
  const page = useAsync(async (signal) => {
    if (!label?.dataset_id) return null;
    return getDatasetBars(label.dataset_id, { limit: 5000 }, signal);
  }, [label?.dataset_id ?? ""]);

  if (!label?.dataset_id) {
    return (
      <div className="state" role="status">
        <h3>{NO_DATA}</h3>
        <p className="mute">اجرای مبنای این معامله dataset_id ثبت نکرده است؛ بازپخش ممکن نیست.</p>
      </div>
    );
  }
  if (page.loading) return <p className="mute">خواندن کندل‌های داده‌ی واقعی…</p>;
  if (page.error) return <div className="notice danger" role="alert">{page.error}</div>;

  const bars: Bar[] = page.data?.bars ?? [];
  const entryTime = exp.ledger.entry_time;
  const exitTime = exp.ledger.exit_time;
  if (bars.length === 0 || entryTime === null || entryTime === undefined) {
    return (
      <div className="state" role="status">
        <h3>{NO_DATA}</h3>
        <p className="mute">کندلی برای بازپخش در این مجموعه‌داده نیست.</p>
      </div>
    );
  }
  const entryIdx = bars.findIndex((b) => b.t >= entryTime);
  if (entryIdx < 0) {
    return (
      <div className="state" role="status">
        <h3>{NO_DATA}</h3>
        <p className="mute">کندل ورود در بازه‌ی ذخیره‌شده نیست؛ پنجره‌ی بازپخش ساخته نمی‌شود.</p>
      </div>
    );
  }
  let exitIdx = bars.length - 1;
  if (exitTime != null) {
    const found = bars.findIndex((b) => b.t >= exitTime);
    if (found >= 0) exitIdx = found;
  }
  const start = Math.max(0, entryIdx - 40);
  const end = Math.min(bars.length - 1, exitIdx + 8);
  const window = bars.slice(start, end + 1);
  const levels: Level[] = [
    { label: "ورود", value: exp.ledger.entry, tone: "entry" },
    { label: "حد ضرر اولیه", value: exp.code_recorded.initial_stop, tone: "stop" },
    { label: "هدف اولیه", value: exp.code_recorded.initial_target, tone: "target" },
  ];
  const windowLabel = `${label.symbol ?? ""} · ${window.length} کندل پیرامون معامله`;
  return (
    <CandleChart
      bars={window}
      levels={levels}
      windowLabel={windowLabel}
      sourceLabel={`dataset ${label.dataset_id}`}
      totalBars={page.data?.total_bars ?? null}
    />
  );
}

function TradeModal({ exp, report, onClose }: { exp: TradeExplanation | null; report: TradeAnalysisReport; onClose: () => void }) {
  if (!exp) return null;
  const code = exp.code_recorded;
  const analysis = exp.analysis;
  const ledger = exp.ledger;
  const indicators = Object.entries(code.indicators ?? {});
  return (
    <Modal
      open={exp !== null}
      title={`تحلیل معامله ${text(ledger.symbol, "نامشخص")}`}
      description="دو لایه جدا: آنچه کد در لحظه تصمیم ثبت کرد، و تحلیل پس از معامله که فرض است نه توجیه."
      onClose={onClose}
      width={860}
    >
      <h3>۱. دلیل ثبت‌شده‌ی کد (لحظه تصمیم)</h3>
      <dl className="kv">
        <dt>دلیل ورود</dt>
        <dd className="rtl-text">{text(code.entry_reason)}</dd>
        <dt>دلیل خروج</dt>
        <dd className="rtl-text">{text(code.exit_reason)}</dd>
        <dt>زمان تصمیم</dt>
        <dd>{code.decision_time ? dateTime(code.decision_time) : NO_DATA}</dd>
        <dt>اطمینان ثبت‌شده</dt>
        <dd>{num(code.confidence, { max: 3 })}</dd>
        <dt>رژیم بازار</dt>
        <dd className="rtl-text">{text(code.regime ? REGIME_FA[code.regime] ?? code.regime : null)}</dd>
        <dt>وضعیت حجم</dt>
        <dd className="rtl-text">{text(code.volume_state)}</dd>
        <dt>سیگنال‌های صعودی</dt>
        <dd className="rtl-text">{(code.bullish ?? []).length > 0 ? (code.bullish ?? []).join("؛ ") : "ثبت نشده"}</dd>
        <dt>سیگنال‌های نزولی</dt>
        <dd className="rtl-text">{(code.bearish ?? []).length > 0 ? (code.bearish ?? []).join("؛ ") : "ثبت نشده"}</dd>
        <dt>حد ضرر اولیه</dt>
        <dd>{money(code.initial_stop, "USDT", { max: 4 })}</dd>
        <dt>هدف اولیه</dt>
        <dd>{money(code.initial_target, "USDT", { max: 4 })}</dd>
      </dl>
      {indicators.length > 0 ? (
        <>
          <h4 className="rtl-text">وضعیت اندیکاتورها در لحظه تصمیم</h4>
          <dl className="kv">
            {indicators.map(([key, value]) => (
              <div key={key} style={{ display: "contents" }}>
                <dt className="num">{key}</dt>
                <dd>{typeof value === "number" ? num(value, { max: 6 }) : text(value ?? null, "—")}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}

      <h3>۲. دفتر کل (آنچه واقعاً رخ داد)</h3>
      <dl className="kv">
        <dt>ورود / خروج</dt>
        <dd>{money(ledger.entry, "USDT", { max: 4 })} → {money(ledger.exit, "USDT", { max: 4 })}</dd>
        <dt>زمان ورود / خروج</dt>
        <dd>{dateTime(ledger.entry_time)} → {dateTime(ledger.exit_time)}</dd>
        <dt>تعداد / کارمزد</dt>
        <dd>{num(ledger.qty, { max: 6 })} · {money(ledger.fees)}</dd>
        <dt>سود/زیان خالص</dt>
        <dd className={`tone-${tone(ledger.pnl)}`}>{signedMoney(ledger.pnl)}</dd>
      </dl>

      <h3>۳. تحلیل پس از معامله (فرض درباره‌ی گذشته)</h3>
      <dl className="kv">
        <dt>R محقق‌شده</dt>
        <dd>{num(analysis.realized_r, { max: 2 })}</dd>
        <dt>ریسک پوزیشن (%)</dt>
        <dd>{plainPct(analysis.risk_pct, { max: 2 })}</dd>
        <dt>نسبت سود/ریسک برنامه</dt>
        <dd>{num(analysis.rr_ratio, { max: 2 })}</dd>
        <dt>بیشترین سود شناور (MFE)</dt>
        <dd>{plainPct(analysis.mfe_pct, { max: 2 })}</dd>
        <dt>بیشترین زیان شناور (MAE)</dt>
        <dd>{plainPct(analysis.mae_pct, { max: 2 })}</dd>
        <dt>سهم سرمایه</dt>
        <dd>{plainPct(analysis.capital_pct, { max: 2 })}</dd>
      </dl>
      {(analysis.lines ?? []).length > 0 ? (
        <ul className="stat-note">
          {analysis.lines.map((line, index) => (
            <li key={`line-${String(index)}`} className="rtl-text">{line}</li>
          ))}
        </ul>
      ) : null}
      {(analysis.uncertainties ?? []).length > 0 ? (
        <div className="notice warn">
          <strong>عدم‌قطعیت‌ها:</strong>
          <ul className="stat-note">
            {analysis.uncertainties.map((line, index) => (
              <li key={`unc-${String(index)}`} className="rtl-text">{line}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="stat-note rtl-text">{text(exp.note)}</p>

      <h3>۴. بازپخش روی کندل‌های واقعی</h3>
      <ReplayChart exp={exp} report={report} />
      <p className="stat-note">
        خط‌چین‌ها: ورود (آبی)، حد ضرر اولیه (قرمز) و هدف اولیه (سبز) — همان سطوحی که در لحظه تصمیم ثبت شدند.
      </p>
    </Modal>
  );
}

export default function TradeAnalysis() {
  const reports = useAsync((signal) => listTradeAnalysisReports(signal), []);
  const [reportId, setReportId] = useState<string>("");
  const activeId = reportId || reports.data?.[0]?.report_id || "";
  const report = useAsync(async (signal) => {
    if (!activeId) return null;
    return getTradeAnalysisReport(activeId, signal);
  }, [activeId]);

  const [dimension, setDimension] = useState<string>("symbol");
  const [symbolFilter, setSymbolFilter] = useState<string>("");
  const [outcomeFilter, setOutcomeFilter] = useState<string>("");
  const [regimeFilter, setRegimeFilter] = useState<string>("");
  const [exitFilter, setExitFilter] = useState<string>("");
  const [selected, setSelected] = useState<number | null>(null);

  const [building, setBuilding] = useState<boolean>(false);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [started, setStarted] = useState<{ runId: string; hypothesisId: string } | null>(null);

  const data = report.data;
  const explanations = data?.explanations ?? [];

  const regimeOptions = useMemo(() => {
    const set = new Set<string>();
    for (const exp of explanations) {
      const key = String(exp.code_recorded.regime ?? "");
      if (key) set.add(key);
    }
    return [{ value: "", label: "همه" }, ...[...set].sort().map((key) => ({ value: key, label: REGIME_FA[key] ?? key }))];
  }, [explanations]);

  const exitOptions = useMemo(() => {
    const set = new Set<string>();
    for (const exp of explanations) {
      const key = String(exp.code_recorded.exit_reason ?? "");
      if (key) set.add(key);
    }
    return [{ value: "", label: "همه" }, ...[...set].sort().map((key) => ({ value: key, label: key }))];
  }, [explanations]);

  const filtered = useMemo(() => explanations.filter((exp) => {
    if (symbolFilter && exp.ledger.symbol !== symbolFilter) return false;
    if (regimeFilter && exp.code_recorded.regime !== regimeFilter) return false;
    if (exitFilter && exp.code_recorded.exit_reason !== exitFilter) return false;
    if (outcomeFilter === "win" && !(Number(exp.ledger.pnl) > 0)) return false;
    if (outcomeFilter === "loss" && !(Number(exp.ledger.pnl) <= 0)) return false;
    return true;
  }), [explanations, symbolFilter, regimeFilter, exitFilter, outcomeFilter]);

  const columns: Column<TradeExplanation>[] = [
    { key: "symbol", header: "نماد", render: (e) => <span className="num">{text(e.ledger.symbol)}</span>, sortValue: (e) => e.ledger.symbol },
    { key: "entry_time", header: "زمان ورود", sortValue: (e) => e.ledger.entry_time, render: (e) => dateTime(e.ledger.entry_time) },
    { key: "exit_time", header: "زمان خروج", sortValue: (e) => e.ledger.exit_time, render: (e) => dateTime(e.ledger.exit_time) },
    { key: "entry", header: "ورود", numeric: true, sortValue: (e) => e.ledger.entry, render: (e) => money(e.ledger.entry, "USDT", { max: 4 }) },
    { key: "exit", header: "خروج", numeric: true, sortValue: (e) => e.ledger.exit, render: (e) => money(e.ledger.exit, "USDT", { max: 4 }) },
    { key: "pnl", header: "خالص", numeric: true, sortValue: (e) => e.ledger.pnl, render: (e) => <span className={`tone-${tone(e.ledger.pnl)}`}>{signedMoney(e.ledger.pnl)}</span> },
    { key: "r", header: "R محقق", numeric: true, sortValue: (e) => e.analysis.realized_r, render: (e) => <span className={`tone-${tone(e.analysis.realized_r)}`}>{num(e.analysis.realized_r, { max: 2 })}</span> },
    { key: "risk", header: "ریسک %", numeric: true, sortValue: (e) => e.analysis.risk_pct, render: (e) => plainPct(e.analysis.risk_pct, { max: 2 }) },
    { key: "mfe", header: "MFE %", numeric: true, sortValue: (e) => e.analysis.mfe_pct, render: (e) => plainPct(e.analysis.mfe_pct, { max: 1 }) },
    { key: "mae", header: "MAE %", numeric: true, sortValue: (e) => e.analysis.mae_pct, render: (e) => plainPct(e.analysis.mae_pct, { max: 1 }) },
    { key: "regime", header: "رژیم", render: (e) => <span className="rtl-text">{text(e.code_recorded.regime ? REGIME_FA[e.code_recorded.regime] ?? e.code_recorded.regime : null)}</span>, sortValue: (e) => e.code_recorded.regime },
    { key: "exit_reason", header: "دلیل خروج", render: (e) => <span className="num">{text(e.code_recorded.exit_reason)}</span>, sortValue: (e) => e.code_recorded.exit_reason },
    { key: "confidence", header: "اطمینان", numeric: true, sortValue: (e) => e.code_recorded.confidence, render: (e) => num(e.code_recorded.confidence, { max: 2 }) },
  ];

  async function build() {
    setBuilding(true);
    setBuildError(null);
    try {
      const payload = await buildTradeAnalysisReport({});
      await reports.reload();
      setReportId(payload.report_id);
    } catch (error) {
      setBuildError(describeError(error));
    } finally {
      setBuilding(false);
    }
  }

  async function startExperiment(h: TradeHypothesis) {
    const label = data?.run_labels.find((r) => r.dataset_id) ?? null;
    if (!label?.dataset_id || !label.symbol || !label.timeframe) {
      setStartError("اجرای مبنای گزارش dataset_id/نماد ندارد؛ آزمایش شروع نمی‌شود.");
      return;
    }
    setStartingId(h.id);
    setStartError(null);
    setStarted(null);
    try {
      const state = await startOptimizer({
        symbol: label.symbol,
        timeframe: label.timeframe,
        dataset_id: label.dataset_id,
        max_experiments: 1,
        folds: 3,
        warmup: 80,
        assumed_spread_pct: 0.05,
        hypotheses: [{ id: h.id, question: h.question, grid: h.grid }],
      });
      setStarted({ runId: state.run_id, hypothesisId: h.id });
    } catch (error) {
      setStartError(describeError(error));
    } finally {
      setStartingId(null);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>تحلیل معاملات</h1>
          <p className="mute">
            گزارش‌های ساخته‌شده فقط از معاملات ثبت‌شده‌ی اجراهای واقعی بک‌تست — بدون عدد نمونه. دلیل ورود کد از تحلیل
            پس از واقعه جدا می‌ماند و گروه‌های زیان‌ده هرگز پنهان نمی‌شوند.
          </p>
        </div>
        <div className="btn-row">
          <Button size="sm" onClick={build} disabled={building} aria-label="ساخت گزارش تحلیل جدید">
            <RefreshCw size={14} aria-hidden="true" />
            {building ? "در حال ساخت…" : "ساخت گزارش جدید"}
          </Button>
        </div>
      </div>

      {buildError ? <div className="notice danger" role="alert">{buildError}</div> : null}

      {reports.data && reports.data.length === 0 && !reports.loading ? (
        <section className="card">
          <div className="state" role="status">
            <h3>هنوز گزارشی ساخته نشده است</h3>
            <p className="mute">
              اول از صفحه‌ی «بک‌تست» یک اجرای واقعی ثبت کنید، سپس «ساخت گزارش جدید» گزارش را از معاملات همان اجرا می‌سازد.
            </p>
          </div>
        </section>
      ) : null}

      {reports.data && reports.data.length > 0 ? (
        <Filters
          fields={[
            {
              key: "report", label: "گزارش", value: activeId, onChange: setReportId, type: "select",
              options: (reports.data ?? []).map((r) => ({
                value: r.report_id,
                label: `${r.report_id} · ${String(r.generated_at ?? "").slice(0, 16).replace("T", " ")} · ${num(r.trade_count, { max: 0 })} معامله`,
              })),
              hint: "گزارش‌های ذخیره‌شده روی دیسک",
            },
          ]}
        />
      ) : null}

      {report.error ? <div className="notice danger" role="alert">{report.error}</div> : null}

      {data ? (
        <>
          <p className="notice">{data.note}</p>

          <section aria-label="جمع‌بندی گزارش">
            <div className="grid stats">
              <Stat label="تعداد معاملات گزارش" value={num(data.trade_count, { max: 0 })} source="trade-analysis" endpoint="/trade-analysis" window={data.generated_at} />
              <Stat label="نمادها" value={num(data.symbols.length, { max: 0 })} source="trade-analysis" endpoint="/trade-analysis" note={data.symbols.join(" · ")} />
              <Stat label="بیشترین موقعیت هم‌زمان" value={num(data.overlap.max_concurrent, { max: 0 })} source="trade-analysis" endpoint="/trade-analysis" note={data.overlap.note} />
              <Stat label="فرضیه‌های آزمایش‌نشده" value={num(data.discovery.hypotheses.length, { max: 0 })} source="trade-analysis" endpoint="/trade-analysis" note={data.discovery.note} />
            </div>
          </section>

          <section className="card" aria-label="مقایسه‌ی چندنمادی و گروهی">
            <header>
              <h2>تحلیل گروهی — گروه‌های زیان‌ده هم نمایش داده می‌شوند</h2>
              <Badge kind="simulated" label="از معاملات بک‌تست" />
            </header>
            <Filters
              fields={[{
                key: "dimension", label: "بُعد گروه‌بندی", value: dimension, onChange: setDimension, type: "select",
                options: GROUP_DIMS.map((d) => ({ value: d.key, label: d.label })),
              }]}
            />
            <GroupTable groups={data.groups[dimension] ?? []} />
          </section>

          <section className="card" aria-label="فهرست معاملات">
            <header>
              <h2>معاملات با شواهد کامل</h2>
              <Badge kind="simulated" label="GET /trade-analysis/{id}" />
            </header>
            <Filters
              fields={[
                { key: "symbol", label: "نماد", value: symbolFilter, onChange: setSymbolFilter, type: "select", options: [{ value: "", label: "همه" }, ...(data.symbols ?? []).map((s) => ({ value: s, label: s }))] },
                { key: "outcome", label: "نتیجه", value: outcomeFilter, onChange: setOutcomeFilter, type: "select", options: [{ value: "", label: "همه" }, { value: "win", label: "سود" }, { value: "loss", label: "زیان" }] },
                { key: "regime", label: "رژیم", value: regimeFilter, onChange: setRegimeFilter, type: "select", options: regimeOptions },
                { key: "exit", label: "دلیل خروج", value: exitFilter, onChange: setExitFilter, type: "select", options: exitOptions },
              ]}
              onReset={() => {
                setSymbolFilter("");
                setOutcomeFilter("");
                setRegimeFilter("");
                setExitFilter("");
              }}
              resultLabel={`نمایش ${num(filtered.length, { max: 0 })} از ${num(explanations.length, { max: 0 })} معامله`}
            />
            <Table
              rows={filtered}
              getRowId={(e, index) => `${e.ledger.run_id ?? "run"}-${e.ledger.entry_time ?? "t"}-${index}`}
              caption="معاملات اجراهای واقعی همراه با شواهد لحظه تصمیم"
              loading={report.loading}
              error={report.error}
              onRetry={report.reload}
              emptyLabel="معامله‌ای با این فیلترها نیست"
              emptyHint="صفر معامله یعنی هیچ؛ عدد نمونه‌ای نمایش داده نمی‌شود."
              onRowClick={(e) => setSelected(explanations.findIndex((item) => item === e))}
              defaultSort={{ key: "exit_time", dir: "desc" }}
              columns={columns}
            />
            <p className="stat-note">برای تحلیل علت و بازپخش روی کندل، یک ردیف را باز کنید.</p>
          </section>

          <section className="card" aria-label="همپوشانی موقعیت‌ها">
            <header>
              <h2>ریسک هم‌زمانی نمادها</h2>
              <Badge kind="simulated" label="از بازه‌های معامله" />
            </header>
            <dl className="kv">
              <dt>بیشترین موقعیت هم‌زمان</dt>
              <dd>{num(data.overlap.max_concurrent, { max: 0 })}</dd>
              <dt>نمادهای درگیر</dt>
              <dd className="num">{(data.overlap.symbols ?? []).join(" · ") || NO_DATA}</dd>
            </dl>
            {(data.overlap.pairs ?? []).length > 0 ? (
              <Table
                rows={data.overlap.pairs}
                getRowId={(p) => p.symbols}
                caption="جفت‌نمادهای همپوشان"
                columns={[
                  { key: "symbols", header: "جفت", render: (p) => <span className="num">{p.symbols}</span> },
                  { key: "overlaps", header: "تعداد همپوشانی", numeric: true, render: (p) => num(p.overlaps, { max: 0 }) },
                  { key: "hours", header: "ساعت همپوشانی", numeric: true, render: (p) => num(p.overlap_hours, { max: 1 }) },
                ]}
                emptyLabel="بدون همپوشانی"
              />
            ) : (
              <p className="stat-note rtl-text">{data.overlap.note}</p>
            )}
          </section>

          <section className="card" aria-label="فرضیه‌های آزمایش‌پذیر">
            <header>
              <h2>فرضیه‌های کشف‌شده (حداکثر ۳ در هر چرخه)</h2>
              <Badge kind="simulated" label="کشف پس از واقعه" />
            </header>
            {startError ? <div className="notice danger" role="alert">{startError}</div> : null}
            {started ? (
              <p className="notice">
                <FlaskConical size={14} aria-hidden="true" /> حلقه‌ی بهینه‌سازی <strong className="num">{started.runId}</strong> با فرضیه{" "}
                <strong className="num">{started.hypothesisId}</strong> شروع شد. <Link to="/optimizer">پیگیری در بهینه‌سازی خودکار</Link>
              </p>
            ) : null}
            {data.discovery.hypotheses.length === 0 ? (
              <p className="stat-note rtl-text">{data.discovery.note}</p>
            ) : (
              <div className="grid two">
                {data.discovery.hypotheses.map((h) => (
                  <HypothesisCard key={h.id} h={h} onStart={startExperiment} starting={startingId === h.id} disabled={startingId !== null} />
                ))}
              </div>
            )}
          </section>

          <ProvenanceNote provenance={null} fallbackKind="simulated" extra={`گزارش ${data.report_id} · اجراها: ${(data.run_ids ?? []).join(", ")}`} />
        </>
      ) : null}

      <TradeModal
        exp={selected !== null && explanations[selected] ? explanations[selected] : null}
        report={data as TradeAnalysisReport}
        onClose={() => setSelected(null)}
      />

      <p className="notice warn">
        همه‌ی اعداد این صفحه از گزارش‌های ذخیره‌شده‌ی معاملات بک‌تست می‌آیند. فرضیه‌ها پس از واقعه ساخته شده‌اند و تا
        تأیید بیرون از نمونه هیچ ادعای سودآوری معتبر نیست.
      </p>
    </>
  );
}
