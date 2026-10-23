import { useEffect, useMemo, useState } from "react";
import { Activity, TrendingDown, TrendingUp } from "lucide-react";
import { getConfig, getMarketCandles, getUniverse, useAsync, type Bar } from "../api/client";
import CandleChart, { type Overlay } from "../charts/CandleChart";
import Badge from "./Badge";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./StateBlocks";
import { Badge as UiBadge } from "./ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { NO_DATA, dateTime, num, plainPct, sinceLabel, text } from "../format";

const WINDOW_BARS = 180;

/** Simple EMA over closes; null while the period is still warming up. */
function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = values.map(() => null);
  if (values.length < period) return out;
  let sum = 0;
  for (let i = 0; i < period; i += 1) sum += values[i] ?? 0;
  let prev = sum / period;
  out[period - 1] = prev;
  const k = 2 / (period + 1);
  for (let i = period; i < values.length; i += 1) {
    const value = values[i] ?? prev;
    prev = value * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function lastBar(bars: Bar[]): Bar | null {
  return bars.length > 0 ? (bars[bars.length - 1] ?? null) : null;
}

/** The chart lists the whole allow-listed universe; candles are read from the exchange. */
export default function MarketOverview() {
  const config = useAsync((signal) => getConfig(signal), []);
  const universe = useAsync((signal) => getUniverse(signal), []);
  const symbols = useMemo(() => universe.data?.symbols ?? config.data?.symbols ?? [], [universe.data, config.data]);
  const timeframe = config.data?.timeframe ?? null;

  const [symbol, setSymbol] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (symbols.length === 0) return;
    if (!symbol || !symbols.includes(symbol)) setSymbol(symbols.includes("BTC_USDT") ? "BTC_USDT" : (symbols[0] ?? null));
  }, [symbols, symbol]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return symbols;
    return symbols.filter((item) => item.toLowerCase().includes(needle));
  }, [symbols, query]);

  const selected = symbol && symbols.includes(symbol) ? symbol : null;
  const candles = useAsync(
    async (signal) => (selected && timeframe ? getMarketCandles({ symbol: selected, timeframe, limit: WINDOW_BARS }, signal) : null),
    [selected, timeframe],
  );

  const series = useMemo(
    () => (candles.data?.bars ?? []).filter((bar) => bar.closed !== false),
    [candles.data],
  );
  const stats = useMemo(() => {
    const closes = series.map((bar) => bar.close);
    const first = closes[0] ?? null;
    const last = closes[closes.length - 1] ?? null;
    const changePct = first !== null && last !== null && first !== 0 ? ((last - first) / first) * 100 : null;
    let high: number | null = null;
    let low: number | null = null;
    for (const bar of series) {
      high = high === null ? bar.high : Math.max(high, bar.high);
      low = low === null ? bar.low : Math.min(low, bar.low);
    }
    return { first, last, changePct, high, low, closes };
  }, [series]);

  const overlays: Overlay[] = useMemo(() => {
    if (series.length < 20) return [];
    return [
      { label: "EMA ۲۰", points: ema(stats.closes, 20), color: "var(--info)" },
      { label: "EMA ۵۰", points: ema(stats.closes, 50), color: "var(--warn)" },
    ];
  }, [series.length, stats.closes]);

  const firstBar = series[0] ?? null;
  const latest = lastBar(series);
  const windowLabel = firstBar && latest ? `${dateTime(firstBar.t)} → ${dateTime(latest.t)}` : null;
  const provenance = candles.data?.provenance ?? null;
  const options = useMemo(() => {
    if (!selected || filtered.includes(selected)) return filtered;
    return [selected, ...filtered];
  }, [filtered, selected]);

  return (
    <Card aria-label="بازار و داده‌ی ذخیره‌شده">
      <CardHeader>
        <div className="flex flex-col gap-0.5">
          <CardTitle className="flex items-center gap-2">
            <Activity size={17} className="text-info" aria-hidden="true" />
            بازار — کندل‌های واقعی صرافی
          </CardTitle>
          <p className="m-0 text-[12px] text-mute">
            هر نماد جهان معاملاتی را انتخاب کنید؛ کندل‌های بسته از Gate خوانده می‌شوند و منبع و زمان دریافت زیر نمودار می‌آید.
          </p>
        </div>
        <Badge kind="history" label="تاریخچه" source={selected} />
      </CardHeader>

      <CardContent className="flex flex-col gap-3">
        {config.loading && universe.loading ? <LoadingBlock label="خواندن جهان معاملاتی…" /> : null}
        {config.error && universe.error ? <ErrorBlock message={config.error ?? universe.error ?? ""} onRetry={config.reload} /> : null}
        {!config.loading && !universe.loading && symbols.length === 0 ? (
          <EmptyBlock label={NO_DATA} hint="فهرست نمادها در پیکربندی خوانده نشد؛ نمودار چیزی نمی‌سازد." />
        ) : null}

        {symbols.length > 0 && selected ? (
          <>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center" aria-label="انتخاب نماد">
              <label className="relative min-w-0 flex-1">
                <span className="sr-only">جستجوی نماد</span>
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="جستجوی نماد…"
                  className="h-9 w-full rounded-md border border-line bg-elev px-3 text-[13px] text-ink outline-none placeholder:text-mute focus:border-accent"
                />
              </label>
              <label className="flex items-center gap-1.5">
                <span className="sr-only">نماد نمایش‌داده‌شده</span>
                <select
                  value={selected}
                  onChange={(event) => setSymbol(event.target.value)}
                  className="num h-9 max-w-[220px] rounded-md border border-line bg-elev px-2 text-[13px] text-ink outline-none focus:border-accent"
                >
                  {options.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <span className="whitespace-nowrap text-[11px] text-mute">
                {`${num(filtered.length, { max: 0 })} از ${num(symbols.length, { max: 0 })} نماد`}
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <UiBadge tone="info">{text(timeframe)}</UiBadge>
              <UiBadge tone="neutral">{`${num(candles.data?.closed_count ?? null, { max: 0 })} کندل بسته`}</UiBadge>
              <UiBadge tone="neutral">{`آخرین کندل: ${sinceLabel(latest?.t ?? null)}`}</UiBadge>
              {provenance ? <UiBadge tone="neutral">{`منبع: ${text(provenance.source)}`}</UiBadge> : null}
            </div>

            <div className="grid grid-cols-2 gap-2 md:grid-cols-4" aria-label="خلاصه‌ی قیمت در پنجره‌ی نمایش">
              <MiniStat label="آخرین بسته" value={num(stats.last, { max: 2 })} unit="USDT" />
              <MiniStat
                label="تغییر پنجره"
                value={plainPct(stats.changePct, { max: 2 })}
                tone={stats.changePct === null ? "neutral" : stats.changePct > 0 ? "pos" : stats.changePct < 0 ? "neg" : "neutral"}
                icon={stats.changePct === null ? undefined : stats.changePct >= 0 ? "up" : "down"}
              />
              <MiniStat label="سقف پنجره" value={num(stats.high, { max: 2 })} unit="USDT" />
              <MiniStat label="کف پنجره" value={num(stats.low, { max: 2 })} unit="USDT" />
            </div>

            {candles.loading ? <div className="skeleton" style={{ height: 280 }} aria-hidden="true" /> : null}
            {candles.error ? <ErrorBlock message={candles.error} onRetry={candles.reload} /> : null}
            {!candles.loading && !candles.error ? (
              <CandleChart
                bars={series}
                overlays={overlays}
                windowLabel={windowLabel}
                sourceLabel={provenance?.source ?? "gate"}
                totalBars={candles.data?.count ?? series.length}
              />
            ) : null}

            {candles.data ? (
              <p className="m-0 text-[11px] text-mute">
                {`دریافت: ${dateTime(provenance?.fetched_at ?? provenance?.generated_at)} · ${provenance?.cached ? "پاسخ از کش کوتاه سرور" : "خوانده‌شده از صرافی"} · EMA ۲۰/۵۰ فقط برای نمایش، در مرورگر محاسبه شده`}
              </p>
            ) : null}
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

function MiniStat({
  label,
  value,
  unit,
  tone = "neutral",
  icon,
}: {
  label: string;
  value: string;
  unit?: string;
  tone?: "pos" | "neg" | "neutral";
  icon?: "up" | "down";
}) {
  return (
    <div className="rounded-lg border border-line bg-surface2 px-3 py-2">
      <div className="flex items-center gap-1 text-[11px] text-mute">
        {icon === "up" ? <TrendingUp size={12} aria-hidden="true" /> : icon === "down" ? <TrendingDown size={12} aria-hidden="true" /> : null}
        {label}
      </div>
      <div className={`num text-[15px] font-bold tone-${tone}`}>
        {value}
        {unit ? <span className="ms-1 text-[11px] font-normal text-mute">{unit}</span> : null}
      </div>
    </div>
  );
}
