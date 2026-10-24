import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { BarChart3 } from "lucide-react";
import { useAsync } from "../api/client";
import {
  getProfiles,
  getProfileRun,
  type ProfileMarket,
  type ProfileRun,
  type ProfileStage,
  type ProfileStats,
  type ProfileTrade,
} from "../api/profilesClient";
import Badge, { ToneBadge } from "./Badge";
import Filters from "./Filters";
import ProvenanceNote from "./ProvenanceNote";
import Stat from "./Stat";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./StateBlocks";
import Table, { type Column } from "./Table";
import LineChart from "../charts/LineChart";
import { NO_DATA, dateTime, money, num, plainPct, signed, signedMoney, text, tone } from "../format";

const REASON_FA: Record<string, string> = {
  target: "رسیدن به هدف",
  stop: "حد ضرر",
  exit_horizon: "پایان افق زمانی",
  exit_time_limit: "سقف زمان",
  end_of_test: "پایان بازه‌ی آزمون",
  signal: "سیگنال خروج",
  trend_exit: "خروج روند",
};

function reasonFa(reason: string): string {
  return REASON_FA[reason] ?? reason;
}

function toneClass(value: number | null | undefined): string {
  const kind = tone(value);
  return kind === "neutral" ? "" : `tone-${kind}`;
}

