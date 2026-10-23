import { useMemo } from "react";
import {
  getConfig,
  getHealth,
  getPortfolio,
  getRisk,
  useAsync,
  type Provenance,
} from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks from "../components/StateBlocks";
import Stat from "../components/Stat";
import Table, { type Column } from "../components/Table";
import { NO_DATA, money, num, pct, plainPct, signedMoney, text } from "../format";

/* -------------------------------------------------------------------------- */
/* Local helpers.                                                             */
/* `api/client.ts` is shared and untouched, so the defensive readers for the   */
/* `/risk` payload live in this page file. `/risk` currently answers with       */
/* `limits` + `usage` + a boolean `kill_switch`; the contract names them        */
/* `caps` / `kill_switch_active`, so both spellings are read here and anything  */
/* absent renders «داده‌ای موجود نیست» instead of a fabricated zero.            */
/* -------------------------------------------------------------------------- */

type Rec = Record<string, unknown>;

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

function stringsAt(value: unknown, key: string): string[] {
  const raw = asRec(value)[key];
  return Array.isArray(raw) ? raw.map((item) => String(item)) : [];
}

interface RiskView {
  mode: string | null;
  caps: Rec;
  usage: Rec;
  killSwitch: boolean | null;
  safeState: boolean | null;
  breaches: string[];
  warnings: string[];
  provenance: Provenance | null;
  generatedAt: string | null;
}

function toRiskView(payload: unknown): RiskView {
  const root = asRec(payload);
  const caps = { ...asRec(root["limits"]), ...asRec(root["caps"]) };
  const usage = asRec(root["usage"]);
  const kill = boolAt(root, "kill_switch_active", "kill_switch");
  const generated = root["generated_at"];
  return {
    mode: typeof root["mode"] === "string" ? root["mode"] : null,
    caps,
    usage,
    killSwitch: kill,
    safeState: boolAt(root, "safe_state"),
    breaches: stringsAt(root, "breaches"),
    warnings: stringsAt(root, "warnings"),
    provenance: (root["provenance"] as Provenance | undefined) ?? null,
    generatedAt: typeof generated === "string" ? generated : null,
  };
}

/* ------------------------------------------------------------- limit rows */

type RowStatus = "ok" | "near" | "breach" | "unknown";

interface LimitRow {
  key: string;
  label: string;
  meaning: string;
  cap: number | null;
  capText: string;
  usage: number | null;
  usageText: string;
  ratio: number | null;
  status: RowStatus;
  usageReported: boolean;
}

/** `higherIsWorse` rows compare a magnitude (loss, drawdown, count) against a cap. */
interface LimitSpec {
  key: string;
  label: string;
  meaning: string;
  unit: "pct" | "money" | "count" | "minutes" | "seconds" | "ratio";
  /** Keys inside `/risk` usage holding today's actual figure for this cap. */
  usageKeys: string[];
  /** Optional USDT ceiling the server reports next to a percentage cap. */
  limitMoneyKey?: string;
  /** Loss rows arrive as a signed P&L; only the negative part stresses the cap. */
  loss?: boolean;
  /** Cap may also be reported inside usage (e.g. max_trades_per_day). */
  capAlias?: string;
}

