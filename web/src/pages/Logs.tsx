import { useMemo, useState } from "react";
import { getHealth, getLogs, useAsync, type LogRow } from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import Filters from "../components/Filters";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import Table, { type Column } from "../components/Table";
import type { Tone } from "../components/Badge";
import { NO_DATA, dateTime, num, text } from "../format";

const NOT_RECORDED = "ثبت نشده";

const LEVEL_OPTIONS = [
  { value: "", label: "همه‌ی سطوح" },
  { value: "error", label: "error" },
  { value: "warning", label: "warning" },
  { value: "info", label: "info" },
  { value: "debug", label: "debug" },
];

/**
 * Defensive scrubbers — the platform rule is that no key/token/proxy value may
 * ever reach the screen, even if an API or a logger leaks one. These run on
 * every message and context value before rendering. Defined locally because
 * shared components are owned by other agents.
 */
function scrubSecrets(value: string): string {
  return value
    .replace(/(\w+):\/\/([^/\s:@]+):([^/\s@]+)@/g, "$1://[محرمانه-حذف‌شد]@")
    .replace(/\b(?:sk|api[_-]?key|apikey|token|secret|passwd|password)[_=:-]?[A-Za-z0-9_\-+/=]{8,}\b/gi, "[محرمانه-حذف‌شد]");
}

const SECRET_KEY_PATTERN = /key|secret|token|password|passwd|credential|authorization|cookie|proxy|apikey/i;

function redactValue(key: string, value: unknown): string {
  if (SECRET_KEY_PATTERN.test(key)) return "محرمانه — نمایش داده نمی‌شود";
  if (value === null || value === undefined) return NOT_RECORDED;
  if (typeof value === "object") return scrubSecrets(JSON.stringify(value));
  return scrubSecrets(String(value));
}

function levelTone(level: string | null): Tone {
  const l = (level ?? "").toLowerCase();
  if (l === "error" || l === "critical") return "neg";
  if (l === "warning" || l === "warn") return "warn";
  if (l === "info") return "info";
  return "neutral";
}

/** Config/safety events are not exposed as a separate endpoint in contract v1; extract them from /logs by message pattern. */
const CONFIG_EVENT_PATTERN = /kill ?switch|config|mode=|digest|owner_confirmed/i;

