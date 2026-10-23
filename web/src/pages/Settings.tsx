import { useMemo } from "react";
import { getConfig, getHealth, useAsync } from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks from "../components/StateBlocks";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import { NO_DATA, dateTime, money, num, plainPct, text } from "../format";

/* -------------------------------------------------------------------------- */
/* Local helpers.                                                             */
/* `api/client.ts` is shared with other agents and is not edited here. The      */
/* `/config` payload carries the redacted raw tree under `config` plus          */
/* `config_digest`; neither is in the published `ConfigView` type yet, so this  */
/* page widens it locally and reads every field defensively — a key the server  */
/* did not send renders «داده‌ای موجود نیست», never a guessed value.            */
/* -------------------------------------------------------------------------- */

type Rec = Record<string, unknown>;

interface ConfigPayload {
  mode?: string | null;
  digest?: string | null;
  config_digest?: string | null;
  config?: unknown;
}

function asRec(value: unknown): Rec {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Rec) : {};
}

function numAt(value: unknown, ...keys: string[]): number | null {
  const source = asRec(value);
  for (const key of keys) {
    const raw = source[key];
    if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  }
  return null;
}

function strAt(value: unknown, ...keys: string[]): string | null {
  const source = asRec(value);
  for (const key of keys) {
    const raw = source[key];
    if (typeof raw === "string" && raw.trim() !== "" && raw !== "no_data") return raw;
  }
  return null;
}

function boolAt(value: unknown, ...keys: string[]): boolean | null {
  const source = asRec(value);
  for (const key of keys) {
    const raw = source[key];
    if (typeof raw === "boolean") return raw;
    if (raw === 1) return true;
    if (raw === 0) return false;
  }
  return null;
}

function at(root: unknown, ...path: string[]): unknown {
  let cursor: unknown = root;
  for (const key of path) {
    cursor = asRec(cursor)[key];
    if (cursor === undefined || cursor === null) return null;
  }
  return cursor;
}

/* ------------------------------------------------------------- redaction */

/** Key names the backend drops or masks; repeated here as a second check only. */
const SECRET_KEY_PATTERN = /(api[_-]?key|api[_-]?secret|token|password|passwd|secret|authorization|cookie|credential)/i;
/** Keys that would read like a promised return if the config ever carried one. */
const TARGET_KEY_PATTERN = /(target_return|expected_return|profit_target|goal|promise|guarantee)/i;

interface LeafRow {
  path: string;
  value: string;
  masked: boolean;
  dropped: boolean;
}

function flatten(source: unknown, prefix: string[]): LeafRow[] {
  if (Array.isArray(source)) {
    if (source.length === 0) return [{ path: prefix.join("."), value: "(فهرست خالی)", masked: false, dropped: false }];
    return source.flatMap((item, index) => flatten(item, [...prefix, `[${String(index)}]`]));
  }
  if (source !== null && typeof source === "object") {
    const record = source as Rec;
    const entries = Object.entries(record);
    if (entries.length === 0) return [{ path: prefix.join("."), value: "{}", masked: false, dropped: false }];
    return entries.flatMap(([key, value]) => flatten(value, [...prefix, key]));
  }
  const rendered =
    typeof source === "boolean" ? (source ? "true" : "false") : source === null ? "null" : typeof source === "number" ? num(source, { max: 6 }) : String(source);
  const last = prefix[prefix.length - 1] ?? "";
  const dropped = SECRET_KEY_PATTERN.test(last);
  return [{ path: prefix.join("."), value: dropped ? "(این کلید در پاسخ سرور حذف می‌شود)" : rendered, masked: rendered === "***", dropped }];
}

/* ------------------------------------------------------- param/limit labels */