const LIMIT_SPECS: LimitSpec[] = [
  { key: "max_account_allocation_pct", label: "سقف سهم حساب از سرمایه", meaning: "حداکثر درصدی از سرمایه‌ی تخصیص‌یافته که می‌تواند در موقعیت باشد.", unit: "pct", usageKeys: ["exposure_pct"] },
  { key: "max_position_pct_of_allocated_capital", label: "سقف هر موقعیت", meaning: "اندازه‌ی هر موقعیت نسبت به سرمایه؛ بزرگ‌تر کردن آن ریسک هر خطا را بالا می‌برد.", unit: "pct", usageKeys: [] },
  { key: "max_risk_per_trade_pct", label: "سقف ریسک هر معامله", meaning: "زیان حداکثری هر سفارش (فاصله‌ی ورود تا حد ضرر) نسبت به سرمایه.", unit: "pct", usageKeys: [] },
  { key: "max_daily_loss_pct", label: "سقف زیان روزانه", meaning: "بعد از رسیدن به آن، سفارش جدید روز بسته می‌شود.", unit: "pct", usageKeys: ["day_realized_pnl"], limitMoneyKey: "daily_loss_limit", loss: true },
  { key: "max_weekly_loss_pct", label: "سقف زیان هفتگی", meaning: "همان کنترل برای هفت; بزرگ‌ترش کردن فقط زیان را دیرتر متوقف می‌کند.", unit: "pct", usageKeys: ["week_realized_pnl"], limitMoneyKey: "weekly_loss_limit", loss: true },
  { key: "max_drawdown_pct", label: "سقف افت سرمایه", meaning: "عبور از آن ⇒ توقف و رفتن به حالت امن.", unit: "pct", usageKeys: ["drawdown_pct"] },
  { key: "max_trades_per_day", label: "سقف تعداد معامله در روز", meaning: "جلوگیری از معاملات افراطی و کارمزد بی‌دلیل.", unit: "count", usageKeys: ["trades_today"], capAlias: "max_trades_per_day" },
  { key: "max_open_positions", label: "سقف موقعیت‌های هم‌زمان", meaning: "تعداد مجاز موقعیت باز؛ بیشتر یعنی همبستگی و ریسک بیشتر.", unit: "count", usageKeys: ["open_positions"], capAlias: "max_open_positions" },
  { key: "stop_on_consecutive_errors", label: "توقف بعد از خطاهای پیاپی", meaning: "رسیدن به این عدد ⇒ حالت امن، نه تلاش بیشتر.", unit: "count", usageKeys: ["consecutive_errors"] },
  { key: "max_spread_pct", label: "سقف اسپرد مجاز", meaning: "اگر اختلاف خرید/فروش بیشتر از این باشد، سیگنال رد می‌شود.", unit: "pct", usageKeys: [] },
  { key: "max_slippage_pct", label: "سقف اسلیپیج", meaning: "انحراف مجاز قیمت اجرا؛ بالاتر از آن سفاره لغو می‌شود.", unit: "pct", usageKeys: [] },
  { key: "min_reward_risk_ratio", label: "حداقل نسبت ریسک‌به‌بافت", meaning: "کمتر کردن آن معامله‌های بدتر را مجاز می‌کند؛ افزایش آن فقط می‌تواند کیفیت را بالا ببرد.", unit: "ratio", usageKeys: [] },
  { key: "cooldown_minutes_after_loss", label: "سکون بعد از زیان", meaning: "فاصله‌ی اجباری تا سفارش بعدی؛ حذفش یعنی تصمیم در لحظه‌ی احساسی.", unit: "minutes", usageKeys: [] },
  { key: "stop_on_data_staleness_seconds", label: "حداكثر کهنگی داده", meaning: "داده‌ی قدیمی‌تر از این ⇒ توقف سفارش جدید.", unit: "seconds", usageKeys: [] },
  { key: "max_clock_skew_seconds", label: "سقف اختلاف ساعت", meaning: "ساعت نامعتبر ⇒ حالت امن؛ این کنترل زمان را نمی‌توان شل کرد.", unit: "seconds", usageKeys: [] },
];

function capText(value: number | null, unit: LimitSpec["unit"]): string {
  if (value === null) return NO_DATA;
  if (unit === "pct") return plainPct(value, { max: 2 });
  if (unit === "money") return money(value);
  if (unit === "count") return num(value, { max: 0 });
  if (unit === "ratio") return `≥ ${num(value, { max: 2 })}`;
  if (unit === "minutes") return `${num(value, { max: 0 })} دقیقه`;
  return `${num(value, { max: 2 })} ثانیه`;
}

function usageText(value: number | null, spec: LimitSpec): string {
  if (value === null) return NO_DATA;
  if (spec.loss) return `${signedMoney(value)} (فقط بخش منفی زیان است)`;
  if (spec.unit === "pct") return pct(value, { max: 2 });
  if (spec.unit === "money") return money(value);
  if (spec.unit === "count") return num(value, { max: 0 });
  return num(value, { max: 2 });
}

function statusOf(ratio: number | null): RowStatus {
  if (ratio === null) return "unknown";
  if (ratio >= 1) return "breach";
  if (ratio >= 0.75) return "near";
  return "ok";
}

