import { useMemo, useState } from "react";
import {
  getDecisions,
  getPerformance,
  getTrades,
  useAsync,
  type Decision,
  type TradeRow,
} from "../api/client";
import Badge, { ToneBadge } from "../components/Badge";
import Filters from "../components/Filters";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import { NO_DATA, dateTime, money, num, plainPct, signedMoney, text, tone } from "../format";

/** The ledger has no slippage/status columns — say so explicitly instead of computing a substitute. */
const NOT_RECORDED = "ثبت نشده";

const OUTCOME_OPTIONS = [
  { value: "", label: "همه" },
  { value: "win", label: "برنده (net > ۰)" },
  { value: "loss", label: "بازنده (net ≤ ۰)" },
];

const LIMIT_OPTIONS = [
  { value: "100", label: "۱۰۰ ردیف" },
  { value: "200", label: "۲۰۰ ردیف" },
  { value: "500", label: "۵۰۰ ردیف" },
  { value: "1000", label: "۱۰۰۰ ردیف" },
];

function toInt(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

/**
 * Local, clearly-labelled comparison between the original prediction (decision
 * action + confidence) and the realised net P&L of the ledger row. Only offered
 * when both sides exist in the API; never guessed.
 */
function predictionMatch(
  decision: Decision | null,
  row: TradeRow | null,
): { label: string; badge: "pos" | "neg" | "neutral" | "warn" } | null {
  if (!decision || !row) return null;
  const action = (decision.action ?? "").toUpperCase();
  const net = row.net;
  if (typeof net !== "number") return null;
  if (action === "BUY") {
    return net > 0
      ? { label: "جهت پیش‌بینی (BUY) با نتیجه هم‌خوان بود: خالص مثبت", badge: "pos" }
      : { label: "جهت پیش‌بینی (BUY) با نتیجه ناهم‌خوان بود: خالص ≤ ۰", badge: "neg" };
  }
  if (action === "SELL") {
    return { label: "اقدام خروج/فروش ثبت شده؛ نتیجه خالص: " + signedMoney(net), badge: "warn" };
  }
  if (action === "HOLD" || action === "CANCEL") {
    return { label: `اقدام تصمیم ${decision.action} بود؛ این ردیف ledger از تصمیم دیگری آمده یا decision_id منطبق نیست.`, badge: "neutral" };
  }
  return null;
}

export default function Trades() {
  const [symbol, setSymbol] = useState<string>("");
  const [outcome, setOutcome] = useState<string>("");
  const [limit, setLimit] = useState<string>("200");
  const [offset, setOffset] = useState<string>("0");
  const [selected, setSelected] = useState<TradeRow | null>(null);

  const limitNum = toInt(limit, 200);
  const offsetNum = toInt(offset, 0);

  const page = useAsync(
    (signal) => getTrades({ mode: "PAPER", symbol: symbol || null, outcome: (outcome === "win" || outcome === "loss" ? outcome : null), limit: limitNum, offset: offsetNum }, signal),
    [symbol, outcome, limitNum, offsetNum],
  );

  /** Aggregate figures for the same filter come straight from /performance — never summed in the UI. */
  const summary = useAsync(
    (signal) => getPerformance({ symbol: symbol || null, mode: "PAPER" }, signal),
    [symbol],
  );

  /** Decisions are fetched only when a row is opened, to trace the trade back to its prediction. */
  const related = useAsync(async (signal) => {
    if (!selected) return [] as Decision[];
    return getDecisions({ symbol: selected.symbol }, signal);
  }, [selected?.symbol ?? "", selected?.decision_id ?? ""]);

  const rows = page.data?.rows ?? [];

  const decision = useMemo(() => {
    if (!selected?.decision_id) return null;
    return (related.data ?? []).find((item) => String(item.id) === String(selected.decision_id)) ?? null;
  }, [related.data, selected]);

  const match = predictionMatch(decision, selected);

  const metrics = summary.data?.metrics ?? null;
  const windowLabel = summary.data?.window ?? page.data?.provenance?.window ?? null;
  const provenance = page.data?.provenance ?? null;

  const columns: Column<TradeRow>[] = [
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "timeframe", header: "تایم‌فریم", render: (row) => <span className="num">{text(row.timeframe, NOT_RECORDED)}</span>, sortValue: (row) => row.timeframe },
    { key: "strategy", header: "نسخه استراتژی", render: (row) => <span className="num">{text(row.strategy_label, NOT_RECORDED)}</span>, sortValue: (row) => row.strategy_label },
    { key: "entry_ts", header: "زمان ورود", sortValue: (row) => row.entry_ts, render: (row) => dateTime(row.entry_ts) },
    { key: "entry", header: "قیمت ورود", numeric: true, sortValue: (row) => row.entry, render: (row) => money(row.entry, "USDT", { max: 4 }) },
    { key: "exit_ts", header: "زمان خروج", sortValue: (row) => row.exit_ts, render: (row) => dateTime(row.exit_ts) },
    { key: "exit", header: "قیمت خروج", numeric: true, sortValue: (row) => row.exit, render: (row) => money(row.exit, "USDT", { max: 4 }) },
    { key: "qty", header: "تعداد", numeric: true, sortValue: (row) => row.qty, render: (row) => num(row.qty, { max: 6 }) },
    { key: "stop", header: "حد ضرر", numeric: true, sortValue: (row) => row.stop, render: (row) => money(row.stop, "USDT", { max: 4 }) },
    { key: "target", header: "هدف", numeric: true, sortValue: (row) => row.target, render: (row) => money(row.target, "USDT", { max: 4 }) },
    { key: "gross", header: "ناخالص", numeric: true, sortValue: (row) => row.gross, render: (row) => <span className={`tone-${tone(row.gross)}`}>{signedMoney(row.gross)}</span> },
    { key: "fees", header: "کارمزد", numeric: true, sortValue: (row) => row.fees, render: (row) => money(row.fees) },
    { key: "net", header: "خالص پس از کارمزد", numeric: true, sortValue: (row) => row.net, render: (row) => <span className={`tone-${tone(row.net)}`}>{signedMoney(row.net)}</span> },
    { key: "reason", header: "دلیل خروج", render: (row) => <span className="rtl-text">{text(row.reason, NOT_RECORDED)}</span>, sortValue: (row) => row.reason },
    { key: "status", header: "وضعیت", render: () => <ToneBadge tone="neutral" title="ردیف‌های /trades به‌تعریف معاملات بسته‌شده‌ی ثبت‌شده در ledger هستند">بسته‌شده (ledger)</ToneBadge> },
    { key: "decision", header: "تصمیم", render: (row) => <span className="num">{text(row.decision_id, NOT_RECORDED)}</span> },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>تاریخچه‌ی معاملات</h1>
          <p className="mute">
            فقط ردیف‌های ثبت‌شده‌ی ledger کاغذی از <code>/trades</code>؛ بدون عدد نمونه. برای دیدن جزئیات ردیابی هر
            معامله (دلیل ورود، ریسک‌گیت و هم‌سنجی با پیش‌بینی) روی هر ردیف کلیک کنید.
          </p>
        </div>
        <div className="btn-row">
          <Badge kind="paper" label="همه‌ی ردیف‌ها کاغذی (PAPER)" />
        </div>
      </div>

      <Filters
        fields={[
          { key: "symbol", label: "نماد", value: symbol, onChange: setSymbol, placeholder: "مثلاً BTC_USDT", hint: "خالی = همه‌ی نمادها" },
          { key: "outcome", label: "نتیجه", value: outcome, onChange: setOutcome, type: "select", options: OUTCOME_OPTIONS, hint: "مبنا: خالص پس از کارمزد" },
          { key: "limit", label: "سقف ردیف‌ها", value: limit, onChange: setLimit, type: "select", options: LIMIT_OPTIONS },
          { key: "offset", label: "شروع از (offset)", value: offset, onChange: setOffset, type: "number", hint: "برگه‌بندی سمت سرور" },
        ]}
        onReset={() => {
          setSymbol("");
          setOutcome("");
          setLimit("200");
          setOffset("0");
        }}
        resultLabel={`نمایش ${num(rows.length, { max: 0 })} ردیف از offset ${text(offset)} با فیلتر «${symbol ? symbol : "همه‌ی نمادها"}»${outcome ? ` / ${outcome === "win" ? "برنده" : "بازنده"}` : ""}`}
      />

      <section aria-label="جمع‌بندی ledger">
        <div className="grid stats">
          <Stat
            label="کل معاملات ledger (کاغذی)"
            value={num(metrics?.trades ?? null, { max: 0 })}
            window={windowLabel}
            source={summary.data?.provenance?.source ?? "engine"}
            endpoint="/performance"
            tooltip="عدد مستقیم از موتور ledger است، نه شمار ردیف‌های صفحه‌ی جاری؛ اگر ledger خالی باشد همین‌جا صفر واقعی می‌بینید."
          />
          <Stat
            label="خالص پس از کارمزد"
            value={signedMoney(metrics?.net_pnl ?? null)}
            tone={tone(metrics?.net_pnl ?? null)}
            window={windowLabel}
            source={summary.data?.provenance?.source ?? "engine"}
            endpoint="/performance"
          />
          <Stat label="برد / باخت" value={metrics ? `${num(metrics.wins ?? null, { max: 0 })} / ${num(metrics.losses ?? null, { max: 0 })}` : null} window={windowLabel} source="engine" endpoint="/performance" note="مبنای باخت: خالص پس از کارمزد ≤ ۰." />
          <Stat label="نرخ برد" value={plainPct(metrics?.win_rate_pct ?? null, { max: 1 })} window={windowLabel} source="engine" endpoint="/performance" />
          <Stat label="کارمزد کل" value={money(metrics?.fees ?? metrics?.fees_paid ?? null)} window={windowLabel} source="engine" endpoint="/performance" />
          <Stat
            label="اسلیپیج تحقق‌یافته"
            value={NOT_RECORDED}
            window={windowLabel}
            source="ledger"
            endpoint="/trades"
            note="قرارداد ledger ستون اسلیپیج جدا ندارد؛ سقف اسلیپیج در ریسک‌گیت اعمال می‌شود اما پرشدن realized ثبت نمی‌شود."
          />
        </div>
      </section>

      <section className="card" aria-label="جدول معاملات">
        <header>
          <h2>ردیف‌های معاملات کاغذی</h2>
          <Badge kind="paper" label="GET /trades?mode=PAPER" />
        </header>
        <Table
          rows={rows}
          getRowId={(row, index) => `${String(row.id ?? "row")}-${String(index)}-${row.symbol}-${String(row.exit_ts ?? "")}`}
          caption="ledger معاملات در حالت PAPER"
          loading={page.loading}
          error={page.error}
          onRetry={page.reload}
          emptyLabel={NO_DATA}
          emptyHint="با این فیلترها هیچ معامله‌ای در ledger ثبت نشده است — صفر معامله، صفر سود/زیان؛ عدد نمونه‌ای نمایش داده نمی‌شود."
          onRowClick={setSelected}
          defaultSort={{ key: "exit_ts", dir: "desc" }}
          columns={columns}
        />
        <ProvenanceNote provenance={provenance} fallbackKind="paper" extra="ردیف‌های /trades — حالت PAPER" />
      </section>

      <Modal
        open={selected !== null}
        title={selected ? `معامله‌ی کاغذی ${selected.symbol}` : "معامله"}
        description="ردیابی کامل یک ردیف ledger: ورود/خروج، ریسک ثبت‌شده، هزینه‌ها و پیوند به تصمیم."
        onClose={() => setSelected(null)}
        width={720}
      >
        {selected ? (
          <>
            <h3>مشخصات معامله</h3>
            <dl className="kv">
              <dt>شناسه ردیف</dt>
              <dd>{selected.id === null || selected.id === undefined ? NOT_RECORDED : String(selected.id)}</dd>
              <dt>حالت</dt>
              <dd className="rtl-text">{text(selected.mode, NOT_RECORDED)}</dd>
              <dt>نماد / تایم‌فریم</dt>
              <dd className="rtl-text">{`${selected.symbol} · ${text(selected.timeframe, NOT_RECORDED)}`}</dd>
              <dt>نسخه‌ی استراتژی</dt>
              <dd>{text(selected.strategy_label, NOT_RECORDED)}</dd>
              <dt>زمان ورود</dt>
              <dd>{dateTime(selected.entry_ts)}</dd>
              <dt>قیمت ورود (USDT)</dt>
              <dd>{money(selected.entry, "USDT", { max: 4 })}</dd>
              <dt>زمان خروج</dt>
              <dd>{dateTime(selected.exit_ts)}</dd>
              <dt>قیمت خروج (USDT)</dt>
              <dd>{money(selected.exit, "USDT", { max: 4 })}</dd>
              <dt>تعداد</dt>
              <dd>{num(selected.qty, { max: 6 })}</dd>
              <dt>حد ضرر ثبت‌شده</dt>
              <dd>{money(selected.stop, "USDT", { max: 4 })}</dd>
              <dt>هدف ثبت‌شده</dt>
              <dd>{money(selected.target, "USDT", { max: 4 })}</dd>
              <dt>سود/زیان ناخالص</dt>
              <dd className={`tone-${tone(selected.gross)}`}>{signedMoney(selected.gross)}</dd>
              <dt>کارمزد</dt>
              <dd>{money(selected.fees)}</dd>
              <dt>خالص پس از کارمزد</dt>
              <dd className={`tone-${tone(selected.net)}`}>{signedMoney(selected.net)}</dd>
              <dt>اسلیپیج تحقق‌یافته</dt>
              <dd className="rtl-text">{NOT_RECORDED} — ledger این ستون را ندارد</dd>
              <dt>دلیل خروج</dt>
              <dd className="rtl-text">{text(selected.reason, NOT_RECORDED)}</dd>
              <dt>دلیل ورود</dt>
              <dd className="rtl-text">{decision ? (decision.rationale.length > 0 ? decision.rationale.join("؛ ") : "دلالی برای این تصمیم ثبت نشده است") : NOT_RECORDED}</dd>
              <dt>شناسه تصمیم</dt>
              <dd>{text(selected.decision_id, NOT_RECORDED)}</dd>
            </dl>

            <h3>پیش‌بینی اولیه (از /decisions)</h3>
            {related.loading ? (
              <p className="mute">خواندن تصمیم مرتبط…</p>
            ) : related.error ? (
              <div className="notice danger" role="alert">{related.error}</div>
            ) : decision ? (
              <>
                <dl className="kv">
                  <dt>زمان تصمیم</dt>
                  <dd>{dateTime(decision.ts)}</dd>
                  <dt>اقدام پیشنهادی</dt>
                  <dd>{text(decision.action, NOT_RECORDED)}</dd>
                  <dt>اطمینان مدل</dt>
                  <dd>{decision.confidence === null || decision.confidence === undefined ? NOT_RECORDED : num(decision.confidence, { max: 3 })}</dd>
                  <dt>حکم ریسک‌گیت</dt>
                  <dd className="rtl-text">{decision.approved ? "تأییدشده" : "ردشده"}</dd>
                  <dt>دلایل ریسک‌گیت</dt>
                  <dd className="rtl-text">{(decision.risk.reasons ?? []).length > 0 ? (decision.risk.reasons ?? []).join("؛ ") : NOT_RECORDED}</dd>
                  <dt>اندازه/ریسک محاسبه‌شده</dt>
                  <dd>{decision.risk.qty != null && decision.risk.notional != null ? `${num(decision.risk.qty, { max: 6 })} × ${money(decision.risk.notional)}` : NOT_RECORDED}</dd>
                  <dt>ریسک بر حسب R</dt>
                  <dd>{decision.risk.reward_risk_ratio != null ? num(decision.risk.reward_risk_ratio, { max: 2 }) : NOT_RECORDED}</dd>
                </dl>
                {match ? (
                  <p className="notice">
                    <ToneBadge tone={match.badge}>هم‌سنجی محلی</ToneBadge> {match.label}
                  </p>
                ) : (
                  <p className="stat-note">موتور برچسب «هم‌خوانی پیش‌بینی با نتیجه» ثبت نکرده است؛ مقایسه‌ی خودکار فقط برای BUY/SELL انجام می‌شود.</p>
                )}
              </>
            ) : (
              <p className="stat-note">
                {selected.decision_id
                  ? `تصمیمی با شناسه ${selected.decision_id} در /decisions (برای نماد ${selected.symbol}) یافت نشد؛ مقادیر پیش‌بینی «${NOT_RECORDED}» می‌مانند.`
                  : `این ردیف ledger شناسه‌ی تصمیم ندارد؛ پیوند به پیش‌بینی ${NOT_RECORDED}.`}
              </p>
            )}

            <h3>زمینه‌ی ثبت‌شده (context)</h3>
            {selected.context && Object.keys(selected.context).length > 0 ? (
              <dl className="kv">
                {Object.entries(selected.context).map(([key, value]) => (
                  <div key={key} style={{ display: "contents" }}>
                    <dt>{key}</dt>
                    <dd className={typeof value === "string" ? "rtl-text" : undefined}>
                      {typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? NOT_RECORDED)}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="stat-note">context این ردیف خالی است — چیزی ثبت نشده است.</p>
            )}

            <ProvenanceNote provenance={provenance} fallbackKind="paper" extra="/trades + /decisions" />
          </>
        ) : null}
      </Modal>

      <p className="notice warn">
        این جدول سابقه‌ی ثبت‌شده‌ی چرخه‌های کاغذی است، نه نتیجه‌ی بازار زنده و نه پیش‌بینی. نرخ برد و خالص این ledger
        تضمین‌کننده‌ی نتیجه‌ی آینده نیست.
      </p>
    </>
  );
}
