import {
  getConfig,
  getEquityCurve,
  getHealth,
  getPortfolio,
  useAsync,
  type Portfolio,
  type PositionRow,
  type Provenance,
} from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks from "../components/StateBlocks";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import DrawdownChart from "../charts/DrawdownChart";
import LineChart from "../charts/LineChart";
import { NO_DATA, dateTime, money, num, pct, plainPct, signedMoney, text, tone } from "../format";

const NOT_RECORDED = "ثبت نشده";

/* ------------------------------------------------------------------ helpers
 * Local adapters (shared files are owned by other agents): the backend's
 * `/portfolio` answer carries `mark`/`equity_basis`/`price_source`/`closed_trades`
 * which `client.ts` does not declare; these readers surface them without
 * inventing values — an absent field stays `null`.
 */

type PortfolioExtra = Portfolio & {
  closed_trades?: number | null;
  price_source?: string | null;
  equity_basis?: string | null;
  /** `/portfolio` sends provenance but the shared `Portfolio` type does not declare it. */
  provenance?: Provenance | null;
};

type PositionExtra = PositionRow & { mark?: number | null };

function markPrice(row: PositionRow): number | null {
  const extra = row as PositionExtra;
  const value = extra.mark ?? extra.mark_price ?? null;
  return typeof value === "number" ? value : null;
}

function numberExtra(view: Portfolio, key: string): number | null {
  const value = (view as unknown as Record<string, unknown>)[key];
  return typeof value === "number" ? value : null;
}

