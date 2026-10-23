import { Fragment, useMemo, useState } from "react";
import {
  getAnalysis,
  getConfig,
  getDatasets,
  unwrap,
  url,
  useAsync,
  type AnalysisView,
  type Provenance,
} from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import type { Tone } from "../components/Badge";
import Filters from "../components/Filters";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks, { EmptyBlock, ErrorBlock, LoadingBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import CandleChart from "../charts/CandleChart";
import { NO_DATA, dateTime, num, text } from "../format";

/**
 * `/analysis` answers with `{symbol, timeframe, analysis, provenance}` where
 * `analysis` is either the object below or the string `"no_data"`. The shared
 * `AnalysisView` type in client.ts describes the flat variant, so both shapes are
 * normalised here (a page may not edit client.ts).
 */
interface RawAnalysis {
  regime?: string | null;
  summary?: string | null;
  bullish?: string[] | null;
  bearish?: string[] | null;
  indicators?: Record<string, number | string | null> | null;
  scenarios?: Record<string, string> | null;
  volume_state?: string | null;
  invalidation?: string | null;
}

interface AnalysisEnvelope {
  symbol?: string | null;
  timeframe?: string | null;
  dataset_id?: string | null;
  analysis?: RawAnalysis | "no_data" | null;
  provenance?: Provenance | null;
}

interface BarsEnvelope {
  dataset_id?: string | null;
  symbol?: string | null;
  timeframe?: string | null;
  bars?: { t: number; open: number; high: number; low: number; close: number; base_volume?: number | null; quote_volume?: number | null }[];
  overlays?: { label: string; points: (number | null)[] }[];
  total_bars?: number | null;
  provenance?: Provenance | null;
}

const NO_DATASET = "__live__";
const TIMEFRAMES = ["15m", "1h", "4h", "1d"];

/** `getDatasetBars()` in client.ts cannot request `overlays=true`; built from the exported primitives. */
async function getBarsWithOverlays(datasetId: string, limit: number, signal: AbortSignal): Promise<BarsEnvelope> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(datasetId)}/bars`, { limit, overlays: true }), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  return unwrap<BarsEnvelope>(response);
}

function resolveAnalysis(view: AnalysisView | null): { noData: boolean; data: RawAnalysis | null } {
  if (!view) return { noData: true, data: null };
  const envelope = view as unknown as AnalysisEnvelope;
  const nested = envelope.analysis;
  if (nested === "no_data" || nested === null || nested === undefined) {
    if (nested === "no_data" || nested === null) return { noData: true, data: null };
    const flat = view as AnalysisView;
    const indicators = flat.indicators ?? {};
    if (flat.regime == null && flat.summary == null && Object.keys(indicators).length === 0) return { noData: true, data: null };
    return {
      noData: false,
      data: {
        regime: flat.regime ?? null,
        summary: flat.summary ?? null,
        bullish: flat.signals?.bullish ?? null,
        bearish: flat.signals?.bearish ?? null,
        indicators,
        scenarios: flat.scenarios ?? null,
        volume_state: flat.volume_state ?? null,
        invalidation: flat.invalidation ?? null,
      },
    };
  }
  if (typeof nested === "object") return { noData: false, data: nested };
  return { noData: true, data: null };
}

const REGIME_FA: Record<string, string> = {
  TRENDING_UP: "روند صعودی",
  TRENDING_DOWN: "روند نزولی",
  RANGING: "نوسانی/بدون روند",
  HIGH_VOLATILITY: "نوسان بالا",
  UNKNOWN: "نامشخص — داده کافی نیست",
};

function regimeBadge(regime: string | null | undefined): { label: string; tone: Tone } {
  const key = (regime ?? "").toUpperCase();
  const fa = REGIME_FA[key];
  const label = fa ? (key === "UNKNOWN" ? fa : `${fa} (${key})`) : regime ? `${regime}` : NO_DATA;
  if (key === "TRENDING_UP") return { label, tone: "pos" };
  if (key === "TRENDING_DOWN") return { label, tone: "neg" };
  if (key === "HIGH_VOLATILITY" || key === "UNKNOWN") return { label, tone: "warn" };
  if (key === "RANGING") return { label, tone: "info" };
  return { label, tone: "neutral" };
}

const VOLUME_FA: Record<string, string> = {
  ok: "حجم کافی",
  weak: "حجم ضعیف",
  collapsing: "حجم در حال فروپاشی",
  unknown: "نامشخص",
};

function volumeBadge(state: string | null | undefined): { label: string; tone: Tone } {
  const key = (state ?? "").toLowerCase();
  const label = VOLUME_FA[key] ?? (state ? state : NO_DATA);
  if (key === "ok") return { label, tone: "pos" };
  if (key === "weak") return { label, tone: "warn" };
  if (key === "collapsing") return { label, tone: "neg" };
  return { label, tone: "neutral" };
}

/** Persian gloss for the indicator keys the strategy actually returns. */
const INDICATOR_FA: Record<string, string> = {
  price: "قیمت آخرین کندل",
  ema_fast: "میانگین نمایی سریع",
  ema_slow: "میانگین نمایی کند",
  rsi: "شاخص قدرت نسبی",
  atr: "دامنه‌ی واقعی متوسط",
  atr_pct: "ATR نسبت به قیمت",
  realized_vol_pct_per_bar: "نوسان تحویلی هر کندل",
  trend_slope_pct_per_bar: "شیب روند هر کندل",
  spread_pct: "اسپرد فعلی",
  recent_volume3: "میانگین حجم سه کندل اخیر",
  avg_volume20: "میانگین حجم بیست کندل",
  volume_state: "وضعیت حجم",
  bars_used: "کندل‌های استفاده‌شده",
  strategy_version: "نسخه‌ی استراتژی",
};

interface IndicatorRow {
  key: string;
  label: string;
  value: number | string | null;
}

function indicatorRows(indicators: Record<string, number | string | null> | null | undefined): IndicatorRow[] {
  return Object.entries(indicators ?? {}).map(([key, value]) => ({
    key,
    label: INDICATOR_FA[key] ?? (key.startsWith("ema") ? "میانگین نمایی" : key.startsWith("rsi") ? "شاخص قدرت نسبی" : key.startsWith("atr") ? "دامنه‌ی واقعی متوسط" : key),
    value: value === undefined ? null : value,
  }));
}

function formatIndicatorValue(value: number | string | null): string {
  if (value === null) return NO_DATA;
  if (typeof value === "number") return num(value, { max: 6 });
  return VOLUME_FA[String(value).toLowerCase()] ?? String(value);
}

export default function Analysis() {
  const config = useAsync((signal) => getConfig(signal), []);
  const datasets = useAsync((signal) => getDatasets(signal), []);

  const [symbolInput, setSymbolInput] = useState("");
  const [timeframeInput, setTimeframeInput] = useState("");
  const [datasetInput, setDatasetInput] = useState("");
  const [limit, setLimit] = useState("300");

  const rows = datasets.data ?? [];
  const symbolOptions = useMemo(() => {
    const set = new Set<string>();
    for (const symbol of config.data?.symbols ?? []) set.add(symbol);
    for (const row of rows) set.add(row.symbol);
    return [...set].sort((a, b) => a.localeCompare(b, "en"));
  }, [config.data, rows]);

  const timeframeOptions = useMemo(() => {
    const set = new Set<string>(TIMEFRAMES);
    for (const row of rows) set.add(row.timeframe);
    if (config.data?.timeframe) set.add(config.data.timeframe);
    return [...set];
  }, [config.data, rows]);

  const activeSymbol = symbolInput || config.data?.symbols?.[0] || symbolOptions[0] || "";
  const activeTimeframe =
    timeframeInput || rows.find((row) => row.symbol === activeSymbol)?.timeframe || config.data?.timeframe || "4h";

  const datasetOptions = useMemo(
    () => rows.filter((row) => activeSymbol === "" || row.symbol === activeSymbol),
    [rows, activeSymbol],
  );
  const chosenDataset = datasetOptions.find((row) => row.id === datasetInput) ?? null;
  const usesStoredDataset = datasetInput !== NO_DATASET;
  const activeDatasetId = datasetInput === NO_DATASET ? "" : chosenDataset?.id ?? datasetOptions[0]?.id ?? "";

  const analysis = useAsync(async (signal) => {
    if (activeSymbol === "") return null;
    return getAnalysis({ symbol: activeSymbol, timeframe: activeTimeframe, dataset_id: activeDatasetId || null }, signal);
  }, [activeSymbol, activeTimeframe, activeDatasetId]);

  const bars = useAsync(async (signal) => {
    if (activeDatasetId === "") return null;
    return getBarsWithOverlays(activeDatasetId, Number(limit), signal);
  }, [activeDatasetId, limit]);

  const resolved = resolveAnalysis(analysis.data);
  const envelope = (analysis.data ?? {}) as unknown as AnalysisEnvelope;
  const provenance = envelope.provenance ?? null;
  const view = resolved.data;
  const regime = regimeBadge(view?.regime);
  const volume = volumeBadge(view?.volume_state);
  const indicators = indicatorRows(view?.indicators);
  const barsUsed = view?.indicators?.bars_used;
  const scenarios = Object.entries(view?.scenarios ?? {});
  const bullish = view?.bullish ?? [];
  const bearish = view?.bearish ?? [];

  const barsWindow =
    bars.data?.provenance?.window ??
    (bars.data?.bars && bars.data.bars.length > 0
      ? `${dateTime(bars.data.bars[0]?.t)} ← ${dateTime(bars.data.bars[bars.data.bars.length - 1]?.t)}`
      : null);

  const indicatorColumns: Column<IndicatorRow>[] = [
    { key: "key", header: "کلید (همان پاسخ API)", render: (row) => <span className="num">{row.key}</span>, sortValue: (row) => row.key },
    { key: "label", header: "توضیح", render: (row) => row.label, sortValue: (row) => row.label },
    {
      key: "value",
      header: "مقدار",
      numeric: true,
      sortValue: (row) => (typeof row.value === "number" ? row.value : null),
      render: (row) => <span className={typeof row.value === "number" ? "num" : "rtl-text"}>{formatIndicatorValue(row.value)}</span>,
    },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>تحلیل بازار</h1>
          <p className="mute">
            آخرین خروجی <code>analyze()</code> از <code>/analysis</code>؛ توصیف وضعیت فعلی داده‌ی بسته‌شده است، نه پیش‌بینی قیمت. هر فیلدی که در پاسخ
            نباشد «داده‌ای موجود نیست» می‌ماند.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={config.data?.mode ?? null} />
          <Badge kind="history" label="تاریخچه / داده‌ی ورودی" />
          <button type="button" className="btn small" onClick={analysis.reload} aria-label="خواندن دوباره‌ی تحلیل">
            خواندن دوباره
          </button>
        </div>
      </div>

      <Filters
        fields={[
          {
            key: "symbol",
            label: "نماد",
            type: "select",
            value: activeSymbol,
            onChange: (value) => {
              setSymbolInput(value);
              setDatasetInput("");
            },
            options: symbolOptions.map((symbol) => ({ value: symbol, label: symbol })),
            disabled: symbolOptions.length === 0,
            hint: "نمادهای پیکربندی از /config",
          },
          {
            key: "timeframe",
            label: "تایم‌فریم",
            type: "select",
            value: activeTimeframe,
            onChange: setTimeframeInput,
            options: timeframeOptions.map((tf) => ({ value: tf, label: tf })),
          },
          {
            key: "dataset",
            label: "مجموعه‌داده",
            type: "select",
            value: usesStoredDataset ? activeDatasetId : NO_DATASET,
            onChange: setDatasetInput,
            options: [
              ...(datasetOptions.length === 0 ? [{ value: "", label: "مجموعه‌داده‌ای ذخیره نشده است" }] : []),
              ...datasetOptions.map((row) => ({ value: row.id, label: `${row.id} · ${row.timeframe} · ${num(row.bars, { max: 0 })} کندل` })),
              { value: NO_DATASET, label: "بدون مجموعه‌داده (درخواست تازه از صرافی)" },
            ],
            hint: "با مجموعه‌داده‌ی ذخیره‌شده، فقط داده‌ی معتبرسنجیده‌شده خوانده می‌شود",
          },
          {
            key: "limit",
            label: "کندل نمودار",
            type: "select",
            value: limit,
            onChange: setLimit,
            options: [
              { value: "200", label: "۲۰۰" },
              { value: "300", label: "۳۰۰" },
              { value: "500", label: "۵۰۰" },
              { value: "1000", label: "۱۰۰۰" },
            ],
          },
        ]}
        onReset={() => {
          setSymbolInput("");
          setTimeframeInput("");
          setDatasetInput("");
        }}
        resultLabel={`تحلیل ${text(activeSymbol)} روی ${text(activeTimeframe)} — ${
          activeDatasetId ? `مجموعه‌داده‌ی ${activeDatasetId}` : "دریافت تازه از صرافی"
        }`}
      />

      <section className="grid two" aria-label="وضعیت بازار">
        <div className="card">
          <header>
            <h2>وضعیت شناسایی‌شده</h2>
            <ToneBadge tone={regime.tone}>{regime.label}</ToneBadge>
          </header>
          {analysis.error ? (
            <ErrorBlock message={analysis.error} onRetry={analysis.reload} />
          ) : analysis.loading ? (
            <LoadingBlock label="خواندن /analysis…" />
          ) : resolved.noData ? (
            <EmptyBlock
              label={NO_DATA}
              hint="تحلیلی برای این نماد/تایم‌فریم برگشتنی نیست: یا داده‌ای ذخیره نشده و یا اتصال داده در دسترس نیست. در حالت fail-closed هیچ عددی ساخته نمی‌شود."
            />
          ) : (
            <>
              <dl className="kv">
                <dt>خلاصه‌ی سیستم</dt>
                <dd>{text(view?.summary)}</dd>
                <dt>نماد / تایم‌فریم</dt>
                <dd>{`${text(envelope.symbol ?? analysis.data?.symbol)} · ${text(envelope.timeframe ?? analysis.data?.timeframe)}`}</dd>
                <dt>مجموعه‌داده‌ی ورودی</dt>
                <dd>{text(activeDatasetId || null, "دریافت تازه از صرافی")}</dd>
                <dt>کندل‌های استفاده‌شده</dt>
                <dd>{num(typeof barsUsed === "number" ? barsUsed : null, { max: 0 })}</dd>
                <dt>وضعیت حجم</dt>
                <dd className="rtl-text">{volume.label}</dd>
                <dt>بشرط باطل‌شدن</dt>
                <dd className="rtl-text">{text(view?.invalidation)}</dd>
              </dl>
              <ProvenanceNote provenance={provenance} fallbackKind="history" extra="/analysis" />
            </>
          )}
        </div>

        <div className="card">
          <header>
            <h2>سیگنال‌های ثبت‌شده</h2>
            <Badge kind="history" label="تاریخچه" />
          </header>
          {resolved.noData || analysis.error ? (
            <EmptyBlock label={NO_DATA} hint="بدون تحلیل موفق، سیگنالی برای نمایش نیست." />
          ) : (
            <>
              <h3 className="tone-pos">صعودی ({num(bullish.length, { max: 0 })})</h3>
              {bullish.length === 0 ? (
                <p className="mute">در پاسخ API هیچ سیگنال صعودی ثبت نشده است.</p>
              ) : (
                <ul>
                  {bullish.map((item, index) => (
                    <li key={`bull-${String(index)}`} className="tone-pos">
                      {item}
                    </li>
                  ))}
                </ul>
              )}
              <h3 className="tone-neg">نزولی ({num(bearish.length, { max: 0 })})</h3>
              {bearish.length === 0 ? (
                <p className="mute">در پاسخ API هیچ سیگنال نزولی ثبت نشده است.</p>
              ) : (
                <ul>
                  {bearish.map((item, index) => (
                    <li key={`bear-${String(index)}`} className="tone-neg">
                      {item}
                    </li>
                  ))}
                </ul>
              )}
              <p className="mute">
                متن سیگنال‌ها عیناً از پاسخ <code>/analysis</code> است و UI آن را بازنویسی یا تعبیر نمی‌کند؛ تعداد سیگنال به‌تنهایی توجیه‌گر ورود نیست.
              </p>
            </>
          )}
        </div>
      </section>

      <section className="grid two" aria-label="اندیکاتورها و سناریوها">
        <div className="card">
          <header>
            <h2>اندیکاتورها</h2>
            <Badge kind="history" label="مقادیر لحظه‌ای" />
          </header>
          <StateBlocks state={analysis} emptyLabel={NO_DATA} emptyHint="پاسخ تحلیلی موجود نیست.">
            {() =>
              indicators.length === 0 ? (
                <EmptyBlock label={NO_DATA} hint="فیلد indicators در پاسخ خالی برگشته است." />
              ) : (
                <Table
                  rows={indicators}
                  getRowId={(row) => `ind-${row.key}`}
                  caption="مقادیر اندیکاتور از پاسخ /analysis"
                  columns={indicatorColumns}
                  defaultSort={{ key: "key", dir: "asc" }}
                />
              )
            }
          </StateBlocks>
          <p className="mute">
            کلیدهای هم‌معنی (مثلاً <code>ema20</code> و <code>ema_fast</code>) همان‌طور که API می‌فرستد نمایش داده می‌شوند؛ چیزی حذف یا ادغام نمی‌شود.
          </p>
          <ProvenanceNote provenance={provenance} fallbackKind="history" extra="/analysis → indicators" />
        </div>

        <div className="card">
          <header>
            <h2>سناریوها</h2>
            <ToneBadge tone="warn" title="سناریو شرط‌های توصیفی‌اند، نه پیش‌بینی">
              بدون پیش‌بینی
            </ToneBadge>
          </header>
          <StateBlocks state={analysis} emptyLabel={NO_DATA} emptyHint="پاسخ تحلیلی موجود نیست.">
            {() =>
              scenarios.length === 0 ? (
                <EmptyBlock label={NO_DATA} hint="هیچ سناریویی در پاسخ ثبت نشده است." />
              ) : (
                <dl className="kv">
                  {scenarios.map(([key, value]) => (
                    <Fragment key={`sc-${key}`}>
                      <dt>{key === "bullish" ? "صعودی" : key === "bearish" ? "نزولی" : key === "neutral" ? "خنثی" : key}</dt>
                      <dd>{text(value)}</dd>
                    </Fragment>
                  ))}
                </dl>
              )
            }
          </StateBlocks>
          <p className="mute">
            سناریوها «اگر… آنگاه» روی سطوح محاسبه‌شده‌ی اندیکاتور هستند و احتمال یا هدف قیمتی تضمینی نمی‌دهند. شرط باطل‌شدن تحلیل:{" "}
            {text(view?.invalidation)}
          </p>
          <ProvenanceNote provenance={provenance} fallbackKind="history" extra="/analysis → scenarios" />
        </div>
      </section>

      <section className="card" aria-label="کندل‌ها و میانگین‌های نمایی">
        <header>
          <h2>کندل‌ها و EMA — {text(activeSymbol)} {text(activeTimeframe)}</h2>
          <Badge kind="history" label="داده‌ی خام" />
        </header>
        {activeDatasetId === "" ? (
          <EmptyBlock
            label={NO_DATA}
            hint="نمودار کندلی فقط از مجموعه‌داده‌ی ذخیره‌شده رسم می‌شود؛ برای این نماد dataset‌ای انتخاب نشده است. یک مجموعه‌داده از صفحه‌ی «داده‌های تاریخی» بسازید."
          />
        ) : (
          <StateBlocks state={bars} emptyLabel={NO_DATA} emptyHint="کندلی برای این مجموعه‌داده ذخیره نشده است.">
            {(page) => (
              <>
                <CandleChart
                  bars={page?.bars ?? []}
                  overlays={(page?.overlays ?? []).map((overlay) => ({ label: overlay.label, points: overlay.points }))}
                  windowLabel={barsWindow}
                  sourceLabel={page?.provenance?.source ?? null}
                  totalBars={page?.total_bars ?? null}
                />
                <p className="mute">
                  EMAها از <code>/datasets/&#123;id&#125;/bars?overlays=true</code> می‌آیند و جایی که هنوز تعریف نشده‌اند مقدار <code>null</code> است؛
                  نمودار آن نقاط را رسم نمی‌کند (نه صفر).
                </p>
                <ProvenanceNote provenance={page?.provenance ?? null} fallbackKind="history" extra={`/datasets/${text(activeDatasetId)}/bars`} />
              </>
            )}
          </StateBlocks>
        )}
      </section>

      <p className="notice warn">
        این صفحه توصیف وضعیت گذشته/فعلی داده‌ی تاریخچه است. هیچ عددی اینجا پیش‌بینی نرخ برد یا سود نیست؛ تصمیم واقعی باید با ریسک‌گیت، مدیریت سرمایه و
        آزمون برون‌نمونه‌ای گرفته شود — و نتایج بک‌تست در صفحه‌ی «بک‌تست» با برچسب <Badge kind="simulated" label="شبیه‌سازی" /> نمایش داده می‌شود تا با
        داده‌ی تاریخچه یا نتیجه‌ی پیپر اشتباه نشود.
      </p>
    </>
  );
}