export default function Logs() {
  const [level, setLevel] = useState<string>("");
  const [visible, setVisible] = useState<string>("200");

  const logs = useAsync((signal) => getLogs({ level: level || null }, signal), [level]);
  const health = useAsync((signal) => getHealth(signal), []);

  const [selected, setSelected] = useState<LogRow | null>(null);

  const allRows = logs.data ?? [];
  const visibleCount = Math.max(1, Math.min(Number.parseInt(visible, 10) || 200, allRows.length || 1));
  const rows = allRows.slice(0, visibleCount);

  const levelCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const row of allRows) {
      const key = (row.level ?? "بدون سطح").toLowerCase();
      map.set(key, (map.get(key) ?? 0) + 1);
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }, [allRows]);

  const configEvents = useMemo(
    () => allRows.filter((row) => CONFIG_EVENT_PATTERN.test(`${row.source ?? ""} ${row.message ?? ""}`)),
    [allRows],
  );

  const columns: Column<LogRow>[] = [
    {
      key: "id",
      header: "شناسه",
      numeric: true,
      sortValue: (row) => row.id,
      render: (row) => <span className="num">{row.id === null || row.id === undefined ? NOT_RECORDED : String(row.id)}</span>,
    },
    { key: "ts", header: "زمان (UTC)", sortValue: (row) => row.ts, render: (row) => dateTime(row.ts) },
    {
      key: "level",
      header: "سطح",
      render: (row) => <ToneBadge tone={levelTone(row.level)}>{text(row.level, NOT_RECORDED)}</ToneBadge>,
      sortValue: (row) => row.level,
    },
    { key: "source", header: "منبع", render: (row) => <span className="num">{text(row.source, NOT_RECORDED)}</span>, sortValue: (row) => row.source },
    {
      key: "message",
      header: "پیام",
      render: (row) => <span className="rtl-text">{scrubSecrets(row.message) || NOT_RECORDED}</span>,
      sortValue: (row) => row.message,
    },
    {
      key: "context",
      header: "زمینه",
      render: (row) =>
        Object.keys(row.context ?? {}).length > 0 ? <span className="mute">دارد — برای جزئیات کلیک کنید</span> : <span className="mute">خالی</span>,
    },
  ];

  const healthWarnings = health.data?.warnings ?? [];
  const healthErrors = health.data?.errors ?? [];
  const counts = health.data?.counts ?? {};
  const countsEntries = Object.entries(counts);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>لاگ سیستم</h1>
          <p className="mute">
            رویدادهای ثبت‌شده در جدول logs از <code>/logs?level=</code>. هیچ کلید API، token یا مقدار پروکسی در این
            رابط نمایش داده نمی‌شود؛ پیام‌ها و زمینه‌ها پیش از رندر غربال می‌شوند.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode ?? null} />
          <Badge kind="paper" label="رویدادهای سامانه‌ی پژوهشی/کاغذی" />
        </div>
      </div>

      <Filters
        fields={[
          { key: "level", label: "سطح رویداد", value: level, onChange: setLevel, type: "select", options: LEVEL_OPTIONS, hint: "فیلتر سمت سرور" },
          { key: "visible", label: "نمایش حداکثر", value: visible, onChange: setVisible, type: "number", hint: "برش محلی از ردیف‌های دریافت‌شده" },
        ]}
        onReset={() => {
          setLevel("");
          setVisible("200");
        }}
        resultLabel={`نمایش ${num(rows.length, { max: 0 })} رویداد از ${num(allRows.length, { max: 0 })} ردیف دریافت‌شده${level ? ` (فیلتر: ${level})` : ""}`}
        actions={
          <button type="button" className="btn small" onClick={logs.reload} aria-label="خواندن دوباره‌ی لاگ‌ها">
            خواندن دوباره
          </button>
        }
      />

      <section className="grid two" aria-label="خلاصه‌ی وضعیت و شمارش‌ها">
        <div className="card">
          <header>
            <h2>خلاصه‌ی ممیزی (از /health)</h2>
            <ToneBadge tone={healthErrors.length > 0 ? "neg" : healthWarnings.length > 0 ? "warn" : "pos"}>
              {healthErrors.length > 0 ? "خطا گزارش‌شده" : healthWarnings.length > 0 ? "هشدار" : "وضعیت عادی"}
            </ToneBadge>
          </header>
          {health.loading ? <p className="mute">خواندن /health…</p> : null}
          {health.error ? <div className="notice danger" role="alert">{health.error}</div> : null}
          {!health.loading && !health.error && countsEntries.length === 0 ? <p className="mute">{NO_DATA}</p> : null}
          {countsEntries.length > 0 ? (
            <dl className="kv">
              {countsEntries.map(([table, count]) => (
                <div key={`count-${table}`} style={{ display: "contents" }}>
                  <dt>{`ردیف‌های جدول ${table}`}</dt>
                  <dd>{count === null || count === undefined ? NO_DATA : num(count, { max: 0 })}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {healthWarnings.length > 0 ? (
            <ul>
              {healthWarnings.map((warning, index) => (
                <li key={`warn-${String(index)}`}>{scrubSecrets(warning)}</li>
              ))}
            </ul>
          ) : null}
          {healthErrors.length > 0 ? (
            <ul>
              {healthErrors.map((message, index) => (
                <li key={`err-${String(index)}`} className="tone-neg">
                  {scrubSecrets(message)}
                </li>
              ))}
            </ul>
          ) : null}
          <p className="stat-note">شمارش‌ها مستقیم از پاسخ /health است؛ UI چیزی جمع یا تخمین نمی‌زند.</p>
        </div>

        <div className="card">
          <header>
            <h2>توزیع سطوح در پنجره‌ی دریافت‌شده</h2>
            <ToneBadge tone="neutral" title="محلی از ردیف‌های /logs">
              استخراج محلی
            </ToneBadge>
          </header>
          {levelCounts.length === 0 ? (
            <p className="mute">{logs.loading ? "در حال خواندن…" : NO_DATA}</p>
          ) : (
            <dl className="kv">
              {levelCounts.map(([name, count]) => (
                <div key={`lvl-${name}`} style={{ display: "contents" }}>
                  <dt>{name}</dt>
                  <dd>{num(count, { max: 0 })}</dd>
                </div>
              ))}
            </dl>
          )}
          <p className="stat-note">
            سرور در قرارداد v1 پیش‌فرض ۲۰۰ رویداد اخیر برمی‌گرداند؛ این توزیع فقط همین پنجره را می‌شمارد، نه کل جدول
            logs.
          </p>
        </div>
      </section>

      <section className="card" aria-label="رویدادهای پیکربندی و ایمنی">
        <header>
          <h2>رویدادهای پیکربندی / توقف اضطراری</h2>
          <ToneBadge tone={configEvents.length > 0 ? "warn" : "neutral"} title="endpoint مستقل برای config_events در قرارداد v1 وجود ندارد">
            استخراج‌شده از /logs
          </ToneBadge>
        </header>
        {logs.loading ? (
          <p className="mute">در حال خواندن رویدادها…</p>
        ) : configEvents.length === 0 ? (
          <p className="mute">
            در {num(allRows.length, { max: 0 })} رویداد دریافت‌شده، هیچ پیام مربوط به kill switch یا تغییر پیکربندی ثبت نشده است.
          </p>
        ) : (
          <dl className="kv">
            {configEvents.slice(0, 12).map((row, index) => (
              <div key={`cfg-${String(row.id ?? index)}`} style={{ display: "contents" }}>
                <dt>{dateTime(row.ts)}</dt>
                <dd className="rtl-text">{`[${text(row.level, NOT_RECORDED)}] ${scrubSecrets(row.message)}`}</dd>
              </div>
            ))}
          </dl>
        )}
        <p className="stat-note">
          تغییر وضعیت kill switch و رویدادهای config توسط موتور با source=api در همان جدول logs ثبت می‌شوند؛ این
          فهرست با تطبیق عبارت‌های kill switch/config از پیام‌ها جدا شده است و ادعا نمی‌کند که همه‌ی رویدادهای
          پیکربندی را پوشش می‌دهد (endpoint مستقل config_events در قرارداد v1 نیست).
        </p>
      </section>

      <section className="card" aria-label="جدول رویدادها">
        <header>
          <h2>رویدادهای سیستم</h2>
          <Badge kind="paper" label="GET /logs" />
        </header>
        <Table
          rows={rows}
          getRowId={(row, index) => `log-${String(row.id ?? index)}-${String(index)}`}
          caption="جدول رویدادهای لاگ با فیلتر سطح سمت سرور"
          loading={logs.loading}
          error={logs.error}
          onRetry={logs.reload}
          emptyLabel={NO_DATA}
          emptyHint={level ? `رویدادی با سطح «${level}» در پنجره‌ی دریافت‌شده ثبت نشده است.` : "هیچ رویدادی در جدول logs ثبت نشده است."}
          onRowClick={setSelected}
          defaultSort={{ key: "id", dir: "desc" }}
          columns={columns}
        />
        <ProvenanceNote
          provenance={null}
          fallbackKind="paper"
          extra="/logs — پاسخ سرور بدون provenance است؛ ۲۰۰ رویداد اخیر، برش محلی برای نمایش"
        />
      </section>

      <Modal
        open={selected !== null}
        title={selected ? `رویداد ${text(selected.level, "بدون سطح")} · ${text(selected.source, "بدون منبع")}` : "رویداد"}
        description="زمینه‌ی خام رویداد، بدون مقدارهای محرمانه."
        onClose={() => setSelected(null)}
        width={680}
      >
        {selected ? (
          <>
            <dl className="kv">
              <dt>شناسه</dt>
              <dd>{selected.id === null || selected.id === undefined ? NOT_RECORDED : String(selected.id)}</dd>
              <dt>زمان (UTC)</dt>
              <dd>{dateTime(selected.ts)}</dd>
              <dt>سطح</dt>
              <dd className="rtl-text">{text(selected.level, NOT_RECORDED)}</dd>
              <dt>منبع</dt>
              <dd>{text(selected.source, NOT_RECORDED)}</dd>
              <dt>پیام</dt>
              <dd className="rtl-text">{scrubSecrets(selected.message) || NOT_RECORDED}</dd>
            </dl>
            <h3>زمینه (context)</h3>
            {Object.keys(selected.context ?? {}).length === 0 ? (
              <p className="stat-note">این رویداد زمینه‌ای ثبت نکرده است.</p>
            ) : (
              <dl className="kv">
                {Object.entries(selected.context ?? {}).map(([key, value]) => (
                  <div key={`ctx-${key}`} style={{ display: "contents" }}>
                    <dt>{key}</dt>
                    <dd className={SECRET_KEY_PATTERN.test(key) ? "rtl-text" : undefined}>{redactValue(key, value)}</dd>
                  </div>
                ))}
              </dl>
            )}
            <p className="stat-note">
              اگر کلیدی با نامِ کلیدوار (key/token/proxy/…) در زمینه باشد، مقدارش عمداً نمایش داده نمی‌شود — مطابق
              دستورالعمل ایمنی، هیچ رازی (کلید API، token یا پروکسی) در این رابط دیده نمی‌شود.
            </p>
          </>
        ) : null}
      </Modal>
    </>
  );
}
