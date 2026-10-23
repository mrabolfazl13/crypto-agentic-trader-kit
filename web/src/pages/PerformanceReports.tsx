import { useState } from "react";
import type { ReactNode } from "react";
import { getEquityCurve, getHealth, getPerformance, getReport, useAsync, type Metrics } from "../api/client";
import Badge, { ModeBadge } from "../components/Badge";
import ProvenanceNote from "../components/ProvenanceNote";
import Filters from "../components/Filters";
import StateBlocks, { EmptyBlock, LoadingBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import DrawdownChart from "../charts/DrawdownChart";
import LineChart from "../charts/LineChart";
import { NO_DATA, dateTime, num, pct, signedMoney, text, todayIso } from "../format";

/** Minimal Persian/Markdown renderer: headings, bullets, bold, inline code. */
function renderInline(source: string, keyPrefix: string) {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let cursor = 0;
  let index = 0;
  let match = pattern.exec(source);
  while (match) {
    if (match.index > cursor) nodes.push(source.slice(cursor, match.index));
    const token = match[0];
    if (token.startsWith("**")) nodes.push(<strong key={`${keyPrefix}-b${String(index)}`}>{token.slice(2, -2)}</strong>);
    else nodes.push(<code key={`${keyPrefix}-c${String(index)}`}>{token.slice(1, -1)}</code>);
    cursor = match.index + token.length;
    index += 1;
    match = pattern.exec(source);
  }
  if (cursor < source.length) nodes.push(source.slice(cursor));
  return nodes;
}

function Markdown({ source }: { source: string }) {
  const lines = source.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  lines.forEach((raw, lineIndex) => {
    const line = raw.trimEnd();
    const pushList = () => {
      if (list.length === 0) return;
      blocks.push(
        <ul key={`ul-${String(lineIndex)}`}>
          {list.map((item, itemIndex) => (
            <li key={`li-${String(itemIndex)}`}>{renderInline(item, `li-${String(lineIndex)}-${String(itemIndex)}`)}</li>
          ))}
        </ul>,
      );
      list = [];
    };
    if (line.startsWith("- ")) {
      list.push(line.slice(2));
      return;
    }
    pushList();
    if (line === "") return;
    if (line.startsWith("## ")) blocks.push(<h2 key={`h2-${String(lineIndex)}`}>{renderInline(line.slice(3), `h2-${String(lineIndex)}`)}</h2>);
    else if (line.startsWith("# ")) blocks.push(<h1 key={`h1-${String(lineIndex)}`}>{renderInline(line.slice(2), `h1-${String(lineIndex)}`)}</h1>);
    else if (/^(این گزارش|بازده گذشته)/.test(line)) blocks.push(<p className="disclaimer" key={`d-${String(lineIndex)}`}>{renderInline(line, `d-${String(lineIndex)}`)}</p>);
    else blocks.push(<p key={`p-${String(lineIndex)}`}>{renderInline(line, `p-${String(lineIndex)}`)}</p>);
  });
  const closeList = () => {
    if (list.length === 0) return;
    blocks.push(
      <ul key="ul-tail">
        {list.map((item, itemIndex) => (
          <li key={`tail-${String(itemIndex)}`}>{renderInline(item, `tail-${String(itemIndex)}`)}</li>
        ))}
      </ul>,
    );
  };
  closeList();
  return <div className="prose">{blocks}</div>;
}

const METRIC_ROWS: { key: keyof Metrics; label: string; kind: "count" | "money" | "pct" | "plain" }[] = [
  { key: "trades", label: "تعداد کل معاملات", kind: "count" },
  { key: "win_rate_pct", label: "نرخ برد", kind: "pct" },
  { key: "profit_factor", label: "ضریب سود", kind: "plain" },
  { key: "avg_win", label: "میانگین سود هر برد", kind: "money" },
  { key: "avg_loss", label: "میانگین زیان هر باخت", kind: "money" },
  { key: "net_pnl", label: "خالص پس از کارمزد", kind: "money" },
  { key: "net_return_pct", label: "بازده خالص", kind: "pct" },
  { key: "fees_paid", label: "کارمزد کل", kind: "money" },
  { key: "fees", label: "کارمزد (ledger)", kind: "money" },
  { key: "max_drawdown_pct", label: "بیشترین افت سرمایه", kind: "pct" },
  { key: "sharpe_annualised_crude", label: "شارپ (تحویلی و خام)", kind: "plain" },
  { key: "sortino_annualised_crude", label: "سورتینو (تحویلی و خام)", kind: "plain" },
];

interface MetricRow {
  label: string;
  value: string;
  signed: boolean;
}

function toRows(metrics: Metrics | null | undefined): MetricRow[] {
  if (!metrics) return [];
  const rows: MetricRow[] = [];
  for (const definition of METRIC_ROWS) {
    const raw = metrics[definition.key];
    if (typeof raw !== "number" && raw !== null && raw !== undefined) continue;
    const value = typeof raw === "number" ? raw : null;
    if (value === null && (definition.key === "fees" || definition.key === "net_return_pct" || definition.key === "sortino_annualised_crude")) continue;
    const rendered =
      value === null
        ? NO_DATA
        : definition.kind === "money"
          ? signedMoney(value)
          : definition.kind === "pct"
            ? pct(value, { max: 2 })
            : definition.kind === "count"
              ? num(value, { max: 0 })
              : num(value, { max: 3 });
    rows.push({ label: definition.label, value: rendered, signed: typeof value === "number" && value !== 0 });
  }
  return rows;
}

export default function PerformanceReports() {
  const [day, setDay] = useState<string>(() => todayIso());
  const report = useAsync((signal) => getReport(day, signal), [day]);
  const fallback = useAsync((signal) => getPerformance({ mode: "PAPER" }, signal), []);
  const equity = useAsync((signal) => getEquityCurve("PAPER", signal), []);
  const health = useAsync((signal) => getHealth(signal), []);

  const metrics: Metrics | null = report.data?.metrics ?? fallback.data?.metrics ?? null;
  const rows = toRows(metrics);
  const provenance = report.data?.provenance ?? fallback.data?.provenance ?? null;
  const points = equity.data?.points ?? [];
  const series = [
    { id: "equity", label: "Equity کاغذی", points: points.map((p) => [p[0], p[1]] as [string | number, number | null]), color: "var(--info)" },
  ];
  if ((equity.data?.buy_hold?.length ?? 0) > 1) {
    series.push({
      id: "buy_hold",
      label: "خرید و نگه‌داشتن (ملاک)",
      points: (equity.data?.buy_hold ?? []).map((p) => [p[0], p[1]] as [string | number, number | null]),
      color: "var(--warn)",
    });
  }

  const columns: Column<MetricRow>[] = [
    { key: "label", header: "شاخص", render: (row) => row.label, sortValue: (row) => row.label },
    {
      key: "value",
      header: "مقدار",
      numeric: true,
      render: (row) => <span className={row.signed ? undefined : "mute"}>{row.value}</span>,
    },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>گزارش عملکرد</h1>
          <p className="mute">گزارش روزانه‌ی فارسی از <code>/reports/&#123;day&#125;</code> — متن ثبت‌شده‌ی سیستم، نه بازنویسی UI.</p>
        </div>
        <ModeBadge mode={health.data?.mode ?? null} />
      </div>

      <Filters
        fields={[
          { key: "day", label: "روز (UTC)", type: "date", value: day, onChange: (value) => setDay(value || todayIso()), hint: "قالب yyyy-mm-dd" },
        ]}
        actions={
          <button type="button" className="btn small" onClick={report.reload} aria-label="خواندن دوباره‌ی گزارش">
            خواندن دوباره
          </button>
        }
        resultLabel={`نمایش: ${text(day)}`}
      />

      <section className="card" aria-label="گزارش روزانه">
        <header>
          <h2>گزارش روز {dateTime(day)}</h2>
          <Badge kind={provenance?.kind === "live" ? "live" : "paper"} label={provenance ? text(provenance.kind) : "پیپر"} />
        </header>
        <StateBlocks state={report} emptyLabel={NO_DATA} emptyHint={`گزارشی برای ${text(day)} ثبت نشده است.`}>
          {(data) => (
            <>
              <Markdown source={data.markdown} />
              <ProvenanceNote provenance={data.provenance} fallbackKind="paper" extra="/reports" />
            </>
          )}
        </StateBlocks>
      </section>

      <section className="card" aria-label="جدول شاخص‌ها">
        <header>
          <h2>شاخص‌های دوره</h2>
          <Badge kind={provenance?.kind ?? "paper"} />
        </header>
        {rows.length === 0 ? (
          fallback.loading || report.loading ? (
            <LoadingBlock label="خواندن شاخص‌ها…" />
          ) : (
            <EmptyBlock label={NO_DATA} hint="ledger هنوز metriکی برنگردانده است." />
          )
        ) : (
          <>
            <Table rows={rows} getRowId={(row) => row.label} columns={columns} caption="شاخص‌های تجمیعی ledger" />
            <ProvenanceNote provenance={provenance} fallbackKind="paper" extra="/performance" />
          </>
        )}
      </section>

      <section className="grid two" aria-label="نمودارها">
        <div className="card">
          <header>
            <h2>منحنی سرمایه</h2>
            <Badge kind="paper" />
          </header>
          <StateBlocks state={equity} emptyLabel={NO_DATA} emptyHint="snapshot حساب کاغذی موجود نیست.">
            {(data) => (
              <>
                <LineChart series={series} windowLabel={data.provenance?.window ?? null} sourceLabel={data.provenance?.source ?? "engine"} unit="USDT" />
                <ProvenanceNote provenance={data.provenance} fallbackKind="paper" extra="/equity-curve" />
              </>
            )}
          </StateBlocks>
        </div>
        <div className="card">
          <header>
            <h2>افت سرمایه</h2>
            <Badge kind="paper" />
          </header>
          <StateBlocks state={equity} emptyLabel={NO_DATA} emptyHint="برای drawdown به دو نقطه نیاز است.">
            {(data) => (
              <>
                <DrawdownChart
                  points={points.map((p) => [p[0], p[2] ?? null] as [string | number, number | null])}
                  windowLabel={data.provenance?.window ?? null}
                  sourceLabel={data.provenance?.source ?? "engine"}
                />
                <ProvenanceNote provenance={data.provenance} fallbackKind="paper" extra="/equity-curve" />
              </>
            )}
          </StateBlocks>
        </div>
      </section>

      <p className="notice warn">
        این گزارش سابقه‌ی ثبت‌شده است، نه پیش‌بینی. بازده گذشته تضمین‌کننده‌ی آینده نیست و ممکن است تمام سرمایه‌ی اختصاص‌یافته از دست برود. نرخ
        برد و ضریب سود بدون آزمون برون‌نمونه‌ی کافی، لبه‌ی آماری محسوب نمی‌شود.
      </p>
    </>
  );
}