const PARAM_LABELS: Record<string, string> = {
  name: "نام استراتژی",
  version: "نسخه",
  note: "یادداشت ساختار پایه",
  digest: "هش پارامترها",
  label: "برچسب نسخه",
  ema_fast: "EMA سریع",
  ema_slow: "EMA کند",
  rsi_period: "دوره‌ی RSI",
  atr_period: "دوره‌ی ATR",
  atr_stop_mult: "ضریب حد ضرر (ATR)",
  atr_target_mult: "ضریب هدف قیمت (ATR)",
  min_bars: "حداقل کندل تاریخچه",
  vol_lookback: "دوره‌ی نوسان",
  slope_lookback: "دوره‌ی شیب",
  volume_avg_bars: "دوره‌ی میانگین حجم",
  regime_gate: "دروازه‌ی رژیم بازار",
  require_multiple_signals: "نیاز به چند سیگنال هم‌زمان",
  no_trade_if_signals_conflict: "رد سیگنال در تضاد",
  rsi_bullish_low: "کف بازه‌ی RSI صعودی",
  rsi_bullish_high: "سقف بازه‌ی RSI صعودی",
  rsi_stretched: "آستانه‌ی RSI خیلی گرم",
  rsi_weak: "آستانه‌ی RSI ضعیف",
  volume_collapse_ratio: "نسبت ریزش حجم",
  volume_weak_ratio: "نسبت حجم ضعیف",
  slope_threshold: "آستانه‌ی شیب",
  vol_threshold: "آستانه‌ی نوسان",
  atr_pct_threshold: "آستانه‌ی درصد ATR",
};

const LIMIT_LABELS: Record<string, string> = {
  max_account_allocation_pct: "سقف سهم حساب از سرمایه",
  max_position_pct_of_allocated_capital: "سقف هر موقعیت",
  max_risk_per_trade_pct: "سقف ریسک هر معامله",
  max_daily_loss_pct: "سقف زیان روزانه",
  max_weekly_loss_pct: "سقف زیان هفتگی",
  max_drawdown_pct: "سقف افت سرمایه",
  max_trades_per_day: "سقف تعداد معامله در روز",
  max_spread_pct: "سقف اسپرد مجاز",
  max_slippage_pct: "سقف اسلیپیج",
  min_reward_risk_ratio: "حداقل نسبت ریسک‌به‌بافت",
  cooldown_minutes_after_loss: "سکون بعد از زیان (دقیقه)",
  stop_on_data_staleness_seconds: "حداکثر کهنگی داده (ثانیه)",
  stop_on_consecutive_errors: "توقف بعد از خطاهای پیاپی",
  max_clock_skew_seconds: "سقف اختلاف ساعت (ثانیه)",
};

const NOT_AGENT_CONTROLLED: { label: string; detail: string }[] = [
  { label: "بودجه و سرمایه‌ی تخصیص‌یافته", detail: "عامل هرگز آن را افزایش نمی‌دهد؛ فقط مالک در config.yaml تغییرش می‌دهد." },
  { label: "سقف زیان روزانه/هفتگی و drawdown", detail: "کاهش یا افزایش این سقف‌ها از مسیر UI یا توسط عامل ممکن نیست." },
  { label: "اندازه‌ی موقعیت و ریسک هر معامله", detail: "تعیین اندازه فقط در لایه‌ی ریسک و از سقف‌های ثابت انجام می‌شود." },
  { label: "فهرست نمادها", detail: "عامل نمی‌تواند نماد جدیدی به فهرست مجاز اضافه کند." },
  { label: "حالت اجرا (PAPER/LIVE)", detail: "پیش‌فرض هر نصب تازه PAPER است؛ این صفحه فقط حالت را می‌خواند و هیچ کنترل «شروع زنده» وجود ندارد." },
  { label: "فروش کوتاه، مارجین، اهرم و برداشت", detail: "خاموش و غیرقابل‌فعال‌سازی توسط عامل؛ کد پیکربندی آن‌ها را اجباراً خاموش نگه می‌دارد." },
  { label: "کلیدهای API و اطلاعات حساب", detail: "از log، گزارش، commit، prompt و پیام خطا حذف می‌شوند و در پاسخ API هم نیامده‌اند." },
];

/* --------------------------------------------------------------- the page */

