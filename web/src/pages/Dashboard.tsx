import { useMemo } from "react";
import {
  getConfig,
  getEquityCurve,
  getHealth,
  getLogs,
  getPerformance,
  getPortfolio,
  getRisk,
  getRuns,
  getStrategies,
  getTrades,
  useAsync,
  type Metrics,
  type PerformanceView,
  type TradeRow,
} from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks, { EmptyBlock, LoadingBlock } from "../components/StateBlocks";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import DrawdownChart from "../charts/DrawdownChart";
import LineChart from "../charts/LineChart";
import { NO_DATA, dateTime, money, num, pct, signedMoney, text, tone, plainPct } from "../format";

interface Bucket {
  key: string;
  trades: number;
  wins: number;
  losses: number;
  net: number;
  fees: number;
  lastExit: number | null;
}

/** Group real ledger rows by a key. Nothing is invented: absent -> `null`. */
function bucketize(rows: TradeRow[], pick: (row: TradeRow) => string | null): Bucket[] {
  const map = new Map<string, Bucket>();
  for (const row of rows) {
    const key = pick(row) ?? "بدون برچسب";
    const bucket = map.get(key) ?? { key, trades: 0, wins: 0, losses: 0, net: 0, fees: 0, lastExit: null };
    bucket.trades += 1;
    const net = typeof row.net === "number" ? row.net : null;
    if (net !== null) {
      bucket.net += net;
      if (net > 0) bucket.wins += 1;
      else bucket.losses += 1;
    }
    if (typeof row.fees === "number") bucket.fees += row.fees;
    if (typeof row.exit_ts === "number" && (bucket.lastExit === null || row.exit_ts > bucket.lastExit)) bucket.lastExit = row.exit_ts;
    map.set(key, bucket);
  }
  return [...map.values()].sort((a, b) => b.trades - a.trades);
}

function winRate(bucket: Bucket): number | null {
  const decided = bucket.wins + bucket.losses;
  return decided > 0 ? (bucket.wins / decided) * 100 : null;
}

