import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  describeError,
  fetchDataset,
  getConfig,
  getDatasetQuality,
  getDatasets,
  importDataset,
  unwrap,
  url,
  useAsync,
  type BarsPage,
  type DatasetInfo,
  type Provenance,
} from "../api/client";
import Badge, { ToneBadge } from "../components/Badge";
import Filters from "../components/Filters";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks, { EmptyBlock, LoadingBlock, SuccessBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import CandleChart from "../charts/CandleChart";
import { NO_DATA, dateTime, num, plainPct, text } from "../format";

/**
 * Helpers and types that the shared layer does not expose yet. They are local on
 * purpose: `client.ts` must not be edited from a page.
 *
 * - `QualityReport` is the quality block the backend actually stores
 *   (`closed_bars`, `gaps{}`, `coverage_pct`, `is_stale`, …); the shared
 *   `Quality` type in client.ts uses a different, older key set.
 * - `getDatasetQuality` is typed as `Quality` but the route answers
 *   `{quality, provenance}`, so both shapes are unwrapped here.
 * - `getDatasetBars` in client.ts cannot ask for `overlays=true`, so the candle
 *   request is built from the exported `url()` + `unwrap()` primitives instead.
 */
interface QualityReport {
  source?: string | null;
  origin?: string | null;
  timeframe?: string | null;
  bars?: number | null;
  closed_bars?: number | null;
  dropped_unclosed?: number | null;
  duplicates?: number | null;
  gaps?: { count?: number | null; missing_bars?: number | null; starts?: (number | string)[] } | null;
  impossible_ohlc?: number | null;
  non_positive_prices?: number | null;
  coverage_pct?: number | null;
  expected_bars?: number | null;
  stale_seconds?: number | null;
  is_stale?: boolean | null;
  incomplete?: boolean | null;
  median_spacing?: number | null;
  first_iso?: string | null;
  last_iso?: string | null;
  warnings?: string[] | null;
}

interface QualityEnvelope {
  quality?: QualityReport | null;
  provenance?: Provenance | null;
}

/** The bars route also answers with the real totals; `BarsPage` does not declare them. */
interface BarsEnvelope extends BarsPage {
  total_bars?: number | null;
  returned_bars?: number | null;
}

interface ImportAck {
  dataset_id?: string | null;
  bars?: number | null;
  timeframe?: string | null;
  quality?: QualityReport | null;
  provenance?: Provenance | null;
}

const TIMEFRAMES = ["15m", "1h", "4h", "1d"];

function qualityOf(payload: unknown): QualityReport | null {
  if (!payload || typeof payload !== "object") return null;
  const envelope = payload as QualityEnvelope;
  if (envelope.quality && typeof envelope.quality === "object") return envelope.quality;
  const flat = payload as QualityReport;
  return Object.keys(flat).length > 0 ? flat : null;
}

function provenanceOf(payload: unknown): Provenance | null {
  if (!payload || typeof payload !== "object") return null;
  const candidate = (payload as QualityEnvelope).provenance;
  return candidate && typeof candidate === "object" ? candidate : null;
}

async function getBarsWithOverlays(datasetId: string, limit: number, signal: AbortSignal): Promise<BarsEnvelope> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(datasetId)}/bars`, { limit, overlays: true }), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  return unwrap<BarsEnvelope>(response);
}

function originLabel(origin: string | null | undefined): string {
  if (!origin) return NO_DATA;
  if (origin === "csv") return "CSV واردشده";
  if (origin === "exchange") return "تاریخچه‌ی صرافی";
  return origin;
}

function formOk(row: QualityReport | null): boolean {
  if (!row) return false;
  return row.incomplete !== true && (row.warnings?.length ?? 0) === 0 && row.is_stale !== true;
}

/** One labelled control inside a Modal (the shared `Filters` bar is for lists). */
function FormRow({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? <span className="mute">{hint}</span> : null}
    </div>
  );
}

interface ActionNotice {
  title: string;
  datasetId: string | null;
  bars: number | null;
  timeframe: string | null;
  warnings: string[];
  windowLabel: string | null;
}

export default function Data() {
  const datasets = useAsync((signal) => getDatasets(signal), []);
  const config = useAsync((signal) => getConfig(signal), []);

  const [symbolFilter, setSymbolFilter] = useState("");
  const [timeframeFilter, setTimeframeFilter] = useState("");
  const [originFilter, setOriginFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [limit, setLimit] = useState("500");

  const [csvOpen, setCsvOpen] = useState(false);
  const [fetchOpen, setFetchOpen] = useState(false);
  const [busy, setBusy] = useState<"import" | "fetch" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<ActionNotice | null>(null);
  const [csvForm, setCsvForm] = useState({ path: "", symbol: "", timeframe: "" });
  const [fetchForm, setFetchForm] = useState({ symbol: "", timeframe: "", days: "365" });

  const rows = useMemo(() => {
    const all = datasets.data ?? [];
    return all.filter(
      (row) =>
        (symbolFilter === "" || row.symbol === symbolFilter) &&
        (timeframeFilter === "" || row.timeframe === timeframeFilter) &&
        (originFilter === "" || (originFilter === "csv" ? row.origin === "csv" : row.origin === "exchange")),
    );
  }, [datasets.data, symbolFilter, timeframeFilter, originFilter]);

  const selected = rows.find((row) => row.id === selectedId) ?? rows[0] ?? null;
  const selectedKey = selected?.id ?? "";

  const quality = useAsync(async (signal) => {
    if (!selectedKey) return null;
    return getDatasetQuality(selectedKey, signal);
  }, [selectedKey]);

  const bars = useAsync(async (signal) => {
    if (!selectedKey) return null;
    return getBarsWithOverlays(selectedKey, Number(limit), signal);
  }, [selectedKey, limit]);

  const symbolOptions = useMemo(() => {
    const set = new Set<string>();
    for (const symbol of config.data?.symbols ?? []) set.add(symbol);
    for (const row of datasets.data ?? []) set.add(row.symbol);
    return [...set].sort((a, b) => a.localeCompare(b, "en"));
  }, [config.data, datasets.data]);

  const timeframeOptions = useMemo(() => {
    const set = new Set<string>(TIMEFRAMES);
    if (config.data?.timeframe) set.add(config.data.timeframe);
    for (const row of datasets.data ?? []) set.add(row.timeframe);
    return [...set];
  }, [config.data, datasets.data]);

  const listProvenance = provenanceOf(datasets.data);
  const detailQuality = qualityOf(quality.data);
  const detailWindow = quality.data != null ? provenanceOf(quality.data)?.window ?? null : null;

  const columns: Column<DatasetInfo>[] = [
    { key: "id", header: "شناسه", render: (row) => <span className="num">{row.id}</span>, sortValue: (row) => row.id },
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "timeframe", header: "تایم‌فریم", render: (row) => <span className="num">{text(row.timeframe)}</span>, sortValue: (row) => row.timeframe },
    {
      key: "origin",
      header: "خاستگاه",
      render: (row) => <span title={text(row.origin)}>{originLabel(row.origin)}</span>,
      sortValue: (row) => row.origin,
    },
    { key: "source", header: "منبع", render: (row) => <span className="num">{text(row.source)}</span>, sortValue: (row) => row.source },
    { key: "bars", header: "کندل", numeric: true, sortValue: (row) => row.bars, render: (row) => num(row.bars, { max: 0 }) },
    { key: "start", header: "شروع (UTC)", sortValue: (row) => row.start_ts, render: (row) => dateTime(row.start_ts) },
    { key: "end", header: "پایان (UTC)", sortValue: (row) => row.end_ts, render: (row) => dateTime(row.end_ts) },
    { key: "created", header: "ثبت", sortValue: (row) => row.created_at, render: (row) => dateTime(row.created_at) },
    {
      key: "quality",
      header: "کیفیت",
      render: (row) => {
        const q = qualityOf(row.quality ?? null);
        if (!q) return <span className="mute">{NO_DATA}</span>;
        const ok = formOk(q);
        const notes = [
          q.incomplete === true ? "پوشش ناقص" : null,
          q.is_stale === true ? "داده‌ی کهنه" : null,
          (q.warnings?.length ?? 0) > 0 ? `هشدار: ${q.warnings?.join("، ")}` : null,
        ].filter((item): item is string => item !== null);
        return (
          <ToneBadge tone={ok ? "pos" : "warn"} title={notes.length > 0 ? notes.join(" · ") : "بدون هشدار در گزارش کیفیت"}>
            {ok ? "قابل‌استفاده" : "بازبینی لازم"}
          </ToneBadge>
        );
      },
    },
    { key: "note", header: "یادداشت", render: (row) => text(row.note, "—") },
  ];

  const barColumns: Column<BarsPage["bars"][number]>[] = [
    { key: "t", header: "زمان باز شدن (UTC)", sortValue: (bar) => bar.t, render: (bar) => dateTime(bar.t) },
    { key: "open", header: "open", numeric: true, sortValue: (bar) => bar.open, render: (bar) => num(bar.open, { max: 6 }) },
    { key: "high", header: "high", numeric: true, sortValue: (bar) => bar.high, render: (bar) => num(bar.high, { max: 6 }) },
    { key: "low", header: "low", numeric: true, sortValue: (bar) => bar.low, render: (bar) => num(bar.low, { max: 6 }) },
    { key: "close", header: "close", numeric: true, sortValue: (bar) => bar.close, render: (bar) => num(bar.close, { max: 6 }) },
    { key: "base_volume", header: "حجم پایه", numeric: true, sortValue: (bar) => bar.base_volume, render: (bar) => num(bar.base_volume, { max: 4 }) },
    { key: "quote_volume", header: "حجم USDT", numeric: true, sortValue: (bar) => bar.quote_volume, render: (bar) => num(bar.quote_volume, { max: 2 }) },
    { key: "closed", header: "وضعیت", render: (bar) => (bar.closed === false ? "باز" : bar.closed === true ? "بسته" : NO_DATA) },
  ];

  const windowLabel =
    bars.data?.provenance?.window ??
    (bars.data && bars.data.bars.length > 0
      ? `${dateTime(bars.data.bars[0]?.t)} ← ${dateTime(bars.data.bars[bars.data.bars.length - 1]?.t)}`
      : null);

  async function submitImport() {
    const path = csvForm.path.trim();
    const symbol = csvForm.symbol.trim().toUpperCase().replace(/[-/]/g, "_");
    if (!path || !symbol) {
      setActionError("برای وارد کردن، هم مسیر فایل و هم نماد لازم است.");
      return;
    }
    setBusy("import");
    setActionError(null);
    try {
      const ack = (await importDataset({ path, symbol, timeframe: csvForm.timeframe, origin: "csv" })) as unknown as ImportAck;
      const q = qualityOf(ack.quality ?? null);
      setNotice({
        title: "وارد کردن با اعتبارسنجی انجام شد",
        datasetId: ack.dataset_id ?? null,
        bars: typeof ack.bars === "number" ? ack.bars : q?.closed_bars ?? null,
        timeframe: ack.timeframe ?? q?.timeframe ?? null,
        warnings: q?.warnings ?? [],
        windowLabel: provenanceOf(ack)?.window ?? null,
      });
      setCsvOpen(false);
      setCsvForm({ path: "", symbol: "", timeframe: "" });
      if (ack.dataset_id) setSelectedId(ack.dataset_id);
      datasets.reload();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setBusy(null);
    }
  }

  async function submitFetch() {
    const symbol = fetchForm.symbol.trim().toUpperCase().replace(/[-/]/g, "_");
    const timeframe = fetchForm.timeframe || config.data?.timeframe || "";
    const days = Number(fetchForm.days);
    if (!symbol || !timeframe) {
      setActionError("نماد و تایم‌فریم برای دریافت تاریخ لازم است.");
      return;
    }
    if (!Number.isInteger(days) || days < 1 || days > 1500) {
      setActionError("«روز» باید عددی صحیح بین ۱ تا ۱۵۰۰ باشد (سقف قرارداد API).");
      return;
    }
    setBusy("fetch");
    setActionError(null);
    try {
      const ack = (await fetchDataset({ symbol, timeframe, days })) as unknown as ImportAck;
      const q = qualityOf(ack.quality ?? null);
      setNotice({
        title: "دریافت تاریخ از صرافی ذخیره شد",
        datasetId: ack.dataset_id ?? null,
        bars: typeof ack.bars === "number" ? ack.bars : q?.closed_bars ?? null,
        timeframe: ack.timeframe ?? q?.timeframe ?? timeframe,
        warnings: q?.warnings ?? [],
        windowLabel: provenanceOf(ack)?.window ?? null,
      });
      setFetchOpen(false);
      if (ack.dataset_id) setSelectedId(ack.dataset_id);
      datasets.reload();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>داده‌های تاریخی</h1>
          <p className="mute">
            فهرست مجموعه‌داده‌ها از <code>/datasets</code>، گزارش کیفیت از <code>/datasets/&#123;id&#125;/quality</code> و کندل‌ها از{" "}
            <code>/datasets/&#123;id&#125;/bars</code>. هر عددی که API برنگرداند «داده‌ای موجود نیست» می‌ماند.
          </p>
        </div>
        <div className="btn-row">
          <Badge kind="history" label="تاریخچه — داده‌ی خام صرافی/CSV" />
          <button type="button" className="btn small" onClick={() => { setActionError(null); setNotice(null); setCsvOpen(true); }}>
            وارد کردن CSV
          </button>
          <button type="button" className="btn small" onClick={() => { setActionError(null); setNotice(null); setFetchForm({ symbol: symbolOptions[0] ?? "", timeframe: config.data?.timeframe ?? "", days: fetchForm.days }); setFetchOpen(true); }}>
            دریافت تاریخ از صرافی
          </button>
        </div>
      </div>

      {notice ? (
        <SuccessBlock title={notice.title}>
          <dl className="kv">
            <dt>مجموعه‌داده</dt>
            <dd>{text(notice.datasetId)}</dd>
            <dt>کندل بسته‌ی ذخیره‌شده</dt>
            <dd>{num(notice.bars, { max: 0 })}</dd>
            <dt>تایم‌فریم</dt>
            <dd>{text(notice.timeframe)}</dd>
            <dt>پنجره</dt>
            <dd className="rtl-text">{text(notice.windowLabel)}</dd>
            <dt>هشدارهای کیفیت</dt>
            <dd className="rtl-text">{notice.warnings.length > 0 ? notice.warnings.join("، ") : "بدون هشدار در گزارش کیفیت"}</dd>
          </dl>
          <p className="mute">
            مجموعه‌داده‌ی تازه انتخاب شده است؛ اگر داده ناقص یا کهنه باشد، گزارش کیفیت پایین همان را اعلام می‌کند و سیستم داده را «معتبر» جا نمی‌زند.
          </p>
        </SuccessBlock>
      ) : null}

      <Filters
        fields={[
          {
            key: "symbol",
            label: "نماد",
            type: "select",
            value: symbolFilter,
            onChange: setSymbolFilter,
            options: [{ value: "", label: "همه" }, ...symbolOptions.map((symbol) => ({ value: symbol, label: symbol }))],
          },
          {
            key: "timeframe",
            label: "تایم‌فریم",
            type: "select",
            value: timeframeFilter,
            onChange: setTimeframeFilter,
            options: [{ value: "", label: "همه" }, ...timeframeOptions.map((tf) => ({ value: tf, label: tf }))],
          },
          {
            key: "origin",
            label: "خاستگاه",
            type: "select",
            value: originFilter,
            onChange: setOriginFilter,
            options: [
              { value: "", label: "همه" },
              { value: "exchange", label: "تاریخچه‌ی صرافی" },
              { value: "csv", label: "CSV واردشده" },
            ],
          },
          {
            key: "limit",
            label: "کندل نمودار",
            type: "select",
            value: limit,
            onChange: setLimit,
            options: [
              { value: "200", label: "۲۰۰ کندل آخر" },
              { value: "500", label: "۵۰۰ کندل آخر" },
              { value: "1000", label: "۱۰۰۰ کندل آخر" },
              { value: "2000", label: "۲۰۰۰ کندل آخر" },
            ],
            hint: "سقف قرارداد: ۵۰۰۰",
          },
        ]}
        onReset={() => { setSymbolFilter(""); setTimeframeFilter(""); setOriginFilter(""); setSelectedId(null); }}
        resultLabel={`نمایش ${num(rows.length, { max: 0 })} از ${num((datasets.data ?? []).length, { max: 0 })} مجموعه‌داده (فیلتر محلی — /datasets پارامتر فیلتر ندارد)`}
      />

      <section className="card" aria-label="فهرست مجموعه‌داده‌ها">
        <header>
          <h2>مجموعه‌داده‌ها</h2>
          <Badge kind="history" label="تاریخچه" />
        </header>
        <StateBlocks state={datasets} emptyLabel={NO_DATA} emptyHint="هنوز هیچ مجموعه‌داده‌ای وارد یا دریافت نشده است؛ از دکمه‌های بالا استفاده کنید.">
          {(all) => (
            <>
              <Table
                rows={rows}
                getRowId={(row) => row.id}
                caption="مجموعه‌داده‌های تاریخی ثبت‌شده"
                emptyLabel={NO_DATA}
                emptyHint="هیچ ردیفی با این فیلترها مطابقت ندارد."
                onRowClick={(row) => setSelectedId(row.id)}
                defaultSort={{ key: "created", dir: "desc" }}
                columns={columns}
              />
              <ProvenanceNote provenance={listProvenance} fallbackKind="history" extra={`/datasets · ${num(all.length, { max: 0 })} ردیف`} />
            </>
          )}
        </StateBlocks>
      </section>

      <section className="grid two" aria-label="جزئیات مجموعه‌داده">
        <div className="card">
          <header>
            <h2>گزارش کیفیت — {text(selected?.id)}</h2>
            <Badge kind="history" label="تاریخچه" />
          </header>
          {selected === null ? (
            <EmptyBlock label={NO_DATA} hint="برای دیدن گزارش کیفیت، ابتدا یک مجموعه‌داده وارد یا دریافت کنید." />
          ) : (
            <StateBlocks state={quality} emptyLabel={NO_DATA} emptyHint="گزارش کیفیتی برای این مجموعه‌داده ذخیره نشده است.">
              {() => {
                const q = detailQuality;
                if (!q) return <EmptyBlock label={NO_DATA} hint="ساختار گزارش کیفیت پاسخ داده نشده است." />;
                return (
                  <>
                    <dl className="kv">
                      <dt>خاستگاه / منبع</dt>
                      <dd className="rtl-text">{`${originLabel(q.origin)} · ${text(q.source)}`}</dd>
                      <dt>مجموع کندل‌ها</dt>
                      <dd>{num(q.bars, { max: 0 })}</dd>
                      <dt>کندل بسته</dt>
                      <dd>{num(q.closed_bars, { max: 0 })}</dd>
                      <dt>حذف‌شده (باز/نامعتبر)</dt>
                      <dd>{num(q.dropped_unclosed, { max: 0 })}</dd>
                      <dt>کندل مورد انتظار در پنجره</dt>
                      <dd>{num(q.expected_bars, { max: 0 })}</dd>
                      <dt>پوشش زمانی</dt>
                      <dd>{plainPct(q.coverage_pct, { max: 2 })}</dd>
                      <dt>تعداد شکاف</dt>
                      <dd>{num(q.gaps?.count ?? null, { max: 0 })}</dd>
                      <dt>کندل غایب در شکاف‌ها</dt>
                      <dd>{num(q.gaps?.missing_bars ?? null, { max: 0 })}</dd>
                      <dt>ردیف تکراری</dt>
                      <dd>{num(q.duplicates, { max: 0 })}</dd>
                      <dt>OHLC نامعتبر (high&lt;low و مشابه)</dt>
                      <dd>{num(q.impossible_ohlc, { max: 0 })}</dd>
                      <dt>قیمت نامعتبر (≤ صفر)</dt>
                      <dd>{num(q.non_positive_prices, { max: 0 })}</dd>
                      <dt>قدم زمانی (ثانیه)</dt>
                      <dd>{num(q.median_spacing, { max: 0 })}</dd>
                      <dt>تازگی داده</dt>
                      <dd className="rtl-text">{q.is_stale === true ? "کهنه — برای تصمیم تازه معتبر نیست" : q.is_stale === false ? "تازه" : NO_DATA}</dd>
                      <dt>فاصله تا آخرین کندل (ثانیه)</dt>
                      <dd>{num(q.stale_seconds, { max: 0 })}</dd>
                      <dt>شروع داده</dt>
                      <dd>{text(q.first_iso)}</dd>
                      <dt>پایان داده</dt>
                      <dd>{text(q.last_iso)}</dd>
                    </dl>
                    {(q.warnings?.length ?? 0) > 0 ? (
                      <div className="notice warn" role="alert">
                        <strong>هشدارهای کیفیت:</strong>
                        <span className="num">{q.warnings?.join("، ")}</span>
                      </div>
                    ) : (
                      <p className="stat-note">در گزارش کیفیت این مجموعه‌داده، هیچ هشداری ثبت نشده است.</p>
                    )}
                    <p className="mute">
                      این اعداد فقط وضعیت داده را نشان می‌دهند، نه اعتبار یک استراتژی. داده‌ی ناقص یا کهنه در لایه‌ی اجرا/ریسک رد می‌شود؛ UI آن را مجاز
                      اعلام نمی‌کند.
                    </p>
                    <ProvenanceNote
                      provenance={provenanceOf(quality.data) ?? selected?.provenance ?? null}
                      fallbackKind="history"
                      extra={detailWindow ? detailWindow : `/datasets/${text(selected?.id)}/quality`}
                    />
                  </>
                );
              }}
            </StateBlocks>
          )}
        </div>

        <div className="card">
          <header>
            <h2>کندل‌ها — {text(selected?.symbol)} {text(selected?.timeframe)}</h2>
            <Badge kind="history" label="تاریخچه" />
          </header>
          {selected === null ? (
            <EmptyBlock label={NO_DATA} hint="بدون مجموعه‌داده، نموداری رسم نمی‌شود." />
          ) : (
            <StateBlocks state={bars} emptyLabel={NO_DATA} emptyHint="این مجموعه‌داده کندل ذخیره‌شده ندارد.">
              {(page) => {
                if (!page) return <EmptyBlock label={NO_DATA} hint="پاسخی برای کندل‌ها برگشت نیامده است." />;
                return (
                  <>
                    <CandleChart
                      bars={page.bars}
                      overlays={(page.overlays ?? []).map((overlay) => ({ label: overlay.label, points: overlay.points }))}
                      windowLabel={windowLabel}
                      sourceLabel={page.provenance?.source ?? selected?.source ?? null}
                      totalBars={page.total_bars ?? null}
                    />
                    <p className="mute">
                      میانگین‌های نمایی (EMA) روی کل سری محاسبه و سپس بریده شده‌اند؛ به همین دلیل در ابتدای پنجره‌ی نمایش مقدار EMA تعریف نشده است و
                      نمودار به‌جای صفر، خط را می‌شکافد.
                    </p>
                    <ProvenanceNote provenance={page.provenance} fallbackKind="history" extra={`/datasets/${text(selected?.id)}/bars`} />
                  </>
                );
              }}
            </StateBlocks>
          )}
        </div>
      </section>

      <section className="card" aria-label="آخرین کندل‌های ذخیره‌شده">
        <header>
          <h2>۲۰ کندل آخر (بازبینی ردیف‌های خام)</h2>
          <Badge kind="history" label="تاریخچه" />
        </header>
        <StateBlocks state={bars} emptyLabel={NO_DATA} emptyHint="ردیفی برای نمایش وجود ندارد.">
          {(page) => (
            <Table
              rows={[...(page?.bars ?? [])].slice(-20).reverse()}
              getRowId={(bar) => `bar-${String(bar.t)}`}
              caption="آخرین کندل‌های بسته‌ی همان مجموعه‌داده"
              columns={barColumns}
              emptyHint="این مجموعه‌داده کندل ذخیره‌شده ندارد."
            />
          )}
        </StateBlocks>
      </section>

      <Modal
        open={csvOpen}
        title="وارد کردن CSV"
        description="فایل فقط خوانده و اعتبارسنجی می‌شود؛ هیچ داده‌ای به حساب یا صرافی متصل نیست."
        onClose={() => setCsvOpen(false)}
        footer={
          <>
            <button type="button" className="btn primary" onClick={submitImport} disabled={busy !== null}>
              {busy === "import" ? "در حال اعتبارسنجی…" : "وارد کردن"}
            </button>
            <button type="button" className="btn ghost" onClick={() => setCsvOpen(false)} disabled={busy !== null}>
              انصراف
            </button>
          </>
        }
      >
        <div className="grid two">
          <FormRow id="csv-path" label="مسیر فایل" hint="مسیر روی همان ماشین؛ نسبی به ریشه‌ی پروژه هم پذیرفته می‌شود.">
            <input
              id="csv-path"
              type="text"
              value={csvForm.path}
              placeholder="data/csv/BTC_USDT_1h.csv"
              onChange={(event) => setCsvForm({ ...csvForm, path: event.target.value })}
            />
          </FormRow>
          <FormRow id="csv-symbol" label="نماد" hint="مثال BTC_USDT">
            <input
              id="csv-symbol"
              type="text"
              value={csvForm.symbol}
              placeholder="BTC_USDT"
              onChange={(event) => setCsvForm({ ...csvForm, symbol: event.target.value })}
            />
          </FormRow>
          <FormRow id="csv-tf" label="تایم‌فریم" hint="اگر «خودکار» بماند، از فاصله‌ی زمانی فایل استنتاج می‌شود.">
            <select id="csv-tf" value={csvForm.timeframe} onChange={(event) => setCsvForm({ ...csvForm, timeframe: event.target.value })}>
              <option value="">خودکار از فایل</option>
              {timeframeOptions.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </FormRow>
        </div>
        {busy === "import" ? <LoadingBlock label="خواندن فایل و ساخت گزارش کیفیت…" /> : null}
        {actionError ? (
          <div className="notice danger" role="alert">
            <strong>وارد کردن انجام نشد.</strong>
            <span>{actionError}</span>
          </div>
        ) : null}
        <p className="mute">
          فایل نامعتبر یا ناقص با پیام فارسی رد می‌شود (۴۲۲) و هیچ مجموعه‌داده‌ی نیمه‌کاره‌ای ثبت نمی‌شود؛ داده‌ی کوتاه‌تر از پنجره با هشدار{" "}
          <code>incomplete</code> برچسب می‌خورد.
        </p>
      </Modal>

      <Modal
        open={fetchOpen}
        title="دریافت تاریخ از صرافی"
        description="دریافت کندل‌های بسته برای ساخت مجموعه‌داده‌ی تاریخچه؛ این کار هیچ سفارشی ارسال نمی‌کند."
        onClose={() => setFetchOpen(false)}
        footer={
          <>
            <button type="button" className="btn primary" onClick={submitFetch} disabled={busy !== null}>
              {busy === "fetch" ? "در حال دریافت…" : "دریافت و ذخیره"}
            </button>
            <button type="button" className="btn ghost" onClick={() => setFetchOpen(false)} disabled={busy !== null}>
              انصراف
            </button>
          </>
        }
      >
        <div className="grid two">
          <FormRow id="fetch-symbol" label="نماد" hint="نمادهای مجاز از /config خوانده می‌شوند.">
            <input
              id="fetch-symbol"
              type="text"
              list="fetch-symbol-options"
              value={fetchForm.symbol}
              placeholder="BTC_USDT"
              onChange={(event) => setFetchForm({ ...fetchForm, symbol: event.target.value })}
            />
            <datalist id="fetch-symbol-options">
              {symbolOptions.map((symbol) => (
                <option key={symbol} value={symbol} />
              ))}
            </datalist>
          </FormRow>
          <FormRow id="fetch-tf" label="تایم‌فریم">
            <select id="fetch-tf" value={fetchForm.timeframe} onChange={(event) => setFetchForm({ ...fetchForm, timeframe: event.target.value })}>
              <option value="">پیکربندی فعال</option>
              {timeframeOptions.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </FormRow>
          <FormRow id="fetch-days" label="روز" hint="۱ تا ۱۵۰۰ — سقف قرارداد API.">
            <input
              id="fetch-days"
              type="number"
              min={1}
              max={1500}
              value={fetchForm.days}
              onChange={(event) => setFetchForm({ ...fetchForm, days: event.target.value })}
            />
          </FormRow>
        </div>
        {busy === "fetch" ? <LoadingBlock label="دریافت تاریخچه و اعتبارسنجی ردیف‌ها…" /> : null}
        {actionError ? (
          <div className="notice danger" role="alert">
            <strong>دریافت انجام نشد.</strong>
            <span>{actionError}</span>
          </div>
        ) : null}
        <p className="mute">
          اگر پروکسی یا اتصال داده قطع باشد، سرویس ۵۰۳ و پیام fail-closed برمی‌گرداند و هیچ مجموعه‌داده‌ی ناقصی به‌جای آن ثبت نمی‌شود.
        </p>
      </Modal>

      <p className="notice warn">
        داده‌ی این صفحه صرفاً تاریخچه است (خاستگاه صرافی یا CSV کاربر). نتیجه‌ی بک‌تست و خروجی چرخه‌های پیپر در صفحات جدا و با برچسب{" "}
        <Badge kind="simulated" label="شبیه‌سازی" /> / <Badge kind="paper" label="پیپر" /> نمایش داده می‌شوند تا با تاریخچه اشتباه نشوند.
      </p>
    </>
  );
}