function buildRows(caps: Rec, usage: Rec, portfolioExposure: number | null): LimitRow[] {
  return LIMIT_SPECS.map((spec) => {
    const cap = numAt(caps, spec.key) ?? (spec.capAlias ? numAt(usage, spec.capAlias) : null);
    const rawUsage = numAt(usage, ...spec.usageKeys) ?? (spec.usageKeys.includes("exposure_pct") ? portfolioExposure : null);
    const usageReported = rawUsage !== null;
    const limitMoney = spec.limitMoneyKey ? numAt(usage, spec.limitMoneyKey) : null;
    const magnitude = spec.loss ? (rawUsage !== null && rawUsage < 0 ? Math.abs(rawUsage) : rawUsage === null ? null : 0) : rawUsage;
    const denominator = spec.loss && limitMoney !== null ? limitMoney : cap;
    const ratio = magnitude === null || denominator === null || denominator === 0 ? null : magnitude / denominator;
    return {
      key: spec.key,
      label: spec.label,
      meaning: spec.meaning,
      cap,
      capText: capText(cap, spec.unit) + (spec.loss && limitMoney !== null ? ` (= ${money(limitMoney)})` : ""),
      usage: rawUsage,
      usageText: usageText(rawUsage, spec),
      ratio,
      status: statusOf(ratio),
      usageReported,
    };
  });
}

const STATUS_LABEL: Record<RowStatus, string> = {
  ok: "در محدوده‌ی مجاز",
  near: "نزدیک به سقف",
  breach: "نقض سقف",
  unknown: "نامشخص",
};

/* --------------------------------------------------------------- the page */