export default function Dashboard() {
  const health = useAsync((signal) => getHealth(signal), []);
  const config = useAsync((signal) => getConfig(signal), []);
  const portfolio = useAsync((signal) => getPortfolio(signal), []);
  const performance = useAsync((signal) => getPerformance({ mode: "PAPER" }, signal), []);
  const trades = useAsync((signal) => getTrades({ mode: "PAPER", limit: 1000 }, signal), []);
  const runs = useAsync((signal) => getRuns({ limit: 25 }, signal), []);
  const strategies = useAsync((signal) => getStrategies(signal), []);
  const risk = useAsync((signal) => getRisk(signal), []);
  const errorLogs = useAsync((signal) => getLogs({ level: "error", limit: 25 }, signal), []);
  const equity = useAsync((signal) => getEquityCurve("PAPER", signal), []);

  const symbols = useMemo(() => config.data?.symbols ?? [], [config.data]);
  const perSymbol = useAsync(async (signal) => {
    if (symbols.length === 0) return [];
    return Promise.all(symbols.map((symbol) => getPerformance({ symbol, mode: "PAPER" }, signal)));
  }, [symbols.join(","), config.loading]);

  const ledgerWindow = performance.data?.provenance?.window ?? null;
  const ledgerSource = performance.data?.provenance?.source ?? null;
  const metrics: Metrics | null = performance.data?.metrics ?? null;
  const account = portfolio.data;
  const accountWindow = account?.updated_at ? `لحظه‌ی آخرین به‌روزرسانی: ${dateTime(account.updated_at)}` : null;
  const drawdownCap = config.data?.limits?.max_drawdown_pct ?? null;
  const drawdownTone: "neg" | "neutral" =
    account?.drawdown_pct != null && drawdownCap != null && account.drawdown_pct >= drawdownCap * 0.75 ? "neg" : "neutral";

  const bySymbol = useMemo(() => bucketize(trades.data?.rows ?? [], (row) => row.symbol), [trades.data]);
  const byTimeframe = useMemo(() => bucketize(trades.data?.rows ?? [], (row) => row.timeframe), [trades.data]);

  const latestRun = runs.data?.[0] ?? null;
  const optimizationRounds = (runs.data ?? []).filter((run) => run.kind === "grid" || run.kind === "walk_forward").length;

  const warnings = [
    ...(health.data?.warnings ?? []),
    ...(config.data?.warnings ?? []),
    ...(risk.data?.warnings ?? []),
  ];
  const breaches = risk.data?.breaches ?? [];

  const bucketColumns = (header: string): Column<Bucket>[] => [
    { key: "key", header, render: (row) => <span className="num">{row.key}</span>, sortValue: (row) => row.key },
    { key: "trades", header: "تعداد معامله", numeric: true, sortValue: (row) => row.trades, render: (row) => num(row.trades, { max: 0 }) },
    { key: "wins", header: "برد", numeric: true, sortValue: (row) => row.wins, render: (row) => <span className="tone-pos">{num(row.wins, { max: 0 })}</span> },
    { key: "losses", header: "باخت", numeric: true, sortValue: (row) => row.losses, render: (row) => <span className="tone-neg">{num(row.losses, { max: 0 })}</span> },
    {
      key: "win_rate",
      header: "نرخ برد",
      numeric: true,
      sortValue: (row) => winRate(row),
      render: (row) => plainPct(winRate(row), { max: 1 }),
    },
    {
      key: "net",
      header: "خالص پس از کارمزد",
      numeric: true,
      sortValue: (row) => row.trades > 0 ? row.net : null,
      render: (row) => <span className={`tone-${tone(row.trades > 0 ? row.net : null)}`}>{signedMoney(row.trades > 0 ? row.net : null)}</span>,
    },
    { key: "fees", header: "کارمزد", numeric: true, sortValue: (row) => row.fees, render: (row) => money(row.fees) },
    { key: "last", header: "آخرین خروج", sortValue: (row) => row.lastExit, render: (row) => dateTime(row.lastExit) },
  ];

  const equitySeries = useMemo(() => {
    const points = equity.data?.points ?? [];
    const line = points.map((point) => [point[0], point[1]] as [string | number, number | null]);
    const buyHold = (equity.data?.buy_hold ?? []).map((point) => [point[0], point[1]] as [string | number, number | null]);
    const series = [{ id: "equity", label: "Equity کاغذی", points: line, color: "var(--info)" }];
    if (buyHold.length > 1) series.push({ id: "buy_hold", label: "خرید و نگه‌داشتن (ملاک مقایسه)", points: buyHold, color: "var(--warn)" });
    return series;
  }, [equity.data]);

  const drawdownPoints = useMemo(
    () => (equity.data?.points ?? []).map((point) => [point[0], point[2] ?? null] as [string | number, number | null]),
    [equity.data],
  );

  const perSymbolColumns: Column<PerformanceView>[] = [
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "trades", header: "معامله", numeric: true, sortValue: (row) => row.metrics.trades, render: (row) => num(row.metrics.trades, { max: 0 }) },
    { key: "win", header: "نرخ برد", numeric: true, sortValue: (row) => row.metrics.win_rate_pct, render: (row) => plainPct(row.metrics.win_rate_pct, { max: 1 }) },
    { key: "pf", header: "ضریب سود", numeric: true, sortValue: (row) => row.metrics.profit_factor, render: (row) => num(row.metrics.profit_factor, { max: 2 }) },
    {
      key: "net",
      header: "خالص پس از کارمزد",
      numeric: true,
      sortValue: (row) => row.metrics.net_pnl,
      render: (row) => <span className={`tone-${tone(row.metrics.net_pnl)}`}>{signedMoney(row.metrics.net_pnl)}</span>,
    },
    { key: "ret", header: "بازده", numeric: true, sortValue: (row) => row.metrics.net_return_pct, render: (row) => pct(row.metrics.net_return_pct, { max: 2 }) },
    { key: "dd", header: "بیشترین افت", numeric: true, sortValue: (row) => row.metrics.max_drawdown_pct, render: (row) => pct(row.metrics.max_drawdown_pct, { max: 2 }) },
    { key: "fees", header: "کارمزد", numeric: true, sortValue: (row) => row.metrics.fees_paid, render: (row) => money(row.metrics.fees_paid) },
    { key: "window", header: "پنجره", render: (row) => text(row.window ?? row.provenance?.window) },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>داشبورد</h1>
          <p className="mute">همه‌ی مقادیر از API محلی خوانده می‌شوند؛ هرچه در API نباشد «داده‌ای موجود نیست» است، نه صفر یا عدد نمونه.</p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode} />
          <Badge kind="paper" label="حساب کاغذی — بدون سفارش زنده" />
        </div>
      </div>

      {health.error ? (
        <div className="notice danger" role="alert">
          <strong>اتصال برقرار نشد.</strong>
          <span>{health.error}</span>
        </div>
      ) : null}

      <section aria-label="شاخص‌های اصلی حساب">
        <h2>شاخص‌های اصلی</h2>
        {portfolio.loading && performance.loading ? <LoadingBlock label="خواندن حساب و ledger…" /> : null}
        <div className="grid stats">
          <Stat
            label="سرمایه‌ی تخصیص‌یافته‌ی اولیه"
            value={money(account?.allocated_capital ?? null)}
            window={accountWindow}
            source="config.allocated_capital"
            endpoint="/portfolio"
            tooltip="موجودی فرضی حساب کاغذی. عامل هیچ‌وقت آن را افزایش نمی‌دهد."
          />
          <Stat label="ارزش فعلی حساب (equity)" value={money(account?.equity ?? null)} window={accountWindow} source="engine/paper" endpoint="/portfolio" />
          <Stat
            label="سود/زیان تحقق‌یافته"
            value={signedMoney(account?.realized ?? null)}
            tone={tone(account?.realized ?? null)}
            window={accountWindow}
            source="ledger"
            endpoint="/portfolio"
          />
          <Stat
            label="سود/زیان تحقق‌نیافته"
            value={signedMoney(account?.unrealized ?? null)}
            tone={tone(account?.unrealized ?? null)}
            window={accountWindow}
            source="mark-to-market"
            endpoint="/portfolio"
            tooltip="محاسبه با آخرین قیمت معتبر؛ اگر قیمت تازه نباشد سیستم در حالت امن می‌ماند."
          />
          <Stat
            label="بازده خالص پس از کارمزد"
            value={
              account?.allocated_capital && typeof account.equity === "number"
                ? pct(((account.equity - account.allocated_capital) / account.allocated_capital) * 100, { max: 2 })
                : null
            }
            tone={
              account?.allocated_capital && typeof account.equity === "number"
                ? tone(account.equity - account.allocated_capital)
                : "neutral"
            }
            window={ledgerWindow}
            source="engine"
            endpoint="/portfolio"
            note="اختلاف equity فعلی (شامل تحقق‌نیافته) با سرمایه‌ی تخصیص‌یافته؛ کارمزد ثبت‌شده در این عدد کسر شده است."
          />
          <Stat label="تعداد کل معاملات" value={num(metrics?.trades ?? null, { max: 0 })} window={ledgerWindow} source={ledgerSource} endpoint="/performance" />
          <Stat
            label="برد / باخت"
            value={metrics ? `${num(metrics.wins ?? null, { max: 0 })} / ${num(metrics.losses ?? null, { max: 0 })}` : null}
            window={ledgerWindow}
            source={ledgerSource}
            endpoint="/performance"
            note="مبنای باخت: خالص پس از کارمزد ≤ ۰."
          />
          <Stat label="نرخ برد" value={plainPct(metrics?.win_rate_pct ?? null, { max: 1 })} window={ledgerWindow} source={ledgerSource} endpoint="/performance" />
          <Stat
            label="ضریب سود (profit factor)"
            value={num(metrics?.profit_factor ?? null, { max: 2 })}
            window={ledgerWindow}
            source={ledgerSource}
            endpoint="/performance"
            tooltip="مجموع سود تقسیم بر مجموع زیان؛ وقتی هیچ زیانی ثبت نشده باشد API مقدار null می‌فرستد و اینجا عددی ساخته نمی‌شود."
          />
          <Stat
            label="بیشترین افت سرمایه"
            value={pct(metrics?.max_drawdown_pct ?? null, { max: 2 })}
            tone={drawdownTone}
            window={ledgerWindow}
            source={ledgerSource}
            endpoint="/performance"
            note={`سقف فعال drawdown: ${plainPct(config.data?.limits?.max_drawdown_pct ?? null, { max: 1 })}`}
          />
          <Stat
            label="شارپ (تحویلی و خام)"
            value={num(metrics?.sharpe_annualised_crude ?? null, { max: 2 })}
            window={ledgerWindow}
            source={ledgerSource}
            endpoint="/performance"
            tooltip={
              metrics?.sharpe_note ??
              "بازده هر کندل میانگین‌گیری و روی sqrt(کندل در سال) مقیاس می‌شود؛ بدون نرخ بدون‌ریسک، بدون ادعاى پیش‌بینی."
            }
          />
          <Stat
            label="کارمزد کل"
            value={money(metrics?.fees_paid ?? metrics?.fees ?? account?.fees_paid ?? null)}
            window={ledgerWindow}
            source={ledgerSource}
            endpoint="/performance"
            note={`نرخ پیکربندی: ${plainPct(config.data?.taker_fee_pct ?? null, { max: 2 })} taker`}
          />
          <Stat
            label="اسلیپیج کل"
            value={account?.slippage_paid == null ? null : money(account.slippage_paid)}
            window={ledgerWindow}
            source="ledger"
            endpoint="/portfolio"
            note={
              account?.slippage_paid == null
                ? "ledger اسلیپیج تحقق‌یافته را جدا ثبت نمی‌کند؛ سقف اسلیپیج از ریسک‌گیت اخذ می‌شود."
                : undefined
            }
            tooltip={`سقف اسلیپیج مجاز: ${plainPct(config.data?.limits?.max_slippage_pct ?? null, { max: 2 })}`}
          />
        </div>
      </section>

      <section className="grid two" aria-label="روند سرمایه">
        <div className="card">
          <header>
            <h2>منحنی سرمایه در برابر خرید و نگه‌داشتن</h2>
            <Badge kind="paper" />
          </header>
          <StateBlocks state={equity} emptyLabel={NO_DATA} emptyHint="هنوز snapshot حساب کاغذی ثبت نشده است.">
            {(data) => (
              <>
                <LineChart
                  series={equitySeries}
                  windowLabel={data.provenance?.window ?? null}
                  sourceLabel={data.provenance?.source ?? "engine"}
                  unit="USDT"
                  ariaLabel="منحنی سرمایه"
                />
                <ProvenanceNote provenance={data.provenance} fallbackKind="paper" extra="/equity-curve" />
              </>
            )}
          </StateBlocks>
        </div>
        <div className="card">
          <header>
            <h2>افت سرمایه از قله</h2>
            <Badge kind="paper" />
          </header>
          <StateBlocks state={equity} emptyLabel={NO_DATA} emptyHint="برای رسم drawdown به snapshot بیشتری نیاز است.">
            {(data) => (
              <>
                <DrawdownChart points={drawdownPoints} windowLabel={data.provenance?.window ?? null} sourceLabel={data.provenance?.source ?? "engine"} />
                <ProvenanceNote provenance={data.provenance} fallbackKind="paper" extra="/equity-curve" />
              </>
            )}
          </StateBlocks>
        </div>
      </section>

      <section className="grid two" aria-label="عملکرد به تفکیک">
        <div className="card">
          <header>
            <h2>عملکرد به تفکیک نماد (از API)</h2>
            <Badge kind="paper" label="پیپر" />
          </header>
          <StateBlocks state={perSymbol} emptyLabel={NO_DATA} emptyHint="پیکربندی نمادی در دسترس نیست یا /performance پاسخ نداد.">
            {(rows) => (rows.length === 0 ? <EmptyBlock label={NO_DATA} /> : <Table rows={rows} getRowId={(row) => row.symbol ?? "unknown"} caption="عملکرد هر نماد از /performance?symbol=" columns={perSymbolColumns} />)}
          </StateBlocks>
        </div>

        <div className="card">
          <header>
            <h2>عملکرد به تفکیک تایم‌فریم</h2>
            <ToneBadge tone="warn" title="قرارداد API پارامتر timeframe برای /performance ندارد">
              استخراج‌شده از /trades
            </ToneBadge>
          </header>
          <StateBlocks
            state={trades}
            emptyLabel={NO_DATA}
            emptyHint="بدون معامله‌ی ثبت‌شده، تفکیک تایم‌فریم معنایی ندارد."
          >
            {() => (
              <>
                <Table
                  rows={byTimeframe}
                  getRowId={(row) => `tf-${row.key}`}
                  caption="دسته‌بندی ردیف‌های ledger بر اساس timeframe"
                  columns={bucketColumns("تایم‌فریم")}
                />
                <ProvenanceNote provenance={trades.data?.provenance ?? null} fallbackKind="paper" extra="دسته‌بندی محلی از ردیف‌های /trades" />
              </>
            )}
          </StateBlocks>
        </div>
      </section>

      <section className="card" aria-label="عملکرد به تفکیک نماد (ledger)">
        <header>
          <h2>ledger به تفکیک نماد</h2>
          <Badge kind="paper" />
        </header>
        <StateBlocks state={trades} emptyLabel={NO_DATA} emptyHint="هیچ معامله‌ای در حالت پیپر ثبت نشده است.">
          {() => (
            <>
              <Table rows={bySymbol} getRowId={(row) => `sym-${row.key}`} caption="دسته‌بندی ردیف‌های ledger بر اساس نماد" columns={bucketColumns("نماد")} />
              <ProvenanceNote provenance={trades.data?.provenance ?? null} fallbackKind="paper" extra="دسته‌بندی محلی از ردیف‌های /trades" />
            </>
          )}
        </StateBlocks>
      </section>

      <section className="grid stats" aria-label="وضعیت سامانه">
        <div className="card">
          <header>
            <h3>آخرین اجرای ثبت‌شده</h3>
            {latestRun ? <Badge kind={latestRun.kind === "backtest" || latestRun.kind === "walk_forward" ? "simulated" : "paper"} /> : null}
          </header>
          {runs.loading ? <LoadingBlock label="خواندن /runs…" /> : null}
          {runs.error ? <div className="notice danger" role="alert">{runs.error}</div> : null}
          {!runs.loading && !runs.error && !latestRun ? <EmptyBlock label={NO_DATA} hint="هنوز اجرایی (بک‌تست/والک‌فورورد) ثبت نشده است." /> : null}
          {latestRun ? (
            <dl className="kv">
              <dt>نوع</dt>
              <dd className="rtl-text">{text(latestRun.kind)}</dd>
              <dt>وضعیت</dt>
              <dd className="rtl-text">{text(latestRun.status)}</dd>
              <dt>نماد</dt>
              <dd>{text(latestRun.symbol)}</dd>
              <dt>تاریخ</dt>
              <dd className="rtl-text">{dateTime(latestRun.created_at)}</dd>
              <dt>پنجره</dt>
              <dd className="rtl-text">{text(latestRun.window_start)} ← {text(latestRun.window_end)}</dd>
              <dt>بازده خالص</dt>
              <dd className={`rtl-text tone-${tone(latestRun.metrics.net_return_pct ?? null)}`}>{pct(latestRun.metrics.net_return_pct ?? null, { max: 2 })}</dd>
              <dt>پارامترها</dt>
              <dd className="rtl-text">{text(latestRun.params_label)}</dd>
            </dl>
          ) : null}
        </div>

        <div className="card">
          <header>
            <h3>نسخه‌ی استراتژی فعال</h3>
            <Badge kind="simulated" label="پژوهشی" />
          </header>
          <StateBlocks state={strategies} emptyLabel={NO_DATA} emptyHint="نسخه‌ای در strategy_versions ثبت نشده است.">
            {(rows) => {
              const item = rows.find((row) => row.accepted) ?? rows[0];
              if (!item) return <EmptyBlock label={NO_DATA} />;
              return (
                <dl className="kv">
                  <dt>نام/نسخه</dt>
                  <dd className="rtl-text">{`${text(item.name)} ${text(item.version)}`}</dd>
                  <dt>هش (digest)</dt>
                  <dd>{text(item.digest)}</dd>
                  <dt>وضعیت پذیرش</dt>
                  <dd className="rtl-text">{item.accepted ? "پذیرفته‌شده" : "پذیرفته‌نشده"}</dd>
                  <dt>تصمیم</dt>
                  <dd className="rtl-text">{text(item.decision)}</dd>
                  <dt>آزمون برون‌نمونه‌ای</dt>
                  <dd className="rtl-text">{pct(item.test?.net_return_pct ?? null, { max: 2 })}</dd>
                  <dt>پنجره</dt>
                  <dd className="rtl-text">{text(item.provenance?.window)}</dd>
                </dl>
              );
            }}
          </StateBlocks>
        </div>

        <div className="card">
          <header>
            <h3>دورهای بهینه‌سازی</h3>
            <Badge kind="simulated" />
          </header>
          <p className="stat-value">{runs.loading ? "…" : num(optimizationRounds, { max: 0 })}</p>
          <p className="mute">
            شمارش اجرای kind=grid و kind=walk_forward در ۲۵ اجرای اخیر از /runs — نه شمار کل پایگاه‌داده.
          </p>
          <p className="stat-note">
            کل ردیف‌های runs در /health: {num(health.data?.counts?.runs ?? null, { max: 0 })}
          </p>
        </div>

        <div className="card">
          <header>
            <h3>خطاها و هشدارها</h3>
            <ToneBadge tone={breaches.length > 0 ? "neg" : warnings.length > 0 ? "warn" : "pos"}>
              {breaches.length > 0 ? "نقض محدودیت" : warnings.length > 0 ? "هشدار" : "وضعیت عادی"}
            </ToneBadge>
          </header>
          <p className="mute">
            خطاهای ۲۵ رویداد اخیر log: {num(errorLogs.data?.length ?? null, { max: 0 })} · خطاهای پیاپی حساب:{" "}
            {num(account?.consecutive_errors ?? null, { max: 0 })} از سقف{" "}
            {num(config.data?.limits?.stop_on_consecutive_errors ?? null, { max: 0 })}
          </p>
          {breaches.length > 0 ? (
            <ul>
              {breaches.map((item) => (
                <li key={item} className="tone-neg">
                  {item}
                </li>
              ))}
            </ul>
          ) : null}
          {warnings.length > 0 ? (
            <ul>
              {warnings.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          {errorLogs.data && errorLogs.data.length > 0 ? (
            <ul>
              {errorLogs.data.slice(0, 5).map((log) => (
                <li key={`${String(log.id)}-${log.ts}`}>
                  <span className="mute">{dateTime(log.ts)}</span> {log.message}
                </li>
              ))}
            </ul>
          ) : null}
          {warnings.length === 0 && breaches.length === 0 && (errorLogs.data?.length ?? 0) === 0 && !errorLogs.loading ? (
            <p className="stat-note">هیچ هشدار یا خطایی از /config، /risk و /logs?level=error گزارش نشده است.</p>
          ) : null}
        </div>
      </section>

      <section className="notice" aria-label="افزونه‌های عدد">
        <span>
          میانگین سود/زیان از /performance: {money(metrics?.avg_win ?? null)} در برابر {money(metrics?.avg_loss ?? null)} — نرخ برد و ضریب سود به‌تنهایی
          لبه‌ی آماری نیستند؛ برای پذیرش، walk-forward و آزمون برون‌نمونه‌ای لازم است.
        </span>
      </section>
    </>
  );
}