export default function Settings() {
  const health = useAsync((signal) => getHealth(signal), []);
  const config = useAsync(async (signal) => (await getConfig(signal)) as Awaited<ReturnType<typeof getConfig>> & ConfigPayload, []);

  const data = config.data;
  const raw = asRec(data?.config);
  const leaves = useMemo(() => flatten(raw, []), [raw]);

  const mode = strAt(data, "mode") ?? strAt(health.data, "mode");
  const digest = strAt(data, "config_digest") ?? strAt(data, "digest");
  const liveEnabled = boolAt(health.data, "live_trading_enabled");
  const ownerConfirmed = boolAt(health.data, "owner_confirmed");
  const killRaw = health.data?.kill_switch;
  const killActive = typeof killRaw === "boolean" ? killRaw : boolAt(killRaw, "active");

  const switches: { key: string; label: string; value: boolean | null; note: string }[] = [
    {
      key: "trading.allow_short",
      label: "فروش کوتاه (allow_short)",
      value: boolAt(at(raw, "trading"), "allow_short"),
      note: "خاموش / غیرقابل‌فعال‌سازی توسط عامل — نسخه‌ی پایه spot است.",
    },
    {
      key: "trading.allow_margin",
      label: "مارجین (allow_margin)",
      value: boolAt(at(raw, "trading"), "allow_margin"),
      note: "خاموش / غیرقابل‌فعال‌سازی توسط عامل — کد مقدار true را رد یا بی‌اثر می‌کند و هشدار ثبت می‌کند.",
    },
    {
      key: "trading.allow_leverage",
      label: "اهرم (allow_leverage)",
      value: boolAt(at(raw, "trading"), "allow_leverage"),
      note: "خاموش / غیرقابل‌فعال‌سازی توسط عامل — اهرم در این ساختار وجود ندارد.",
    },
    {
      key: "trading.allow_withdrawals",
      label: "برداشت (allow_withdrawals)",
      value: boolAt(at(raw, "trading"), "allow_withdrawals"),
      note: "خاموش / غیرقابل‌فعال‌سازی توسط عامل — true بودن آن خطای پیکربندی است.",
    },
    {
      key: "trading.live_trading_enabled",
      label: "معامله‌ی زنده (live_trading_enabled)",
      value: liveEnabled,
      note: "از /health خوانده می‌شود؛ فعال‌سازی فقط با رکورد تأیید صریح مالک که به digest همین پیکربندی گره خورده است.",
    },
    {
      key: "security.reject_withdraw_permission",
      label: "رد کلید با اجازه‌ی برداشت",
      value: boolAt(at(raw, "security"), "reject_withdraw_permission"),
      note: "باید true بماند؛ کلید API نباید اجازه‌ی برداشت داشته باشد.",
    },
    {
      key: "security.redact_secrets_from_logs",
      label: "قرینه‌سازی اسرار در log",
      value: boolAt(at(raw, "security"), "redact_secrets_from_logs"),
      note: "باید true بماند.",
    },
    {
      key: "execution.idempotency_required",
      label: "اجباری بودن idempotency سفارش",
      value: boolAt(at(raw, "execution"), "idempotency_required"),
      note: "از سفارش تکراری جلوگیری می‌کند.",
    },
  ];

  const targets = leaves.filter((leaf) => TARGET_KEY_PATTERN.test(leaf.path));

  const leafColumns: Column<LeafRow>[] = [
    { key: "path", header: "کلید پیکربندی", sortValue: (leaf) => leaf.path, render: (leaf) => <span className="num">{leaf.path}</span> },
    {
      key: "value",
      header: "مقدار مؤثر",
      sortValue: (leaf) => leaf.value,
      render: (leaf) =>
        leaf.dropped ? (
          <span className="tone-neg">{leaf.value}</span>
        ) : leaf.masked ? (
          <span className="tone-neg" title="سرور مقدار خام را برنمی‌گرداند">
            <span className="num">***</span> (مقدار خام هرگز ارسال نمی‌شود)
          </span>
        ) : (
          <span className="num">{leaf.value}</span>
        ),
    },
  ];

  const limitRows = useMemo(() => {
    const limits = asRec(data?.limits);
    const keys = [...new Set([...Object.keys(limits), ...Object.keys(LIMIT_LABELS)])];
    return keys.map((key) => ({ key, label: LIMIT_LABELS[key] ?? key, value: numAt(limits, key) }));
  }, [data]);

  const limitColumns: Column<{ key: string; label: string; value: number | null }>[] = [
    { key: "label", header: "محدودیت", sortValue: (row) => row.label, render: (row) => <span>{row.label}</span> },
    { key: "key", header: "کلید", sortValue: (row) => row.key, render: (row) => <span className="num">{row.key}</span> },
    { key: "value", header: "مقدار پیکربندی", numeric: true, sortValue: (row) => row.value, render: (row) => <span className="num">{num(row.value, { max: 4 })}</span> },
  ];

  const paramRows = useMemo(() => {
    const params = asRec(data?.strategy_params);
    return Object.entries(params).map(([key, value]) => ({
      key,
      label: PARAM_LABELS[key] ?? key,
      value: typeof value === "boolean" ? (value ? "true" : "false") : value === null || value === undefined ? null : String(value),
    }));
  }, [data]);

  const paramColumns: Column<{ key: string; label: string; value: string | null }>[] = [
    { key: "label", header: "پارامتر", sortValue: (row) => row.label, render: (row) => <span>{row.label}</span> },
    { key: "key", header: "کلید", sortValue: (row) => row.key, render: (row) => <span className="num">{row.key}</span> },
    { key: "value", header: "مقدار", numeric: true, sortValue: (row) => row.value, render: (row) => <span className="num">{text(row.value)}</span> },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>تنظیمات</h1>
          <p className="mute">
            نمایش قرینه‌شده‌ی پیکربندی مؤثر از <code>/config</code> و وضعیت اجرا از <code>/health</code>. این صفحه فقط می‌خواند: هیچ
            ورودی، هیچ کلید ذخیره و هیچ مسیر «شروع معامله‌ی زنده» در آن وجود ندارد.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={mode} />
          <ToneBadge tone="neutral" title="تغییر پیکربندی فقط با ویرایش دستی config.yaml توسط مالک">
            فقط خواندنی
          </ToneBadge>
          <button type="button" className="btn small" onClick={config.reload} aria-label="خواندن دوباره‌ی پیکربندی">
            خواندن دوباره
          </button>
        </div>
      </div>

      <div className="notice">
        <strong>اسرار هرگز نمایش داده نمی‌شوند.</strong>
        <span>
          سرور پیش از ارسال پاسخ، هر کلید شبیه <code>api_key</code> / <code>secret</code> / <code>token</code> را حذف و مقدار{" "}
          <code>proxy</code> را <code>***</code> می‌کند. این صفحه همان مقدار قرینه‌شده را نشان می‌دهد و فقط مشخص می‌کند که پروکسی «تنظیم
          شده است» یا نه؛ مقدار خام کلید در هیچ پاسخ، log، گزارش یا commit نیست.
        </span>
      </div>

      <div className="grid stats" aria-label="وضعیت سامانه">
        <Stat
          label="حالت اجرای مؤثر"
          value={mode}
          tone={mode?.toUpperCase() === "LIVE" ? "neg" : "neutral"}
          window={windowOf(data)}
          source="/config، /health"
          endpoint="/config"
          note="پیش‌فرض هر نصب تازه PAPER است. این صفحه حالت را تغییر نمی‌دهد؛ فقط می‌خواند."
        />
        <Stat
          label="معامله‌ی زنده"
          value={liveEnabled === null ? null : liveEnabled ? "روشن (خارج از محدوده‌ی نسخه‌ی پایه)" : "خاموش / غیرقابل‌فعال‌سازی توسط عامل"}
          tone={liveEnabled === true ? "neg" : "neutral"}
          source="/health"
          endpoint="/health"
          note="فعال شدن live فقط با رکورد تأیید صریح مالک و گره به digest همان پیکربندی ممکن است؛ adapter زنده در این نسخه وصل نیست."
        />
        <Stat
          label="تأیید مالک برای live"
          value={ownerConfirmed === null ? null : ownerConfirmed ? "رکورد معتبر موجود است" : "رکوردی معتبر موجود نیست"}
          source="/health"
          endpoint="/health"
          note={ownerConfirmed === true ? "با وجود رکورد، این UI هیچ کنترل شروع زنده ندارد." : undefined}
        />
        <Stat
          label="کلید توقف اضطراری"
          value={killActive === null ? null : killActive ? "فعال" : "غیرفعال"}
          tone={killActive === true ? "neg" : "neutral"}
          source="STOP_TRADING"
          endpoint="/health"
          note="کنترل آن در صفحه‌ی پیپر تریدینگ است."
        />
        <Stat
          label="digest پیکربندی مؤثر"
          value={digest}
          source="config_digest"
          endpoint="/config"
          note="هر تغییر دستی در config.yaml این هش را عوض می‌کند؛ رکوردها به همین digest گره می‌خورند."
        />
        <Stat
          label="صرافی / نوع بازار"
          value={data ? `${text(strAt(data, "exchange"))} · ${text(strAt(at(raw, "trading"), "market_type"))}` : null}
          source="config.trading"
          endpoint="/config"
          note="نسخه‌ی پایه فقط spot است؛ market_type غیر از spot خطای پیکربندی است."
        />
        <Stat label="سرمایه‌ی تخصیص‌یافته" value={money(numAt(data, "allocated_capital"))} source="config.risk" endpoint="/config" note="عدد پیکربندی مالک؛ عامل آن را افزایش نمی‌دهد." />
        <Stat
          label="کارمزد پیکربندی"
          value={data ? `${plainPct(numAt(data, "maker_fee_pct"), { max: 2 })} maker · ${plainPct(numAt(data, "taker_fee_pct"), { max: 2 })} taker` : null}
          source="config.fees"
          endpoint="/config"
          note="همین نرخ‌ها در بک‌تست و شبیه‌سازی کسر می‌شوند."
        />
      </div>

      <section className="card" aria-label="مرزهای دسترسی عامل">
        <header>
          <h2>چه چیزی را عامل نمی‌تواند تغییر دهد</h2>
          <ToneBadge tone="warn">قواعد غیرقابل‌مذاکره</ToneBadge>
        </header>
        <ul>
          {NOT_AGENT_CONTROLLED.map((item) => (
            <li key={item.label}>
              <strong>{item.label}</strong> — {item.detail}
            </li>
          ))}
        </ul>
        <p className="stat-note">
          محدودیت‌ها در لایه‌ی سرور و گیت ریسک enforce می‌شوند، نه فقط در prompt مدل. اگر داده ناقص/قدیمی باشد، ساعت نامعتبر باشد،
          موجودی نخوانده شود، خطای صرافی رخ دهد یا وضعیت سفارش نامشخص بماند، سامانه سفارش جدید نمی‌سازد و به حالت امن می‌رود.
        </p>
      </section>

      <section className="card" aria-label="کلیدهای ایمنی">
        <header>
          <h2>کلیدهای ایمنی و دسترسی</h2>
          <Badge kind="paper" label="همه از /config و /health" />
        </header>
        <dl className="kv">
          {switches.map((item) => (
            <div key={item.key} style={{ display: "contents" }}>
              <dt>{item.label}</dt>
              <dd className="rtl-text">
                <ToneBadge
                  tone={item.value === true && item.key !== "security.reject_withdraw_permission" && item.key !== "security.redact_secrets_from_logs" && item.key !== "execution.idempotency_required" ? "neg" : item.value === null ? "warn" : "pos"}
                  title={item.note}
                >
                  {item.value === null ? "در پاسخ نیست" : item.value ? "روشن" : "خاموش / غیرقابل‌فعال‌سازی توسط عامل"}
                </ToneBadge>{" "}
                <span className="num mute">{item.key}</span>
              </dd>
            </div>
          ))}
        </dl>
        <p className="stat-note">
          وضعیت پروکسی: {boolAt(at(raw, "data"), "proxy_configured") === true ? "تنظیم شده (مقدار خام نمایش داده نمی‌شود)" : boolAt(at(raw, "data"), "proxy_configured") === false ? "تنظیم نشده" : NO_DATA} ·
          فایل توقف: <span className="num">{text(strAt(at(raw, "monitoring"), "kill_switch_file"))}</span> · تایم‌فریم تأیید:{" "}
          <span className="num">{text(strAt(data, "confirm_timeframe"))}</span>
        </p>
      </section>

      <section className="grid two" aria-label="بازه‌ی زمانی و نمادها">
        <div className="card">
          <header>
            <h2>نمادها و بازه‌ی زمانی</h2>
            <Badge kind="history" label="پیکربندی" />
          </header>
          <StateBlocks state={config} emptyLabel={NO_DATA} emptyHint="پیکربندی خوانده نشد؛ چیزی ساخته نمی‌شود.">
            {(payload) => (
              <dl className="kv">
                <dt>نمادهای مجاز</dt>
                <dd className="rtl-text">{payload.symbols && payload.symbols.length > 0 ? payload.symbols.join("، ") : NO_DATA}</dd>
                <dt>تایم‌فریم سیگنال</dt>
                <dd className="rtl-text">{text(strAt(payload, "timeframe"))}</dd>
                <dt>تایم‌فریم تأیید</dt>
                <dd className="rtl-text">{text(strAt(payload, "confirm_timeframe"))}</dd>
                <dt>سقف موقعیت هم‌زمان</dt>
                <dd className="rtl-text">{num(numAt(at(payload as unknown, "trading"), "max_open_positions"), { max: 0 })}</dd>
                <dt>منطقه‌ی زمانی</dt>
                <dd className="rtl-text">{text(strAt(at(asRec(payload.config), "app"), "timezone"))}</dd>
                <dt>سطح log</dt>
                <dd className="rtl-text">{text(strAt(at(asRec(payload.config), "app"), "log_level"))}</dd>
              </dl>
            )}
          </StateBlocks>
        </div>
        <div className="card">
          <header>
            <h2>هدف یا وعده‌ی سود در پیکربندی</h2>
            <ToneBadge tone="neutral">تفکیک «هدف» از عدد اندازه‌گیری‌شده</ToneBadge>
          </header>
          {targets.length > 0 ? (
            <>
              <div className="notice warn">
                <strong>این کلیدها «هدف»‌اند، نه پیش‌بینی و نه تضمین.</strong>
                <span>
                  هیچ عدد زیر را به‌عنوان بازده مورد انتظار معاملات نبینید؛ نتایج اندازه‌گیری‌شده فقط در بک‌تست، والک‌فورورد و ledger
                  حساب کاغذی‌اند و جدا از این اهداف نمایش داده می‌شوند.
                </span>
              </div>
              <dl className="kv">
                {targets.map((leaf) => (
                  <div key={leaf.path} style={{ display: "contents" }}>
                    <dt>هدف — پیش‌بینی یا تضمین نیست</dt>
                    <dd className="rtl-text">{leaf.value}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : (
            <p className="stat-note">
              در پیکربندی مؤثر هیچ کلید «هدف بازده» یا عدد وعده‌دار ثبت نشده است و این صفحه هم چنین عددی نمی‌سازد. تنها اعداد
              مربوط به عملکرد، نتایج اندازه‌گیری‌شده در صفحه‌های بک‌تست، آزمایشگاه استراتژی و گزارش عملکردند.
            </p>
          )}
          <p className="stat-note">
            ضریب هدف قیمت (ATR) در پیکربندی استراتژی:{" "}
            <span className="num">{num(numAt(data?.strategy_params, "atr_target_mult"), { max: 2 })}</span> — این عدد چند برابر ATR
            فاصله‌ی حد سود است، نه درصد سود مورد انتظار؛ کنار هیچ عدد اندازه‌گیری‌شده‌ای به‌عنوان «بازده هدف» نمایش داده نمی‌شود.
          </p>
        </div>
      </section>

      <section className="card" aria-label="سقف‌های ریسک در پیکربندی">
        <header>
          <h2>سقف‌های ریسک (مقادیر پیکربندی)</h2>
          <ToneBadge tone="warn" title="تغییر این‌ها فقط با ویرایش دستی config.yaml توسط مالک">
            بدون کنترل تغییر
          </ToneBadge>
        </header>
        <Table
          rows={limitRows}
          getRowId={(row) => row.key}
          columns={limitColumns}
          caption="سقف‌های ریسک از /config.limits"
          loading={config.loading}
          error={config.error}
          onRetry={config.reload}
          emptyLabel={NO_DATA}
          emptyHint="بخش limits در پاسخ /config نبود."
        />
        <p className="stat-note">
          مصرف امروز هر سقف را در صفحه‌ی «مدیریت ریسک» ببینید. اینجا فقط مقدار پیکربندی است و هیچ راه افزایش از این صفحه وجود ندارد.
        </p>
      </section>

      <section className="card" aria-label="پارامترهای استراتژی">
        <header>
          <h2>پارامترهای استراتژی (فقط خواندنی)</h2>
          <Badge kind="simulated" label="پژوهشی" />
        </header>
        <Table
          rows={paramRows}
          getRowId={(row) => row.key}
          columns={paramColumns}
          caption="strategy_params از /config"
          loading={config.loading}
          error={config.error}
          onRetry={config.reload}
          emptyLabel={NO_DATA}
          emptyHint="strategy_params در پاسخ /config نبود."
        />
        <p className="stat-note">
          این مقدار همان ساختار فعال است. تغییر پارامتر از مسیر UI انجام نمی‌شود: یک نسخه‌ی جدید در «آزمایشگاه استراتژی» با شبکه‌ی
          محدود ساخته و فقط با شواهد برون‌نمونه‌ای پذیرفته می‌شود.
        </p>
      </section>

      <section className="card" aria-label="پیکربندی قرینه‌شده">
        <header>
          <h2>درخت پیکربندی مؤثر (قرینه‌شده)</h2>
          <ToneBadge tone="neutral" title="همان چیزی که سرور برمی‌گرداند؛ نه فایل روی دیسک">
            redacted
          </ToneBadge>
        </header>
        <Table
          rows={leaves}
          getRowId={(leaf) => leaf.path}
          columns={leafColumns}
          caption="تک‌تک کلیدهای پیکربندی قرینه‌شده"
          loading={config.loading}
          error={config.error}
          onRetry={config.reload}
          emptyLabel={NO_DATA}
          emptyHint="سرور درخت قرینه‌شده را در این پاسخ برنداشت."
        />
        <ProvenanceNote
          provenance={{
            kind: "paper",
            source: strAt(at(raw, "trading"), "exchange") ?? "config.yaml",
            window: `digest: ${text(digest)}`,
            generated_at: strAt(data, "generated_at") ?? dateTime(health.data?.generated_at ?? null),
          }}
          extra="/config"
        />
      </section>

      {data && data.warnings && data.warnings.length > 0 ? (
        <div className="notice warn">
          <strong>هشدارهای پیکربندی</strong>
          <ul>
            {data.warnings.map((item, index) => (
              <li key={`${item}-${String(index)}`}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="card" aria-label="ممیزی تغییرات">
        <header>
          <h2>ممیزی تغییرات پیکربندی</h2>
          <Badge kind="paper" />
        </header>
        <p className="stat-note">
          هر تغییر پیکربندی و هر فعال/غیرفعال‌کردن کلید توقف در سمت سرور با actor، digest و توضیح در جدول <code>config_events</code>{" "}
          ثبت می‌شود. در قرارداد API فعلی endpoint خواندن آن رویدادها تعریف نشده است، بنابراین این صفحه فقط مقدار مؤثر فعلی را نشان
          می‌دهد و سابقه‌ی تغییر را «داده‌ای موجود نیست» می‌خواند — نه حدس.
        </p>
      </section>
    </>
  );
}

function windowOf(data: { generated_at?: string | null } | null | undefined): string | null {
  const generated = strAt(data, "generated_at");
  return generated ? `زمان خواندن: ${dateTime(generated)}` : null;
}
