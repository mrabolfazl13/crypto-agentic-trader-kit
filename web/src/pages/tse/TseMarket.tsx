import { useEffect, useMemo, useState } from "react";
import { Landmark, Search } from "lucide-react";
import { getMarketCandles, useAsync, type Bar } from "../../api/client";
import {
  getTseBook,
  getTseInstrument,
  getTseInstruments,
  getTseMarket,
  type TseBookLevel,
  type TseInstrumentRow,
  type TseSessionState,
} from "../../api/tseClient";
import CandleChart from "../../charts/CandleChart";
import Badge, { ToneBadge } from "../../components/Badge";
import ProfileBoard from "../../components/ProfileBoard";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlocks";
import Table, { type Column } from "../../components/Table";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { NO_DATA, dateTime, num, pct, signed, text } from "../../format";

const TEHRAN_FORMAT = new Intl.DateTimeFormat("fa-IR-u-nu-latn", {
  dateStyle: "short",
  timeStyle: "short",
  timeZone: "Asia/Tehran",
});

function tehran(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return NO_DATA;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? NO_DATA : TEHRAN_FORMAT.format(date);
}

const SESSION_TONE: Record<TseSessionState, "pos" | "neutral" | "info"> = {
  open: "pos",
  closed: "neutral",
  pre_open: "info",
};

function compactStat(label: string, value: string, hint?: string) {
  return (
    <div className="card stat is-compact" key={label}>
      <div className="stat-label">{label}</div>
      <div className="stat-value num">{value}</div>
      {hint ? <div className="stat-note">{hint}</div> : null}
    </div>
  );
}

/**
 * Read-only view of the Tehran exchange. It shows the exchange's own board
 * values and the recorded session clock; it cannot place, modify or cancel an
 * order and it never falls back to a fabricated price.
 */