export default function Risk() {
  const health = useAsync((signal) => getHealth(signal), []);
  const riskState = useAsync(async (signal) => toRiskView(await getRisk(signal)), []);
  const config = useAsync(async (signal) => (await getConfig(signal)) as Awaited<ReturnType<typeof getConfig>> & { config?: unknown }, []);
  const portfolio = useAsync((signal) => getPortfolio(signal), []);

  const caps = useMemo(() => {
    const raw = asRec(config.data?.config);
    return {
      ...asRec(raw["risk"]),
      ...asRec(raw["trading"]),
      ...asRec(config.data?.limits),
      ...(riskState.data?.caps ?? {}),
      allocated_capital: numAt(config.data, "allocated_capital") ?? numAt(portfolio.data, "allocated_capital"),
    };
  }, [config.data, riskState.data, portfolio.data]);
  const rows = useMemo(
    () => buildRows(caps, riskState.data?.usage ?? {}, portfolio.data?.exposure_pct ?? null),
    [caps, riskState.data, portfolio.data],
  );

  const breaches = riskState.data?.breaches ?? [];
  const warnings = [...(riskState.data?.warnings ?? []), ...(config.data?.warnings ?? [])];
  const healthKill = boolAt(asRec(health.data?.kill_switch), "active");
  const killSwitch = riskState.data?.killSwitch ?? healthKill;
  const safeState = riskState.data?.safeState;
  const windowLabel = riskState.data?.provenance?.window ?? null;

  const columns: Column<LimitRow>[] = [
    {
      key: "label",
      header: "محدودیت",
      sortValue: (row) => row.label,
      render: (row) => (
        <span>
          <strong>{row.label}</strong>
          <br />
          <span className="mute">{row.meaning}</span>
          <br />
          <span className="num mute">{row.key}</span>
        </span>
      ),
    },
    { key: "cap", header: "سقف پیکربندی", numeric: true, sortValue: (row) => row.cap, render: (row) => <span className="num">{row.capText}</span> },
    {
      key: "usage",
      header: "مصرف امروز (واقعی)",
      numeric: true,
      sortValue: (row) => (row.usageReported ? row.usage : null),
      render: (row) =>
        row.usageReported ? (
          <span className="num">{row.usageText}</span>
        ) : (
          <span className="mute">این مقدار در /risk گزارش نمی‌شود — {NO_DATA}</span>
        ),
    },
    {
      key: "ratio",
      header: "سهم مصرف‌شده از سقف",
      numeric: true,
      sortValue: (row) => row.ratio,
      render: (row) => (row.ratio === null ? NO_DATA : <span className="num">{plainPct(row.ratio * 100, { max: 1 })}</span>),
    },
    {
      key: "bar",
      header: "",
      render: (row) => <UsageBar ratio={row.ratio} />,
    },
    {
      key: "status",
      header: "وضعیت",
      sortValue: (row) => row.status,
      render: (row) => (
        <ToneBadge tone={row.status === "breach" ? "neg" : row.status === "near" ? "warn" : row.status === "ok" ? "pos" : "neutral"}>
          {row.status === "breach" ? "سقف رد شده" : STATUS_LABEL[row.status]}
        </ToneBadge>
      ),
    },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>مدیریت ریسک</h1>
          <p className="mute">
            هر سقف پیکربندی در برابر مصرف واقعی امروز. محدودیت‌ها در لایه‌ی سرور/اجرای سفارش enforce می‌شوند، نه در prompt مدل؛ این
            صفحه فقط می‌خواند و چیزی تغییر نمی‌دهد.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode ?? riskState.data?.mode} />
          <ToneBadge tone="neutral" title="هیچ مسیر نوشتن در /risk وجود ندارد">
            فقط خواندنی
          </ToneBadge>
        </div>
      </div>

      <div className="notice">
        <strong>این صفحه هیچ کنترل تغییر سقف ندارد.</strong>
        <span>
          سقف‌ها در <code>config.yaml</code> (بخش <code>risk</code>) زندگی می‌کنند و فقط مالک می‌تواند آن‌ها را ویرایش کند؛ هیچ endpoint
          در API برای افزایش سقف زیان، افزایش بودجه، افزایش اندازه‌ی موقعیت یا تغییر حالت اجرا وجود ندارد و عامل هم اجازه‌ی تغییر
          آن‌ها را ندارد. کلید توقف اضطراری (kill switch) در صفحه‌ی «پیپر تریدینگ» است.
        </span>
      </div>

      <div className="grid stats" aria-label="وضعیت ریسک">
        <Stat
          label="حالت اجرا"
          value={riskState.data?.mode ?? health.data?.mode ?? null}
          window={windowLabel}
          source="/health، /risk"
          endpoint="/risk"
          note="پیش‌فرض هر نصب تازه PAPER است؛ این صفحه حالت را عوض نمی‌کند."
        />
        <Stat
          label="کلید توقف اضطراری"
          value={killSwitch === null ? null : killSwitch ? "فعال — هیچ سفارش جدیدی ثبت نمی‌شود" : "غیرفعال (چرخه‌های کاغذی مجاز)"}
          tone={killSwitch === true ? "neg" : "neutral"}
          window={windowLabel}
          source="STOP_TRADING file"
          endpoint="/risk"
          note="کنترل آن در صفحه‌ی پیپر تریدینگ است؛ در این صفحه فقط وضعیت نمایش داده می‌شود."
        />
        <Stat
          label="حالت امن"
          value={safeState === null ? null : safeState ? "بله — سفارش جدید متوقف" : "خیر"}
          window={windowLabel}
          source="/risk"
          endpoint="/risk"
          note={safeState === null ? "سرور وضعیت safe_state را در این پاسخ گزارش نکرده است." : undefined}
        />
        <Stat
          label="نقض محدودیت‌ها"
          value={num(breaches.length, { max: 0 })}
          tone={breaches.length > 0 ? "neg" : "neutral"}
          window={windowLabel}
          source="/risk"
          endpoint="/risk"
        />
        <Stat
          label="هشدارهای ریسک/پیکربندی"
          value={num(warnings.length, { max: 0 })}
          tone={warnings.length > 0 ? "neutral" : "neutral"}
          window={windowLabel}
          source="/risk، /config"
          endpoint="/risk"
        />
        <Stat
          label="معامله‌ی امروز"
          value={
            numAt(riskState.data?.usage ?? {}, "trades_today") !== null
              ? `${num(numAt(riskState.data?.usage ?? {}, "trades_today"), { max: 0 })} از ${num(numAt(riskState.data?.usage ?? {}, "max_trades_per_day"), { max: 0 })}`
              : null
          }
          window={windowLabel}
          source="account state"
          endpoint="/risk"
        />
        <Stat
          label="افت سرمایه‌ی فعلی"
          value={pct(numAt(riskState.data?.usage ?? {}, "drawdown_pct"), { max: 2 })}
          tone={
            (numAt(riskState.data?.usage ?? {}, "drawdown_pct") ?? 0) >= (numAt(caps, "max_drawdown_pct") ?? Number.POSITIVE_INFINITY) * 0.75
              ? "neg"
              : "neutral"
          }
          window={windowLabel}
          source="engine"
          endpoint="/risk"
          note={`سقف فعال: ${plainPct(numAt(caps, "max_drawdown_pct"), { max: 1 })}`}
        />
        <Stat
          label="زیان تحقق‌یافته‌ی امروز"
          value={signedMoney(numAt(riskState.data?.usage ?? {}, "day_realized_pnl"))}
          tone={(numAt(riskState.data?.usage ?? {}, "day_realized_pnl") ?? 0) < 0 ? "neg" : "neutral"}
          window={windowLabel}
          source="ledger"
          endpoint="/risk"
          note={`سقف روزانه: ${money(numAt(riskState.data?.usage ?? {}, "daily_loss_limit"))}`}
        />
        <Stat
          label="سرمایه‌ی تخصیص‌یافته (عدد پیکربندی)"
          value={money(numAt(riskState.data?.usage ?? {}, "allocated_capital") ?? portfolio.data?.allocated_capital ?? null)}
          window={windowLabel}
          source="config.risk"
          endpoint="/risk"
          note="موجودی فرضی حساب کاغذی؛ نه عامل و نه این صفحه آن را افزایش نمی‌دهند."
        />
        <Stat
          label="موقعیت‌های باز"
          value={
            numAt(riskState.data?.usage ?? {}, "open_positions") !== null
              ? `${num(numAt(riskState.data?.usage ?? {}, "open_positions"), { max: 0 })} از ${num(numAt(riskState.data?.usage ?? {}, "max_open_positions"), { max: 0 })}`
              : null
          }
          window={windowLabel}
          source="account state"
          endpoint="/risk"
        />
        <Stat
          label="خطاهای پیاپی"
          value={num(numAt(riskState.data?.usage ?? {}, "consecutive_errors"), { max: 0 })}
          tone={(numAt(riskState.data?.usage ?? {}, "consecutive_errors") ?? 0) > 0 ? "neg" : "neutral"}
          window={windowLabel}
          source="engine"
          endpoint="/risk"
          note={`آستانه‌ی توقف: ${num(numAt(caps, "stop_on_consecutive_errors"), { max: 0 })} — رسیدن به آن یعنی حالت امن.`}
        />
      </div>

      <section className="card" aria-label="سقف‌ها در برابر مصرف">
        <header>
          <h2>سقف‌های فعال و مصرف امروز</h2>
          <Badge kind="paper" label="حساب کاغذی" />
        </header>
        <StateBlocks state={riskState} emptyLabel={NO_DATA} emptyHint="پاسخ /risk خالی بود؛ هیچ سقفی ساخته نمی‌شود.">
          {() => (
            <Table
              rows={rows}
              getRowId={(row) => row.key}
              columns={columns}
              caption="هر محدودیت پیکربندی در برابر مصرف واقعی امروز"
              defaultSort={{ key: "ratio", dir: "desc" }}
              emptyLabel={NO_DATA}
            />
          )}
        </StateBlocks>
        <p className="stat-note">
          «سهم مصرف‌شده از سقف» نسبت دو عدد واقعی است؛ جایی که مصرف در پاسخ سرور نیست، به‌عمد «گزارش نمی‌شود» نوشته شده و صفر
          گذاشته نشده است. سرمایه‌ی تخصیص‌یافته یک عدد پیکربندی است، نه چیزی که عامل بتواند بالا ببرد.
        </p>
        <ProvenanceNote provenance={riskState.data?.provenance ?? null} fallbackKind="paper" extra="/risk" />
      </section>

      {breaches.length > 0 ? (
        <div className="notice danger" role="alert">
          <strong>نقض محدودیت گزارش شده است.</strong>
          <ul>
            {breaches.map((item, index) => (
              <li key={`${item}-${String(index)}`}>{item}</li>
            ))}
          </ul>
          <span>در این حالت سامانه سفارش جدید نمی‌سازد و به حالت امن می‌رود.</span>
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="notice warn">
          <strong>هشدارهای ثبت‌شده</strong>
          <ul>
            {warnings.map((item, index) => (
              <li key={`${item}-${String(index)}`}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="card" aria-label="مرزهای غیرقابل‌عبور">
        <header>
          <h2>کاری که این محدودیت‌ها هرگز نمی‌کنند</h2>
          <ToneBadge tone="warn">قواعد ضدبازی</ToneBadge>
        </header>
        <ul>
          <li>
            <strong>هیچ Martingale یا «برای جبران زیان اندازه را بالا ببر».</strong> اندازه‌ی موقعیت را فقط لایه‌ی ریسک از سقف‌های
            ثابت config.yaml تعیین می‌کند؛ زیان، مجوز بزرگ‌تر کردن بعدی نیست.
          </li>
          <li>
            <strong>افزایش خودکار سقف‌ها ممنوع.</strong> نه عامل، نه استراتژی، نه UI می‌توانند سقف زیان، بودجه، فهرست نمادها یا سطح
            دسترسی را بالا ببرند. تغییر فقط با ویرایش دستی <code>config.yaml</code> توسط مالک و رکورد ممیزی ممکن است.
          </li>
          <li>
            <strong>پایه فقط spot و بدون اهرم است.</strong> اجازه‌ی فروش کوتاه، مارجین، اهرم و برداشت
            (<code>allow_short</code> / <code>allow_margin</code> / <code>allow_leverage</code> / <code>allow_withdrawals</code>) در این
            ساختار خاموش و غیرقابل‌فعال‌سازی توسط عامل‌اند؛ کد پیکربندی سه مورد آخر را اجباراً <code>false</code> می‌کند و مقدار true برای
            برداشت خطای پیکربندی است.
          </li>
          <li>
            <strong>داده‌ی ناقص یا قدیمی ⇒ توقف.</strong> کهنگی داده، ساعت نامعتبر، خطای صرافی یا وضعیت نامشخص سفارش، سفارش جدید را
            متوقف می‌کند و به حالت امن می‌رود؛ نه retries بی‌پایان و نه «شانس دوباره».
          </li>
          <li>
            <strong>enforcement در سرور است، نه در prompt.</strong> مدل فقط پیشنهاد می‌دهد؛ Risk Gate و لایه‌ی سفارش تصمیم را رد
            می‌کنند. به همین دلیل هیچ عددی در این صفحه «توصیه‌ی مدل» نیست.
          </li>
        </ul>
        <p className="stat-note">
          برای توقف فوری: صفحه‌ی «پیپر تریدینگ» (POST /kill-switch) — تنها تغییر مجاز وضعیت اجرا در کل رابط کاربری. هیچ مسیر
          «شروع معامله‌ی زنده» در API و UI وجود ندارد.
        </p>
      </section>

      <p className="notice warn">
        {text(riskState.data?.generatedAt ? `آخرین محاسبه‌ی ریسک: ${riskState.data.generatedAt}` : "زمان محاسبه در پاسخ /risk نیست")} —
        اعداد این صفحه مصرف ثبت‌شده‌ی حساب کاغذی‌اند و هیچ‌کدام پیش‌بینی یا تضمین سود نیستند.
      </p>
    </>
  );
}

/* --------------------------------------------------------------- usage bar */

/** Tiny non-animated ratio indicator: a plain track plus a filled width. */
function UsageBar({ ratio }: { ratio: number | null }) {
  if (ratio === null) return <span className="mute">{NO_DATA}</span>;
  const width = Math.max(0, Math.min(100, ratio * 100));
  const color = ratio >= 1 ? "var(--danger)" : ratio >= 0.75 ? "var(--warn)" : "var(--pos)";
  return (
    <span
      style={{ display: "block", width: "100%", maxWidth: 160, height: 8, borderRadius: 4, background: "var(--surface-2)", border: "1px solid var(--border)", direction: "ltr" }}
      role="img"
      aria-label={`سهم مصرف‌شده از سقف: ${Math.round(ratio * 100)}٪`}
    >
      <span style={{ display: "block", width: `${String(width)}%`, height: "100%", borderRadius: 3, background: color }} />
    </span>
  );
}