function stringExtra(view: Portfolio, key: string): string | null {
  const value = (view as unknown as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

export default function PortfolioPage() {
  const health = useAsync((signal) => getHealth(signal), []);
  const config = useAsync((signal) => getConfig(signal), []);
  const account = useAsync((signal) => getPortfolio(signal), []);
  const equity = useAsync((signal) => getEquityCurve("PAPER", signal), []);

  const data = account.data;
  const positions = (data?.positions ?? []) as PositionExtra[];
  const limits = config.data?.limits ?? null;
  const allocated = config.data?.allocated_capital ?? data?.allocated_capital ?? null;

  const equityWindow = data?.updated_at ? `لحظه‌ی آخرین به‌روزرسانی: ${dateTime(data.updated_at)}` : "وضعیت لحظه‌ای حساب کاغذی (PAPER)";
  const equityBasis = data ? stringExtra(data, "equity_basis") : null;
  const priceSource = data ? stringExtra(data, "price_source") : null;
  const closedTrades = data ? numberExtra(data, "closed_trades") : null;

  const drawdownCap = limits?.max_drawdown_pct ?? null;
  const drawdownTone: "neg" | "neutral" =
    data?.drawdown_pct != null && drawdownCap != null && data.drawdown_pct >= drawdownCap * 0.75 ? "neg" : "neutral";

  const equitySeries = [
    {
      id: "equity",
      label: "Equity کاغذی",
      points: (equity.data?.points ?? []).map((p) => [p[0], p[1]] as [string | number, number | null]),
      color: "var(--info)",
    },
  ];

  const positionColumns: Column<PositionExtra>[] = [
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "qty", header: "تعداد", numeric: true, sortValue: (row) => row.qty, render: (row) => num(row.qty, { max: 6 }) },
    { key: "avg_entry", header: "میانگین ورود (USDT)", numeric: true, sortValue: (row) => row.avg_entry, render: (row) => money(row.avg_entry, "USDT", { max: 4 }) },
    {
      key: "mark",
      header: "قیمت مارک",
      numeric: true,
      sortValue: (row) => markPrice(row),
      render: (row) =>
        markPrice(row) === null ? (
          <span className="mute">{NOT_RECORDED}</span>
        ) : (
          money(markPrice(row), "USDT", { max: 4 })
        ),
    },
    {
      key: "unrealized",
      header: "سود/زیان تحقق‌نیافته",
      numeric: true,
      sortValue: (row) => (typeof row.unrealized === "number" ? row.unrealized : null),
      render: (row) =>
        typeof row.unrealized === "number" ? (
          <span className={`tone-${tone(row.unrealized)}`}>{signedMoney(row.unrealized)}</span>
        ) : (
          <span className="mute">{NOT_RECORDED}</span>
        ),
    },
    { key: "stop", header: "حد ضرر (USDT)", numeric: true, sortValue: (row) => row.stop, render: (row) => money(row.stop, "USDT", { max: 4 }) },
    { key: "target", header: "هدف (USDT)", numeric: true, sortValue: (row) => row.target, render: (row) => money(row.target, "USDT", { max: 4 }) },
    { key: "opened", header: "زمان ورود", sortValue: (row) => row.opened_at, render: (row) => dateTime(row.opened_at) },
    { key: "decision", header: "شناسه تصمیم", render: (row) => <span className="num">{text(row.decision_id, NOT_RECORDED)}</span> },
    { key: "mode", header: "حالت", render: (row) => <Badge kind="paper" label={text(row.mode ?? "PAPER", "PAPER")} /> },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>پرتفوی کاغذی</h1>
          <p className="mute">
            موقعیت‌های باز و وضعیت حساب از <code>/portfolio</code> — موجودی فرضی در حالت PAPER. هر مقداری که ledger
            ثبت نکرده باشد «داده‌ای موجود نیست» یا «ثبت نشده» می‌ماند؛ عددی ساخته نمی‌شود.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode ?? data?.mode ?? null} />
          <Badge kind="paper" label="حساب کاغذی — بدون سفارش زنده" />
        </div>
      </div>

      {account.error ? (
        <div className="notice danger" role="alert">
          <strong>خواندن حساب ممکن نشد.</strong>
          <span>{account.error}</span>
        </div>
      ) : null}

      <section aria-label="وضعیت حساب">
        <h2>وضعیت حساب</h2>
        <div className="grid stats">
          <Stat
            label="سرمایه‌ی تخصیص‌یافته (فرضی)"
            value={money(allocated)}
            window="از پیکربندی"
            source="config.allocated_capital"
            endpoint="/config"
            tooltip="موتور هرگز این عدد را افزایش نمی‌دهد؛ مبنای همه‌ی سقف‌های ریسک است."
          />
          <Stat
            label="ارزش فعلی حساب (equity)"
            value={money(data?.equity ?? null)}
            window={equityWindow}
            source="engine/paper"
            endpoint="/portfolio"
            note={equityBasis ? `مبنای محاسبه از سرور: ${equityBasis}` : undefined}
            tooltip="پاسخ فعلی /portfolio بدون قیمت زنده است؛ equity با بهای تمام‌شده (cost) گزارش می‌شود، نه mark-to-market."
          />
          <Stat label="نقدینگی (cash)" value={money(data?.cash ?? null)} window={equityWindow} source="engine/paper" endpoint="/portfolio" />
          <Stat
            label="سود/زیان تحقق‌یافته"
            value={signedMoney(data?.realized ?? null)}
            tone={tone(data?.realized ?? null)}
            window={equityWindow}
            source="ledger"
            endpoint="/portfolio"
            note={closedTrades === null ? `تعداد معاملات بسته‌شده: ${NOT_RECORDED}` : `تعداد معاملات بسته‌شده: ${num(closedTrades, { max: 0 })} (فقط کاغذی)`}
          />
          <Stat
            label="سود/زیان تحقق‌نیافته"
            value={typeof data?.unrealized === "number" ? signedMoney(data.unrealized) : NOT_RECORDED}
            tone={tone(typeof data?.unrealized === "number" ? data.unrealized : null)}
            window={equityWindow}
            source="mark-to-market"
            endpoint="/portfolio"
            note={`منبع قیمت از سرور: ${text(priceSource, NOT_RECORDED)} — بدون قیمت معتبر، موتور عمداً null می‌فرستد و اینجا عددی ساخته نمی‌شود.`}
          />
          <Stat
            label="سود/زیان امروز (تحقق‌یافته)"
            value={signedMoney(data?.day_pnl ?? null)}
            tone={tone(data?.day_pnl ?? null)}
            window="روز جاری (UTC)"
            source="ledger"
            endpoint="/portfolio"
            note={`سقف زیان روزانه از پیکربندی: ${plainPct(limits?.max_daily_loss_pct ?? null, { max: 2 })} از تخصیص`}
          />
          <Stat
            label="سود/زیان هفته"
            value={typeof data?.week_pnl === "number" ? signedMoney(data.week_pnl) : null}
            tone={tone(typeof data?.week_pnl === "number" ? data.week_pnl : null)}
            window="هفته جاری (UTC)"
            source="ledger"
            endpoint="/portfolio"
          />
          <Stat
            label="درجه‌ی درگیری (exposure)"
            value={plainPct(data?.exposure_pct ?? null, { max: 2 })}
            window={equityWindow}
            source="engine/paper"
            endpoint="/portfolio"
            note={`سقف تخصیص: ${plainPct(limits?.max_account_allocation_pct ?? null, { max: 1})}`}
          />
          <Stat
            label="افت سرمایه از قله"
            value={pct(data?.drawdown_pct ?? null, { max: 2 })}
            tone={drawdownTone}
            window={equityWindow}
            source="engine/paper"
            endpoint="/portfolio"
            note={`سقف drawdown: ${plainPct(drawdownCap ?? null, { max: 1 })}`}
          />
          <Stat
            label="کارمزد پرداخت‌شده (کاغذی)"
            value={money(data?.fees_paid ?? null)}
            window={equityWindow}
            source="ledger"
            endpoint="/portfolio"
            note={`نرخ پیکربندی: ${plainPct(config.data?.taker_fee_pct ?? null, { max: 2 })} taker`}
          />
          <Stat
            label="اسلیپیج تحقق‌یافته"
            value={data && data.slippage_paid != null ? money(data.slippage_paid) : NOT_RECORDED}
            window={equityWindow}
            source="ledger"
            endpoint="/portfolio"
            note={data && data.slippage_paid == null ? "ledger اسلیپیج تحقق‌یافته را جدا ثبت نمی‌کند؛ سقف اسلیپیج فقط در ریسک‌گیت اعمال می‌شود." : undefined}
            tooltip={`سقف اسلیپیج مجاز: ${plainPct(limits?.max_slippage_pct ?? null, { max: 2 })}`}
          />
          <Stat
            label="معاملات امروز / سقف روزانه"
            value={data?.trades_today == null ? null : `${num(data.trades_today, { max: 0 })} / ${limits?.max_trades_per_day != null ? num(limits.max_trades_per_day, { max: 0 }) : NOT_RECORDED}`}
            window="روز جاری (UTC)"
            source="engine/paper"
            endpoint="/portfolio + /config"
          />
          <Stat
            label="خطاهای پیاپی / حد توقف"
            value={
              data?.consecutive_errors == null
                ? null
                : `${num(data.consecutive_errors, { max: 0 })} / ${limits?.stop_on_consecutive_errors != null ? num(limits.stop_on_consecutive_errors, { max: 0 }) : NOT_RECORDED}`
            }
            tone={data?.consecutive_errors && limits?.stop_on_consecutive_errors && data.consecutive_errors >= limits.stop_on_consecutive_errors ? "neg" : "neutral"}
            window={equityWindow}
            source="engine/paper"
            endpoint="/portfolio"
            note="با رسیدن به این حد، موتور به حالت امن می‌رود و سفارش جدید نمی‌سازد."
          />
          <Stat
            label="موقعیت‌های باز"
            value={account.loading ? null : num(positions.length, { max: 0 })}
            window={equityWindow}
            source="engine/paper"
            endpoint="/portfolio"
            note={`سقف هم‌زمان از پیکربندی: ${limits?.max_open_positions != null ? num(limits.max_open_positions, { max: 0 }) : NOT_RECORDED}`}
          />
        </div>
      </section>

      <section className="card" aria-label="موقعیت‌های باز">
        <header>
          <h2>موقعیت‌های باز کاغذی</h2>
          <ToneBadge tone={positions.length > 0 ? "warn" : "neutral"}>
            {positions.length > 0 ? `${num(positions.length, { max: 0 })} موقعیت باز` : "بدون موقعیت باز"}
          </ToneBadge>
        </header>
        <StateBlocks
          state={account}
          emptyLabel={NO_DATA}
          emptyHint="پاسخی از /portfolio دریافت نشد."
        >
          {(view) => {
            const rows = (view.positions ?? []) as PositionExtra[];
            return (
              <>
                <Table
                  rows={rows}
                  getRowId={(row, index) => `pos-${row.symbol}-${String(index)}`}
                  caption="موقعیت‌های باز در حالت PAPER با حد ضرر/هدف ثبت‌شده در تصمیم"
                  emptyLabel="۰ موقعیت باز"
                  emptyHint="در حال حاضر هیچ موقعیت باز کاغذی ثبت نشده است — صفر واقعی است، نه داده نمونه."
                  columns={positionColumns}
                  defaultSort={{ key: "symbol", dir: "asc" }}
                />
                <ProvenanceNote provenance={(view as PortfolioExtra).provenance ?? null} fallbackKind="paper" extra="/portfolio — positions[] خام سرور" />
                <p className="stat-note">
                  «قیمت مارک» و «تحقق‌نیافته» وقتی «ثبت نشده»اند که سرور قیمت زنده‌ای برای mark نفرستاده است؛ این
                  کمبود عمداً با عدد حدسی پر نمی‌شود. stop/target همان مقادیر مصوب ریسک‌گیت‌اند.
                </p>
              </>
            );
          }}
        </StateBlocks>
      </section>

      <section className="grid two" aria-label="روند سرمایه‌ی کاغذی">
        <div className="card">
          <header>
            <h2>منحنی سرمایه (کاغذی)</h2>
            <Badge kind="paper" />
          </header>
          <StateBlocks state={equity} emptyLabel={NO_DATA} emptyHint="هنوز snapshot حساب کاغذی در portfolio_snapshots ثبت نشده است.">
            {(curve) => (
              <>
                <LineChart
                  series={equitySeries}
                  windowLabel={curve.provenance?.window ?? null}
                  sourceLabel={curve.provenance?.source ?? "engine"}
                  unit="USDT"
                  ariaLabel="منحنی سرمایه کاغذی"
                />
                <ProvenanceNote provenance={curve.provenance ?? null} fallbackKind="paper" extra="/equity-curve?mode=PAPER" />
              </>
            )}
          </StateBlocks>
        </div>
        <div className="card">
          <header>
            <h2>افت سرمایه از قله (کاغذی)</h2>
            <Badge kind="paper" />
          </header>
          <StateBlocks state={equity} emptyLabel={NO_DATA} emptyHint="برای رسم drawdown به snapshot بیشتری نیاز است.">
            {(curve) => (
              <>
                <DrawdownChart
                  points={(curve.points ?? []).map((p) => [p[0], p[2] ?? null] as [string | number, number | null])}
                  windowLabel={curve.provenance?.window ?? null}
                  sourceLabel={curve.provenance?.source ?? "engine"}
                />
                <ProvenanceNote provenance={curve.provenance ?? null} fallbackKind="paper" extra="/equity-curve?mode=PAPER" />
              </>
            )}
          </StateBlocks>
        </div>
      </section>

      <p className="notice warn">
        همه‌ی اعداد این صفحه مربوط به حساب کاغذی با موجودی فرضی است. اینجا سودِ بازار زنده نمایش داده نمی‌شود و
        هیچ‌کدام از موقعیت‌های باز به‌صورت خودکار با دستور واقعی بسته نمی‌شوند.
      </p>
    </>
  );
}