export default function TseMarket() {
  const market = useAsync((signal) => getTseMarket(1, signal), []);
  const instruments = useAsync((signal) => getTseInstruments(500, signal), []);

  const rows = instruments.data?.instruments ?? [];
  const [symbol, setSymbol] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (rows.length === 0) return;
    if (!symbol || !rows.some((row) => row.ins_code === symbol)) setSymbol(rows[0]?.ins_code ?? null);
  }, [rows, symbol]);

  const detail = useAsync(
    async (signal) => (symbol ? getTseInstrument(symbol, signal) : null),
    [symbol],
  );
  const book = useAsync(
    async (signal) => (symbol ? getTseBook(symbol, signal) : null),
    [symbol],
  );
  const candles = useAsync(
    async (signal) => (symbol ? getMarketCandles({ symbol, timeframe: "1d", limit: 240 }, signal) : null),
    [symbol],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      [row.ticker, row.name, row.ins_code, row.isin]
        .filter((value): value is string => Boolean(value))
        .some((value) => value.toLowerCase().includes(needle)),
    );
  }, [rows, query]);

  const selectedRow = rows.find((row) => row.ins_code === symbol) ?? null;
  const quote = detail.data?.quote ?? null;
  const series: Bar[] = useMemo(
    () => (candles.data?.bars ?? []).filter((bar) => bar.closed !== false),
    [candles.data],
  );

  const last = selectedRow?.last_price ?? detail.data?.band.last_price ?? null;
  const yesterday = selectedRow?.price_yesterday ?? detail.data?.band.price_yesterday ?? null;
  const changePct = last !== null && yesterday !== null && yesterday !== 0 ? ((last - yesterday) / yesterday) * 100 : null;

  // TSETMC publishes the index change in index *points*; rendering it with a "%" suffix
  // would print a number no market has ever moved by. The percent is derived from the
  // two published values instead.
  const indexChange = market.data?.board.index_change ?? null;
  const indexLast = market.data?.board.index_last_value ?? null;
  const indexPrevious = indexChange !== null && indexLast !== null ? indexLast - indexChange : null;
  const indexChangePct =
    indexChange !== null && indexPrevious !== null && indexPrevious !== 0 ? (indexChange / indexPrevious) * 100 : null;

  const columns = useMemo<Column<TseInstrumentRow>[]>(
    () => [
      {
        key: "ticker",
        header: "نماد",
        sortValue: (row) => row.ticker,
        render: (row) => (
          <div className="flex flex-col gap-0.5">
            <strong>{text(row.ticker)}</strong>
            <span className="text-[11px] text-mute num">{row.ins_code}</span>
          </div>
        ),
      },
      {
        key: "last",
        header: "قیمت آخر",
        numeric: true,
        sortValue: (row) => row.last_price,
        render: (row) => num(row.last_price, { max: 0 }),
      },
      {
        key: "change",
        header: "تغییر",
        numeric: true,
        sortValue: (row) => row.change_pct,
        render: (row) => (
          <span className={row.change_pct === null ? "mute" : row.change_pct > 0 ? "tone-pos" : row.change_pct < 0 ? "tone-neg" : ""}>
            {pct(row.change_pct)}
          </span>
        ),
      },
      { key: "bid", header: "خرید", numeric: true, sortValue: (row) => row.best_bid, render: (row) => num(row.best_bid, { max: 0 }) },
      { key: "ask", header: "فروش", numeric: true, sortValue: (row) => row.best_ask, render: (row) => num(row.best_ask, { max: 0 }) },
      {
        key: "volume",
        header: "حجم (سهم)",
        numeric: true,
        sortValue: (row) => row.volume_shares,
        render: (row) => num(row.volume_shares, { max: 0 }),
      },
      {
        key: "value",
        header: "ارزش (میلیارد ریال)",
        numeric: true,
        sortValue: (row) => row.trade_value_rial,
        render: (row) => (row.trade_value_rial === null ? NO_DATA : num(row.trade_value_rial / 1_000_000_000, { max: 1 })),
      },
    ],
    [],
  );

  const bookColumns = useMemo<Column<TseBookLevel>[]>(
    () => [
      { key: "level", header: "سطح", numeric: true, render: (row) => num(row.level, { max: 0 }) },
      { key: "bid_orders", header: "سفارش خرید", numeric: true, render: (row) => num(row.bid_orders, { max: 0 }) },
      { key: "bid_qty", header: "حجم خرید", numeric: true, render: (row) => num(row.bid_qty, { max: 0 }) },
      { key: "bid", header: "قیمت خرید", numeric: true, render: (row) => num(row.bid_price, { max: 0 }) },
      { key: "ask", header: "قیمت فروش", numeric: true, render: (row) => num(row.ask_price, { max: 0 }) },
      { key: "ask_qty", header: "حجم فروش", numeric: true, render: (row) => num(row.ask_qty, { max: 0 }) },
      { key: "ask_orders", header: "سفارش فروش", numeric: true, render: (row) => num(row.ask_orders, { max: 0 }) },
    ],
    [],
  );

  return (
    <div className="flex flex-col gap-4">
      <Card aria-label="وضعیت بازار بورس تهران">
        <CardHeader>
          <div className="flex flex-col gap-0.5">
            <CardTitle className="flex items-center gap-2">
              <Landmark size={17} className="text-info" aria-hidden="true" />
              بورس تهران — تابلوی TSETMC
            </CardTitle>
            <p className="m-0 text-[12px] text-mute">
              فقط خواندنی: قیمت‌ها و کتاب سفارش از تابلو عمومی بورس خوانده می‌شوند؛ این صفحه هیچ سفارشی نمی‌سازد، تغییر نمی‌دهد و لغو نمی‌کند.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {market.data ? (
              <ToneBadge tone={SESSION_TONE[market.data.session.state]}>{market.data.session.label}</ToneBadge>
            ) : null}
            <Badge kind="history" label="تاریخچه" source="TSETMC" />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {market.loading ? <LoadingBlock label="خواندن تابلوی بورس…" /> : null}
          {market.error ? <ErrorBlock message={market.error} onRetry={market.reload} /> : null}
          {market.data ? (
            <>
              <div className="grid stats">
                {compactStat("شاخص کل", num(market.data.board.index_last_value, { max: 0 }), "TSETMC، جریان بازار")}
                {compactStat(
                  "تغییر شاخص",
                  signed(indexChange, { max: 2 }),
                  indexChangePct === null
                    ? "واحد شاخص، نسبت به مقدار پیشین"
                    // LRI/PDI isolate the signed number: inside an RTL sentence a bare
                    // "−1.19%" reorders to "1.19%−" and a fall reads like a gain.
                    : `واحد شاخص · معادل ⁦${pct(indexChangePct, { max: 2 })}⁩ از ${num(indexPrevious, { max: 0 })}`,
                )}
                {compactStat("تعداد معاملات", num(market.data.board.trade_count, { max: 0 }), "آخرین روز معاملاتی")}
                {compactStat("ارزش معاملات", num(
                  market.data.board.value_rial === null ? null : market.data.board.value_rial / 1_000_000_000,
                  { max: 1 },
                ), "میلیارد ریال")}
                {compactStat("نمادهای تابلو", num(market.data.board_size, { max: 0 }), "پس از پالایش سهام عادی")}
                {compactStat("مجاز در پیکربندی", num(market.data.allowlist.length, { max: 0 }), "قابل معامله در این نصب")}
              </div>
              <p className="stat-meta">
                <span>روز معاملاتی: {text(String(market.data.board.activity_date ?? ""))}</span>
                <span>به‌وقت تهران: {tehran(market.data.session.tehran_now)}</span>
                <span>مرجع تازگی: {tehran(market.data.freshness_reference)}</span>
                <span>گشایش بعدی: {tehran(market.data.session.next_open)}</span>
              </p>
              <p className="stat-meta">
                <span>منبع: TSETMC (تک‌منبع)</span>
                <span>واحد قیمت: {market.data.currency}</span>
                <span>واحد حجم: {market.data.quantity_unit}</span>
              </p>
            </>
          ) : null}
        </CardContent>
      </Card>

      {instruments.loading ? <LoadingBlock label="خواندن فهرست نمادها…" /> : null}
      {instruments.error ? <ErrorBlock message={instruments.error} onRetry={instruments.reload} /> : null}
      {instruments.data && rows.length === 0 ? (
        <EmptyBlock
          label={NO_DATA}
          hint="هیچ نمادی در جهان معاملاتی بورس نیست؛ universe_tse.json را با tools/build_tse_universe.py بسازید."
        />
      ) : null}

      {instruments.data && rows.length > 0 ? (
        <div className="grid market">
          <div className="flex min-w-0 flex-col gap-3">
            <Card aria-label="نمودار و مشخصات نماد">
              <CardHeader>
                <div className="flex flex-col gap-0.5">
                  <CardTitle>{text(selectedRow?.ticker ?? detail.data?.display.ticker)}</CardTitle>
                  <p className="m-0 text-[12px] text-mute">
                    {text(selectedRow?.name ?? detail.data?.display.name)} · ISIN {text(selectedRow?.isin ?? detail.data?.display.isin)}
                  </p>
                </div>
                <Badge kind="history" label="تاریخچه" source={symbol} />
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {detail.loading ? <LoadingBlock label="خواندن قیمت و بازه‌ی مجاز…" /> : null}
                {detail.error ? <ErrorBlock message={detail.error} onRetry={detail.reload} /> : null}
                {detail.data ? (
                  <>
                    <div className="grid stats">
                      {compactStat("قیمت آخر", num(last, { max: 0 }), "ریال")}
                      {compactStat("تغییر روزانه", pct(changePct), "نسبت به قیمت دیروز")}
                      {compactStat("کف مجاز", num(detail.data.band.band_low, { max: 0 }), "بازه‌ی نوسان بورس")}
                      {compactStat("سقف مجاز", num(detail.data.band.band_high, { max: 0 }), "بازه‌ی نوسان بورس")}
                      {compactStat(
                        "بهترین خرید",
                        num(quote?.bid ?? null, { max: 0 }),
                        quote ? `اسپرد ${pct(quote.spread_pct)}` : "تابلو قیمت دوطرفه ندارد",
                      )}
                      {compactStat(
                        "بهترین فروش",
                        num(quote?.ask ?? null, { max: 0 }),
                        `حداقل حجم ${num(detail.data.venue_rules.lot_size, { max: 0 })} سهم`,
                      )}
                    </div>
                    {quote === null ? (
                      <p className="notice" role="status">
                        تابلوی TSETMC برای این نماد قیمت دوطرفه (خرید و فروش هم‌زمان) منتشر نکرده است؛ نماد ممکن است متوقف باشد یا فقط یک سمت کتاب پر باشد.
                        بازه‌ی مجاز، قیمت آخر و کندل‌های روزانه همچنان از همان منبع خوانده می‌شوند و عددی ساخته نمی‌شود.
                        {detail.data.quote_note ? (
                          <span className="text-[11px] text-mute num" dir="ltr"> ({detail.data.quote_note})</span>
                        ) : null}
                      </p>
                    ) : null}
                    <p className="stat-meta">
                      <span>زمان قیمت (تهران): {tehran(quote?.ts ?? null)}</span>
                      <span>منبع: {text(quote?.source ?? "TSETMC")}</span>
                      <span>مبنای بازه: {text(detail.data.band.band_source)}</span>
                    </p>
                  </>
                ) : null}

                {candles.loading ? <LoadingBlock label="خواندن کندل‌های روزانه…" /> : null}
                {candles.error ? <ErrorBlock message={candles.error} onRetry={candles.reload} /> : null}
                {!candles.loading && !candles.error ? (
                  <CandleChart
                    bars={series}
                    levels={[
                      { label: "کف مجاز", value: detail.data?.band.band_low, tone: "stop" },
                      { label: "سقف مجاز", value: detail.data?.band.band_high, tone: "target" },
                    ]}
                    windowLabel={
                      series.length > 0
                        ? `${dateTime(series[0]?.t)} → ${dateTime(series[series.length - 1]?.t)}`
                        : null
                    }
                    sourceLabel={candles.data?.provenance.source ?? null}
                    totalBars={candles.data?.count ?? series.length}
                  />
                ) : null}
                {candles.data ? (
                  <p className="stat-meta">
                    <span>کندل‌های بسته: {num(candles.data.closed_count, { max: 0 })}</span>
                    <span>دریافت: {dateTime(candles.data.provenance.fetched_at)}</span>
                    <span>کش: {candles.data.provenance.cached ? "بله" : "خیر"}</span>
                    <span>زمان محور نمودار UTC است؛ جلسه‌ی بورس ۰۹:۰۰ تهران آغاز می‌شود.</span>
                  </p>
                ) : null}
              </CardContent>
            </Card>

            <Card aria-label="کتاب سفارش">
              <CardHeader>
                <CardTitle>کتاب سفارش — پنج سطح نخست</CardTitle>
                <Badge kind="history" label="تاریخچه" source={symbol} />
              </CardHeader>
              <CardContent>
                {book.loading ? <LoadingBlock label="خواندن کتاب سفارش…" /> : null}
                {book.error ? <ErrorBlock message={book.error} onRetry={book.reload} /> : null}
                {book.data ? (
                  <>
                    <Table
                      columns={bookColumns}
                      rows={book.data.levels}
                      getRowId={(row, index) => `${row.level ?? index}`}
                      caption="بهترین سفارش‌های خرید و فروش در لحظه‌ی ثبت‌شده"
                      emptyLabel={NO_DATA}
                      emptyHint={book.data.note ?? "کتاب سفارش برای این نماد خالی برگشت."}
                    />
                    {book.data.note && book.data.count > 0 ? (
                      <p className="stat-meta">
                        <span>{text(book.data.note)}</span>
                        <span>سطحی که سفارش ندارد قیمت ندارد؛ به‌جای آن عدد صفر چاپ نمی‌شود.</span>
                      </p>
                    ) : null}
                  </>
                ) : null}
              </CardContent>
            </Card>
          </div>

          <div className="flex min-w-0 flex-col gap-3">
            <Card aria-label="انتخاب نماد بورس">
              <CardHeader>
                <CardTitle>نمادهای مجاز</CardTitle>
                <span className="text-[12px] text-mute num">{num(rows.length, { max: 0 })}</span>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                <label className="relative">
                  <span className="sr-only">جستجوی نماد بورس</span>
                  <Search size={14} className="pointer-events-none absolute end-3 top-1/2 -translate-y-1/2 text-mute" aria-hidden="true" />
                  <input
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="نماد، نام یا کد ISIN…"
                    className="h-9 w-full rounded-md border border-line bg-elev px-3 pe-8 text-[13px] text-ink outline-none placeholder:text-mute focus:border-accent"
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[12px] text-mute">نماد نمایش‌داده‌شده</span>
                  <select
                    value={symbol ?? ""}
                    onChange={(event) => setSymbol(event.target.value || null)}
                    className="num h-9 w-full rounded-md border border-line bg-elev px-2 text-[13px] text-ink outline-none focus:border-accent"
                  >
                    {filtered.map((row) => (
                      <option key={row.ins_code} value={row.ins_code}>
                        {row.ticker ?? row.ins_code} — {row.ins_code}
                      </option>
                    ))}
                  </select>
                </label>
                <p className="m-0 text-[11px] text-mute">
                  فهرست از universe_tse.json و پالایش سهام عادی می‌آید؛ نمادهای خارج از پیکربندی پذیرفته نمی‌شوند.
                </p>
              </CardContent>
            </Card>

            <Card aria-label="فهرست نمادها">
              <CardHeader>
                <CardTitle>تابلوی نمادها</CardTitle>
                <span className="text-[12px] text-mute">{num(filtered.length, { max: 0 })} نتیجه</span>
              </CardHeader>
              <CardContent>
                <Table
                  columns={columns}
                  rows={filtered}
                  getRowId={(row) => row.ins_code}
                  caption="قیمت‌های آخرین روز معاملاتی؛ برای دیدن جزئیات روی سطر بزنید"
                  onRowClick={(row) => setSymbol(row.ins_code)}
                  defaultSort={{ key: "value", dir: "desc" }}
                  emptyLabel={NO_DATA}
                  emptyHint="هیچ نمادی با این جستجو پیدا نشد."
                />
              </CardContent>
            </Card>
          </div>
        </div>
      ) : null}

      <ProfileBoard
        market="tse"
        headingLevel="h2"
        title="پروفایل‌های سود و زیان بورس تهران"
        description="همان موتور بک‌تست کریپتو با وجود تقسیم‌شده، اسپرد و کندل روزانه‌ی بورس تهران. هر مرحله سود و زیان ناخالص و خالص خودش را دارد و هیچ سفارشی ساخته نمی‌شود."
      />

      <p className="notice">
        این دیدگاه فقط برای پژوهش است. داده‌ی بورس از یک منبع عمومی می‌آید، ممکن است تأخیر داشته باشد و مبنای سفارش واقعی نیست.
      </p>
    </div>
  );
}