/** Gross profit and gross loss side by side, both measured, neither hidden. */
function PnlBars({ stages, currency }: { stages: ProfileStage[]; currency: string }) {
  const scale = Math.max(
    1,
    ...stages.flatMap((stage) => [Math.abs(stage.all.gross_profit), Math.abs(stage.all.gross_loss)]),
  );
  return (
    <div className="pnl-bars" role="img" aria-label="سود ناخالص و زیان ناخالص هر مرحله">
      {stages.map((stage) => {
        const profit = Math.abs(stage.all.gross_profit);
        const loss = Math.abs(stage.all.gross_loss);
        return (
          <div className="pnl-row" key={stage.id}>
            <span className="pnl-label" title={stage.change}>
              {stage.title}
            </span>
            <div className="pnl-track" dir="ltr" aria-hidden="true">
              <div className="pnl-side neg">
                <span style={{ width: `${(loss / scale) * 100}%` }} />
              </div>
              <div className="pnl-center" />
              <div className="pnl-side pos">
                <span style={{ width: `${(profit / scale) * 100}%` }} />
              </div>
            </div>
            <span className="pnl-values num">
              <span className="tone-neg">{loss > 0 ? `−${money(loss, currency, { max: 0 })}` : "−0"}</span>
              <span aria-hidden="true"> / </span>
              <span className="tone-pos">{profit > 0 ? `+${money(profit, currency, { max: 0 })}` : "+0"}</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}

function SplitTable({ stage, currency }: { stage: ProfileStage; currency: string }) {
  const rows: { key: string; label: string; stats: ProfileStats; note: string }[] = [
    { key: "all", label: "کل بازه", stats: stage.all, note: "همه‌ی معامله‌های ثبت‌شده" },
    { key: "train", label: "آموزش (نیمه‌ی اول)", stats: stage.train, note: "انتخاب نامزد فقط با این نیمه" },
    { key: "validation", label: "اعتبارسنجی (نیمه‌ی دوم)", stats: stage.validation, note: "داوری یک‌باره روی این نیمه" },
  ];
  const columns: Column<(typeof rows)[number]>[] = [
    {
      key: "label",
      header: "بخش",
      render: (row) => (
        <div className="flex flex-col gap-0.5">
          <strong>{row.label}</strong>
          <span className="text-[11px] text-mute">{row.note}</span>
        </div>
      ),
    },
    { key: "trades", header: "معامله", numeric: true, sortValue: (row) => row.stats.trades, render: (row) => num(row.stats.trades, { max: 0 }) },
    {
      key: "gross_profit",
      header: "سود ناخالص",
      numeric: true,
      sortValue: (row) => row.stats.gross_profit,
      render: (row) => <span className="tone-pos">{money(row.stats.gross_profit, currency, { max: 0 })}</span>,
    },
    {
      key: "gross_loss",
      header: "زیان ناخالص",
      numeric: true,
      sortValue: (row) => row.stats.gross_loss,
      render: (row) => <span className="tone-neg">{signedMoney(row.stats.gross_loss, currency, { max: 0 })}</span>,
    },
    {
      key: "net",
      header: "خالص پس از کارمزد",
      numeric: true,
      sortValue: (row) => row.stats.net_pnl,
      render: (row) => <span className={toneClass(row.stats.net_pnl)}>{signedMoney(row.stats.net_pnl, currency, { max: 0 })}</span>,
    },
    { key: "fees", header: "کارمزد", numeric: true, sortValue: (row) => row.stats.fees_sum, render: (row) => money(row.stats.fees_sum, currency, { max: 0 }) },
    { key: "win", header: "برد%", numeric: true, sortValue: (row) => row.stats.win_rate_pct, render: (row) => plainPct(row.stats.win_rate_pct, { max: 1 }) },
    { key: "pf", header: "PF", numeric: true, sortValue: (row) => row.stats.profit_factor, render: (row) => num(row.stats.profit_factor, { max: 2 }) },
    {
      key: "hold",
      header: "نگه‌داشت میانه (ساعت)",
      numeric: true,
      sortValue: (row) => row.stats.median_hold_hours,
      render: (row) => num(row.stats.median_hold_hours, { max: 1 }),
    },
    {
      key: "intraday",
      header: "زیر 24 ساعت",
      numeric: true,
      sortValue: (row) => row.stats.within_day_pct,
      render: (row) => plainPct(row.stats.within_day_pct, { max: 1 }),
    },
  ];
  return (
    <Table
      columns={columns}
      rows={rows}
      getRowId={(row) => row.key}
      caption="مقایسه‌ی آموزش و اعتبارسنجی برای مرحله‌ی انتخاب‌شده"
      defaultSort={{ key: "label", dir: "asc" }}
    />
  );
}

function SymbolTable({ rows, currency, caption }: { rows: ProfileStage["top_symbols"]; currency: string; caption: string }) {
  const columns: Column<(typeof rows)[number]>[] = [
    { key: "symbol", header: "نماد", render: (row) => <strong>{row.symbol}</strong> },
    { key: "trades", header: "معامله", numeric: true, sortValue: (row) => row.trades, render: (row) => num(row.trades, { max: 0 }) },
    {
      key: "net",
      header: "خالص",
      numeric: true,
      sortValue: (row) => row.net_pnl,
      render: (row) => <span className={toneClass(row.net_pnl)}>{signedMoney(row.net_pnl, currency, { max: 0 })}</span>,
    },
    {
      key: "return",
      header: "بازده نسبت به سرمایه",
      numeric: true,
      sortValue: (row) => row.net_return_pct,
      render: (row) => <span className={toneClass(row.net_return_pct)}>{signed(row.net_return_pct, { max: 2 })}%</span>,
    },
    { key: "win", header: "برد%", numeric: true, sortValue: (row) => row.win_rate_pct, render: (row) => plainPct(row.win_rate_pct, { max: 1 }) },
  ];
  return <Table columns={columns} rows={rows} getRowId={(row) => row.symbol} caption={caption} emptyLabel={NO_DATA} emptyHint="در این مرحله هیچ نمادی معامله‌ی بسته‌شده ندارد." />;
}

function TradeFeed({ trades, truncated, currency }: { trades: ProfileTrade[]; truncated: number; currency: string }) {
  const wins = trades.filter((trade) => trade.pnl > 0).length;
  if (trades.length === 0) {
    return <EmptyBlock label="معامله‌ای ثبت نشده است" hint="این مرحله در بازه‌ی انتخاب‌شده هیچ ورودی نگرفت؛ صفر عدد واقعی است، نه داده‌ی گم‌شده." />;
  }
  return (
    <>
      <p className="stat-meta">
        <span>{num(trades.length, { max: 0 })} معامله نمایش داده شده</span>
        <span className="tone-pos">{num(wins, { max: 0 })} سودده</span>
        <span className="tone-neg">{num(trades.length - wins, { max: 0 })} زیان‌ده</span>
        {truncated > 0 ? <span>{num(truncated, { max: 0 })} معامله‌ی قدیمی‌تر در فایل اجرا ذخیره شده و اینجا فهرست نشده است.</span> : null}
      </p>
      <div className="feed-scroll" role="list" aria-label="فهرست معامله‌های مرحله‌ی انتخاب‌شده">
        {trades.map((trade, index) => (
          <div className="trade-line" role="listitem" key={`${trade.symbol}-${String(trade.exit_time)}-${String(index)}`}>
            <strong>{trade.symbol}</strong>
            <span className="num text-[12px] text-mute">{dateTime(trade.exit_time)}</span>
            <span className={`num ${toneClass(trade.pnl)}`}>{signedMoney(trade.pnl, currency, { max: 2 })}</span>
            <span className={`num ${toneClass(trade.return_pct)}`}>{signed(trade.return_pct, { max: 2 })}%</span>
            <span className="text-[12px] text-mute">
              {reasonFa(trade.reason)} · {num(trade.holding_hours, { max: 1 })} ساعت
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

/**
 * Shared read-only board for the staged experiments. It renders only what the
 * backend stored: an empty market says "no run yet" instead of a sample result,
 * and every missing number stays `داده‌ای موجود نیست`.
 */
export default function ProfileBoard({
  market,
  title = "پروفایل‌های سود و زیان",
  description,
  headingLevel = "h1",
}: {
  market: ProfileMarket;
  title?: string;
  description?: ReactNode;
  headingLevel?: "h1" | "h2";
}) {
  const Heading = headingLevel;
  const list = useAsync((signal) => getProfiles(market, signal), [market]);
  const [runId, setRunId] = useState<string | null>(null);
  const runs = list.data?.runs ?? [];

  useEffect(() => {
    if (runs.length === 0) {
      setRunId(null);
      return;
    }
    if (!runId || !runs.some((run) => run.run_id === runId)) setRunId(runs[0]?.run_id ?? null);
  }, [runs, runId]);

  const detail = useAsync(
    async (signal) => (runId ? getProfileRun(runId, market, signal) : null),
    [runId, market],
  );
  const run: ProfileRun | null = detail.data;
  const [stageId, setStageId] = useState<string | null>(null);

  const stages = run?.stages ?? [];
  const active: ProfileStage | null = useMemo(() => {
    if (stages.length === 0) return null;
    const chosen = stages.find((stage) => stage.id === stageId);
    if (chosen) return chosen;
    const finalist = stages.find((stage) => stage.id === run?.verdict.finalist);
    return finalist ?? stages[0] ?? null;
  }, [stages, stageId, run?.verdict.finalist]);

  useEffect(() => {
    setStageId(null);
  }, [runId]);

  const previousNet = useMemo(() => {
    const map = new Map<string, number | null>();
    stages.forEach((stage, index) => {
      map.set(stage.id, index === 0 ? null : stages[index - 1]?.all.net_pnl ?? null);
    });
    return map;
  }, [stages]);

  const currency = run?.meta.currency ?? "USDT";

  const stageColumns = useMemo<Column<ProfileStage>[]>(() => {
    const finalist = run?.verdict.finalist ?? null;
    const stable = new Set(run?.verdict.stable_stages ?? []);
    return [
      {
        key: "title",
        header: "مرحله",
        render: (stage) => (
          <div className="flex flex-col gap-0.5">
            <span className="flex flex-wrap items-center gap-1">
              <strong>{stage.title}</strong>
              {stage.id === finalist ? <ToneBadge tone="info">نامزد</ToneBadge> : null}
              {stable.has(stage.id) ? <ToneBadge tone="pos">پایدار</ToneBadge> : null}
            </span>
            <span className="text-[11px] text-mute num">{stage.id} · {stage.strategy}</span>
          </div>
        ),
      },
      { key: "trades", header: "معامله", numeric: true, sortValue: (stage) => stage.all.trades, render: (stage) => num(stage.all.trades, { max: 0 }) },
      {
        key: "gross_profit",
        header: "سود ناخالص",
        numeric: true,
        sortValue: (stage) => stage.all.gross_profit,
        render: (stage) => <span className="tone-pos">{money(stage.all.gross_profit, currency, { max: 0 })}</span>,
      },
      {
        key: "gross_loss",
        header: "زیان ناخالص",
        numeric: true,
        sortValue: (stage) => stage.all.gross_loss,
        render: (stage) => <span className="tone-neg">{signedMoney(stage.all.gross_loss, currency, { max: 0 })}</span>,
      },
      {
        key: "net",
        header: "خالص",
        numeric: true,
        sortValue: (stage) => stage.all.net_pnl,
        render: (stage) => <span className={toneClass(stage.all.net_pnl)}>{signedMoney(stage.all.net_pnl, currency, { max: 0 })}</span>,
      },
      {
        key: "delta",
        header: "Δ نسبت به مرحله‌ی قبل",
        numeric: true,
        sortValue: (stage) => {
          const prev = previousNet.get(stage.id);
          return prev === null || prev === undefined ? null : stage.all.net_pnl - prev;
        },
        render: (stage) => {
          const prev = previousNet.get(stage.id);
          if (prev === null || prev === undefined) return <span className="mute">مرحله‌ی اول</span>;
          const delta = stage.all.net_pnl - prev;
          return <span className={toneClass(delta)}>{signedMoney(delta, currency, { max: 0 })}</span>;
        },
      },
      { key: "pf", header: "PF", numeric: true, sortValue: (stage) => stage.all.profit_factor, render: (stage) => num(stage.all.profit_factor, { max: 2 }) },
      {
        key: "win",
        header: "برد%",
        numeric: true,
        sortValue: (stage) => stage.all.win_rate_pct,
        render: (stage) => plainPct(stage.all.win_rate_pct, { max: 1 }),
      },
      {
        key: "train_net",
        header: "خالص آموزش",
        numeric: true,
        sortValue: (stage) => stage.train.net_pnl,
        render: (stage) => <span className={toneClass(stage.train.net_pnl)}>{signedMoney(stage.train.net_pnl, currency, { max: 0 })}</span>,
      },
      {
        key: "validation_net",
        header: "خالص اعتبارسنجی",
        numeric: true,
        sortValue: (stage) => stage.validation.net_pnl,
        render: (stage) => <span className={toneClass(stage.validation.net_pnl)}>{signedMoney(stage.validation.net_pnl, currency, { max: 0 })}</span>,
      },
      {
        key: "hold",
        header: "نگه‌داشت میانه",
        numeric: true,
        sortValue: (stage) => stage.all.median_hold_hours,
        render: (stage) => (stage.all.median_hold_hours === null ? NO_DATA : `${num(stage.all.median_hold_hours, { max: 1 })} ساعت`),
      },
      {
        key: "intraday",
        header: "زیر 24 ساعت",
        numeric: true,
        sortValue: (stage) => stage.all.within_day_pct,
        render: (stage) => plainPct(stage.all.within_day_pct, { max: 1 }),
      },
    ];
  }, [currency, previousNet, run?.verdict.finalist, run?.verdict.stable_stages]);

  return (
    <section className="flex flex-col gap-4" aria-label={title}>
      <div className="page-head">
        <div>
          <Heading className="flex items-center gap-2">
            <BarChart3 size={22} className="text-info" aria-hidden="true" />
            {title}
          </Heading>
          <p className="mute">
            {description ?? (
              <>
                هر مرحله یک نسخه‌ی کامل و جداگانه است. سود ناخالص، زیان ناخالص، کارمزد و خالص پس از کارمزد برای همان مرحله
                اندازه‌گیری شده و انتخاب نامزد فقط با نیمه‌ی آموزش انجام می‌شود.
              </>
            )}
          </p>
        </div>
        <div className="btn-row">
          <Badge kind="simulated" label="بک‌تست ثبت‌شده — نه نتیجه‌ی زنده" />
        </div>
      </div>

      {list.error ? <ErrorBlock message={list.error} onRetry={list.reload} /> : null}
      {list.loading ? <LoadingBlock label="در حال خواندن اجراهای پروفایل…" /> : null}

      {!list.loading && !list.error && runs.length === 0 ? (
        <div className="card">
          <EmptyBlock
            label={market === "tse" ? "هنوز اجرای پروفایلی برای بورس تهران ذخیره نشده است" : "هنوز هیچ اجرای پروفایلی ذخیره نشده است"}
            hint={
              list.data?.note ??
              "برای ساخت اولین اجرا، دستور python trader.py profiles را اجرا کنید؛ نتیجه در state/research/profiles ذخیره می‌شود و همین‌جا نمایش داده می‌شود."
            }
          />
        </div>
      ) : null}

      {runs.length > 0 ? (
        <Filters
          fields={[
            {
              key: "run",
              label: "اجرای ثبت‌شده",
              type: "select",
              value: runId ?? "",
              onChange: setRunId,
              options: runs.map((entry) => ({
                value: entry.run_id,
                label: [
                  entry.timeframe ?? NO_DATA,
                  entry.days !== null ? `${num(entry.days, { max: 0 })} روز` : null,
                  entry.horizon === "short" ? "افق کوتاه" : "افق یک روز",
                  dateTime(entry.generated_at),
                ]
                  .filter(Boolean)
                  .join(" · "),
              })),
              hint: `${num(runs.length, { max: 0 })} اجرا ذخیره شده است`,
            },
          ]}
          actions={
            <button type="button" className="btn ghost small" onClick={() => { list.reload(); detail.reload(); }}>
              تازه‌سازی
            </button>
          }
          resultLabel={run ? `آخرین کندل: ${dateTime(run.meta.coverage_end_iso)}` : undefined}
        />
      ) : null}

      {detail.error ? <ErrorBlock message={detail.error} onRetry={detail.reload} /> : null}
      {runId && detail.loading ? <LoadingBlock label="در حال خواندن جزئیات اجرا…" /> : null}

      {run ? (
        <>
          <p className="stat-meta">
            <Badge kind="simulated" source={run.meta.source} />
            <span>بازار: {text(run.meta.market)} · تایم‌فریم: {text(run.meta.timeframe)} · {num(run.meta.days, { max: 0 })} روز</span>
            <span>نماد اندازه‌گیری‌شده: {num(run.meta.symbols_with_data, { max: 0 })} از {num(run.meta.symbols_requested, { max: 0 })}</span>
            <span>سرمایه‌ی هر نماد: {money(run.meta.capital_per_symbol, currency, { max: 0 })}</span>
            <span>اسپرد فرضی: {num(run.meta.assumed_spread_pct, { max: 2 })}%</span>
            <span>تولید: {dateTime(run.meta.generated_at)}</span>
          </p>

          <div className={`notice ${run.verdict.promoted ? "success" : "warn"}`} role="status">
            <strong>{run.verdict.promoted ? "نامزد روی داده‌ی بیرون‌نمونه هم مثبت ماند" : "هیچ نسخه‌ای برای ترفیع تأیید نشد"}</strong>
            <span>{run.verdict.text_fa}</span>
            <span className="mute">
              قاعده‌ی انتخاب: {run.verdict.selection_rule} · قاعده‌ی پایداری: {run.verdict.stable_rule}
            </span>
          </div>

          {active ? (
            <>
              <div className="grid stats">
                <Stat compact label="خالص مرحله‌ی انتخاب‌شده" value={signedMoney(active.all.net_pnl, currency, { max: 0 })} tone={tone(active.all.net_pnl)} window={active.title} source={run.meta.source ? text(run.meta.source) : null} />
                <Stat compact label="سود ناخالص" value={money(active.all.gross_profit, currency, { max: 0 })} tone="pos" window={active.title} />
                <Stat compact label="زیان ناخالص" value={signedMoney(active.all.gross_loss, currency, { max: 0 })} tone="neg" window={active.title} />
                <Stat compact label="کارمزد پرداختی" value={money(active.all.fees_sum, currency, { max: 0 })} window={active.title} />
                <Stat compact label="معامله‌ی بسته‌شده" value={num(active.all.trades, { max: 0 })} window={active.title} />
                <Stat compact label="نرخ برد" value={plainPct(active.all.win_rate_pct, { max: 1 })} window={active.title} />
                <Stat compact label="ضریب سود (PF)" value={num(active.all.profit_factor, { max: 2 })} window={active.title} tooltip="مجموع سود تقسیم بر مجموع زیان؛ اگر زیانی ثبت نشده باشد null می‌ماند." />
                <Stat compact label="نگه‌داشت میانه" value={active.all.median_hold_hours === null ? null : `${num(active.all.median_hold_hours, { max: 1 })} ساعت`} window={active.title} />
                <Stat compact label="معامله‌های زیر 24 ساعت" value={plainPct(active.all.within_day_pct, { max: 1 })} window={active.title} />
                <Stat compact label="معامله‌های زیر 3 ساعت" value={plainPct(active.all.within_3h_pct, { max: 1 })} window={active.title} />
              </div>

              <section className="card" aria-label="سود و زیان هر مرحله">
                <header>
                  <div className="section-head">
                    <h2>سود و زیان هر مرحله</h2>
                    <p className="mute">هر ردیف یک نسخه‌ی مستقل است؛ سود و زیان ناخالص کنار هم و خالص پس از کارمزد در جدول.</p>
                  </div>
                </header>
                <PnlBars stages={stages} currency={currency} />
                <Table
                  columns={stageColumns}
                  rows={stages}
                  getRowId={(stage) => stage.id}
                  caption="پروفایل‌های توسعه به ترتیب؛ سطر را انتخاب کنید تا جزئیات و معامله‌های همان مرحله دیده شود"
                  onRowClick={(stage) => setStageId(stage.id)}
                  emptyLabel="هیچ مرحله‌ای ثبت نشده است"
                />
              </section>

              <section className="card" aria-label="جزئیات مرحله‌ی انتخاب‌شده">
                <header>
                  <div className="section-head">
                    <h2>{active.title}</h2>
                    <p className="mute">
                      <span className="num">{active.id}</span> · {active.strategy} · {active.horizon === "none" ? "بدون افق زمانی" : `افق ${num(active.max_hold_hours, { max: 1 })} ساعت`}
                    </p>
                  </div>
                  <ToneBadge tone={active.all.net_pnl >= 0 ? "pos" : "neg"}>
                    {active.all.net_pnl >= 0 ? "خالص مثبت" : "خالص منفی"}
                  </ToneBadge>
                </header>
                <p className="stat-note">{active.change}</p>
                <SplitTable stage={active} currency={currency} />
                {active.basket_curve.length > 1 ? (
                  <LineChart
                    series={[{ id: active.id, label: active.title, points: active.basket_curve }]}
                    windowLabel={`${text(run.meta.timeframe)} · ${num(run.meta.days, { max: 0 })} روز`}
                    sourceLabel={text(run.meta.source)}
                    unit="%"
                    ariaLabel="منحنی سبد هم‌وزن مرحله‌ی انتخاب‌شده"
                  />
                ) : (
                  <EmptyBlock label="منحنی سرمایه‌ای ثبت نشده است" hint="این مرحله کندل معامله‌شده‌ای نداشت تا منحنی بسازد." />
                )}
                <div className="grid two">
                  <div>
                    <h3 className="section-head">بهترین نمادها</h3>
                    <SymbolTable rows={active.top_symbols} currency={currency} caption="بهترین نمادها بر اساس سود خالص" />
                  </div>
                  <div>
                    <h3 className="section-head">ضعیف‌ترین نمادها</h3>
                    <SymbolTable rows={active.bottom_symbols} currency={currency} caption="ضعیف‌ترین نمادها بر اساس سود خالص" />
                  </div>
                </div>
                <p className="stat-meta">
                  <span>دلیل خروج:</span>
                  {Object.entries(active.all.exits).length === 0 ? (
                    <span>{NO_DATA}</span>
                  ) : (
                    Object.entries(active.all.exits).map(([reason, count]) => (
                      <span key={reason}>
                        {reasonFa(reason)}: {num(count, { max: 0 })}
                      </span>
                    ))
                  )}
                  {Object.keys(active.skipped).length > 0 ? (
                    <span>{num(Object.keys(active.skipped).length, { max: 0 })} نماد به‌دلیل کمبود تاریخچه یا خطای داده رد شد.</span>
                  ) : null}
                </p>
              </section>

              <section className="card" aria-label="معامله‌های مرحله‌ی انتخاب‌شده">
                <header>
                  <div className="section-head">
                    <h2>معامله‌های ثبت‌شده</h2>
                    <p className="mute">سود یا زیان هر معامله پس از کارمزد همان معامله است؛ کارمزد جدا هم نمایش داده می‌شود.</p>
                  </div>
                </header>
                <TradeFeed trades={active.trades} truncated={active.trades_truncated} currency={currency} />
              </section>
            </>
          ) : null}

          <p className="notice warn">{run.meta.disclaimer}</p>
          <p className="stat-meta">
            <span>{run.meta.live_gate}</span>
            <span>{run.meta.horizon_note}</span>
            <span>تقسیم: {run.meta.split_rule}</span>
          </p>
          <ProvenanceNote
            provenance={run.provenance}
            fallbackKind="simulated"
            extra={`آخرین کندل: ${dateTime(run.meta.coverage_end_iso)} · ${num(run.meta.symbols_with_data, { max: 0 })} نماد`}
          />
        </>
      ) : null}
    </section>
  );
}
