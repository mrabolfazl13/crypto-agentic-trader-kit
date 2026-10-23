import { useState } from "react";
import {
  ApiError,
  describeError,
  getHealth,
  getOrders,
  runPaperCycle,
  setKillSwitch,
  useAsync,
  type Health,
  type OrdersView,
  type PaperCycleResult,
} from "../api/client";
import Badge, { ModeBadge, ToneBadge } from "../components/Badge";
import Modal from "../components/Modal";
import ProvenanceNote from "../components/ProvenanceNote";
import StateBlocks, { EmptyBlock, LoadingBlock, SuccessBlock } from "../components/StateBlocks";
import Table, { type Column } from "../components/Table";
import { NO_DATA, dateTime, money, num, pct, signedMoney, text, tone } from "../format";

/* ------------------------------------------------------------------ helpers
 * These adapters are local to this page (shared files are owned by other
 * agents). They reconcile the client types with the exact shapes returned by
 * `src/cat/api.py` today, so nothing is invented at render time.
 */

type OrderRow = OrdersView["rows"][number];
type CycleOutcome = PaperCycleResult["outcomes"][number] & {
  filled_qty?: number | null;
  fill_price?: number | null;
  details?: Record<string, unknown>;
};

interface KillSwitchState {
  active: boolean | null;
  effect: string | null;
  file: string | null;
  mode: string | null;
}

/** `/health` currently returns `kill_switch` as a boolean; the client type allows an object. Read both. */
function killSwitchFromHealth(health: Health | null): boolean | null {
  if (!health) return null;
  const raw: unknown = (health as unknown as Record<string, unknown>)["kill_switch"];
  if (typeof raw === "boolean") return raw;
  if (raw && typeof raw === "object") {
    const active = (raw as Record<string, unknown>)["active"];
    if (typeof active === "boolean") return active;
  }
  return null;
}

/** `POST /kill-switch` answers `{active, effect, kill_switch_file, mode}`; the client type declares `{kill_switch_active, message}`. */
function readKillSwitchResponse(payload: unknown): KillSwitchState {
  const raw = (payload ?? {}) as Record<string, unknown>;
  const active =
    typeof raw["active"] === "boolean"
      ? raw["active"]
      : typeof raw["kill_switch_active"] === "boolean"
        ? raw["kill_switch_active"]
        : null;
  const effect =
    typeof raw["effect"] === "string" ? raw["effect"] : typeof raw["message"] === "string" ? raw["message"] : null;
  const file = typeof raw["kill_switch_file"] === "string" ? raw["kill_switch_file"] : null;
  const mode = typeof raw["mode"] === "string" ? raw["mode"] : null;
  return { active, effect, file, mode };
}

/** Extra cycle fields the backend sends but `PaperCycleResult` does not declare (`summary`, `mirrored`, …). */
function cycleExtra<T extends string | number | boolean>(cycle: PaperCycleResult | null, key: string): T | null {
  if (!cycle) return null;
  const value = (cycle as unknown as Record<string, unknown>)[key];
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value as T;
  return null;
}

/** `/orders` answers under the key `orders`, while the client type reads `rows`. */
function orderRows(view: OrdersView | null): OrderRow[] {
  if (!view) return [];
  const raw = view as OrdersView & { orders?: OrderRow[] };
  return raw.rows ?? raw.orders ?? [];
}

function cycleFailureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409)
      return `۴۰۹ — موتور اجرا در حالت PAPER نیست و از ساخت سفارش خودداری کرد: ${error.detail} این صفحه هرگز حالت اجرا را تغییر نمی‌دهد.`;
    if (error.status === 503)
      return `۵۰۳ — داده بازار در دسترس نبود؛ سیستم fail-closed ماند و هیچ سفارشی ساخته یا تغییر نکرد: ${error.detail}`;
    return `${String(error.status)} — ${error.detail}`;
  }
  return describeError(error);
}

/* -------------------------------------------------------------------- page */

export default function PaperTrading() {
  const health = useAsync((signal) => getHealth(signal), []);
  const orders = useAsync((signal) => getOrders("all", signal), []);

  const [cycle, setCycle] = useState<PaperCycleResult | null>(null);
  const [cycleError, setCycleError] = useState<string | null>(null);
  const [cycleBusy, setCycleBusy] = useState<boolean>(false);

  const [modalOpen, setModalOpen] = useState<boolean>(false);
  const [modalAck, setModalAck] = useState<boolean>(false);
  const [killBusy, setKillBusy] = useState<boolean>(false);
  const [killResult, setKillResult] = useState<KillSwitchState | null>(null);
  const [killError, setKillError] = useState<string | null>(null);

  const killActive = killResult?.active ?? killSwitchFromHealth(health.data);
  const nextActive = killActive !== true;

  async function triggerCycle() {
    if (cycleBusy) return;
    setCycleBusy(true);
    setCycleError(null);
    try {
      const result = await runPaperCycle();
      setCycle(result);
    } catch (error: unknown) {
      setCycle(null);
      setCycleError(cycleFailureMessage(error));
    } finally {
      setCycleBusy(false);
      health.reload();
      orders.reload();
    }
  }

  async function confirmKillSwitch() {
    if (killBusy || !modalAck) return;
    setKillBusy(true);
    setKillError(null);
    try {
      const payload = await setKillSwitch(nextActive);
      setKillResult(readKillSwitchResponse(payload));
      setModalOpen(false);
      setModalAck(false);
    } catch (error: unknown) {
      setKillResult(null);
      setKillError(describeError(error));
    } finally {
      setKillBusy(false);
      health.reload();
      orders.reload();
    }
  }

  const outcomeRows = (cycle?.outcomes ?? []) as CycleOutcome[];

  const outcomeColumns: Column<CycleOutcome>[] = [
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "action", header: "اقدام پیشنهادی", render: (row) => <span className="num">{text(row.action)}</span>, sortValue: (row) => row.action },
    {
      key: "approved",
      header: "حکم ریسک‌گیت",
      render: (row) =>
        row.approved === null || row.approved === undefined ? (
          <ToneBadge tone="neutral">ثبت نشده</ToneBadge>
        ) : row.approved ? (
          <ToneBadge tone="pos">تأیید</ToneBadge>
        ) : (
          <ToneBadge tone="neg">رد</ToneBadge>
        ),
      sortValue: (row) => (typeof row.approved === "boolean" ? (row.approved ? 1 : 0) : null),
    },
    {
      key: "reasons",
      header: "دلایل",
      render: (row) => (row.reasons.length > 0 ? <span className="rtl-text">{row.reasons.join("؛ ")}</span> : "دلالی ثبت نشده است"),
    },
    {
      key: "fill",
      header: "پرشدن (کاغذی)",
      numeric: true,
      render: (row) =>
        row.filled_qty && typeof row.fill_price === "number" ? (
          <span className="num">
            {num(row.filled_qty, { max: 6 })} @ {money(row.fill_price, "USDT", { max: 4 })}
          </span>
        ) : (
          <span className="mute">پر نشده</span>
        ),
    },
    { key: "order", header: "شناسه سفارش", render: (row) => <span className="num">{text(row.order_id ?? null)}</span> },
    { key: "decision", header: "شناسه تصمیم", render: (row) => <span className="num">{text(row.decision_id ?? null)}</span> },
  ];

  const orderColumns: Column<OrderRow>[] = [
    { key: "client", header: "شناسه مشتری", render: (row) => <span className="num">{text(row.client_order_id)}</span>, sortValue: (row) => row.client_order_id },
    { key: "symbol", header: "نماد", render: (row) => <span className="num">{text(row.symbol)}</span>, sortValue: (row) => row.symbol },
    { key: "side", header: "جهت", render: (row) => <span className="num">{text(row.side)}</span>, sortValue: (row) => row.side },
    { key: "qty", header: "تعداد", numeric: true, sortValue: (row) => row.qty, render: (row) => num(row.qty, { max: 6 }) },
    { key: "price", header: "قیمت (USDT)", numeric: true, sortValue: (row) => row.price, render: (row) => money(row.price, "USDT", { max: 4 }) },
    {
      key: "status",
      header: "وضعیت",
      render: (row) => {
        const status = (row.status ?? "").toLowerCase();
        const badge = status === "filled" ? "pos" : status === "canceled" || status === "cancelled" || status === "rejected" ? "neg" : status === "open" || status === "new" || status === "partially_filled" ? "warn" : "neutral";
        return <ToneBadge tone={badge}>{text(row.status)}</ToneBadge>;
      },
      sortValue: (row) => row.status,
    },
    { key: "mode", header: "حالت", render: (row) => <Badge kind="paper" label={text(row.mode ?? "PAPER")} /> },
    { key: "created", header: "ساخت", sortValue: (row) => row.created_ts, render: (row) => dateTime(row.created_ts) },
    { key: "updated", header: "به‌روزرسانی", sortValue: (row) => row.updated_ts, render: (row) => dateTime(row.updated_ts) },
    { key: "decision", header: "تصمیم", render: (row) => <span className="num">{text(row.decision_id)}</span> },
  ];

  const rows = orderRows(orders.data);
  const cycleSummary = cycleExtra<string>(cycle, "summary");
  const cycleMirrored = cycleExtra<boolean>(cycle, "mirrored");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>اجرای کاغذی</h1>
          <p className="mute">
            همه‌ی اعداد این صفحه خروجی چرخه‌های شبیه‌سازی‌شده روی موجودی فرضی حساب PAPER است. هیچ سفارش واقعی ارسال
            نمی‌شود و این UI هیچ مسیر تغییر حالت به Live ندارد.
          </p>
        </div>
        <div className="btn-row">
          <ModeBadge mode={health.data?.mode ?? cycle?.mode ?? null} />
          <Badge kind="paper" label="همه‌ی خروجی‌ها کاغذی" />
        </div>
      </div>

      {health.error ? (
        <div className="notice danger" role="alert">
          <strong>اتصال به سرویس برقرار نشد.</strong>
          <span>{health.error}</span>
        </div>
      ) : null}

      <section className="grid two" aria-label="کنترل‌های اجرای کاغذی">
        <div className="card">
          <header>
            <h2>چرخه دستی کاغذی</h2>
            <Badge kind="paper" label="POST /cycles/paper" />
          </header>
          <p className="mute">
            با «اجرای یک چرخه» دقیقاً همان مسیر `trader.py cycle` یک بار اجرا می‌شود: تحلیل، ریسک‌گیت، و سفارش‌های
            شبیه‌سازی‌شده روی حساب فرضی. این دکمه فقط یک چرخه اجرا می‌کند و هرگز خودش را تکرار نمی‌کند.
          </p>
          <div className="btn-row">
            <button type="button" className="btn primary" onClick={triggerCycle} disabled={cycleBusy}>
              {cycleBusy ? "چرخه در حال اجرا…" : "اجرای یک چرخه"}
            </button>
            {cycle && !cycleBusy ? (
              <button type="button" className="btn ghost small" onClick={triggerCycle}>
                اجرای چرخه دیگر
              </button>
            ) : null}
          </div>
          {cycleBusy ? <LoadingBlock label="در انتظار پاسخ موتور اجرا…" /> : null}
          {cycleError ? (
            <div className="notice danger" role="alert">
              <strong>چرخه اجرا نشد.</strong>
              <span>{cycleError}</span>
            </div>
          ) : null}
          {cycle && !cycleBusy ? (
            <>
              <SuccessBlock title="چرخه کاغذی اجرا شد">
                <dl className="kv">
                  <dt>خلاصه موتور</dt>
                  <dd className="rtl-text">{text(cycleSummary)}</dd>
                  <dt>حالت</dt>
                  <dd className="rtl-text">{text(cycle.mode)}</dd>
                  <dt>kill switch</dt>
                  <dd className="rtl-text">{cycle.kill_switch === null || cycle.kill_switch === undefined ? NO_DATA : cycle.kill_switch ? "فعال — سفارش جدید ساخته نشد" : "خاموش"}</dd>
                  <dt>حالت امن</dt>
                  <dd className="rtl-text">{cycle.safe_state === null || cycle.safe_state === undefined ? NO_DATA : cycle.safe_state ? "بله — سیستم در حالت امن است" : "خیر"}</dd>
                  <dt>mirror در ledger</dt>
                  <dd className="rtl-text">{cycleMirrored === null ? "ثبت نشده" : cycleMirrored ? "بله، نتایج در ledger ثبت شد" : "خیر (dry-run)"}</dd>
                  <dt>equity پس از چرخه</dt>
                  <dd>{money(cycle.equity ?? null)}</dd>
                  <dt>تحقق‌نیافته</dt>
                  <dd className={`tone-${tone(cycle.unrealized ?? null)}`}>{signedMoney(cycle.unrealized ?? null)}</dd>
                  <dt>افت سرمایه</dt>
                  <dd>{pct(cycle.drawdown_pct ?? null, { max: 2 })}</dd>
                </dl>
              </SuccessBlock>
              {(cycle.errors?.length ?? 0) > 0 ? (
                <div className="notice warn" role="alert">
                  <strong>خطاهای گزارش‌شده در همین چرخه ({String(cycle.errors.length)}):</strong>
                  <ul>
                    {cycle.errors.map((message, index) => (
                      <li key={`cycle-error-${String(index)}`}>{message}</li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="stat-note">هیچ خطایی در پاسخ این چرخه گزارش نشده است.</p>
              )}
              <ProvenanceNote provenance={cycle.provenance ?? null} fallbackKind="paper" extra="خروجی لحظه‌ای POST /cycles/paper — فقط همین یک اجرا" />
            </>
          ) : null}
          {!cycle && !cycleBusy && !cycleError ? (
            <EmptyBlock label="چرخه‌ای در این نشست اجرا نشده است" hint="با «اجرای یک چرخه» یک اجرای واقعی (کاغذی) آغاز می‌شود؛ تا پیش از آن هیچ عددی نمایش داده نمی‌شود." />
          ) : null}
        </div>

        <div className="card">
          <header>
            <h2>توقف اضطراری (kill switch)</h2>
            <ToneBadge tone={killActive === true ? "neg" : killActive === false ? "pos" : "neutral"} title="وضعیت از /health و آخرین پاسخ /kill-switch">
              {killActive === true ? "فعال — توقف امن" : killActive === false ? "خاموش — چرخه‌ها مجاز" : NO_DATA}
            </ToneBadge>
          </header>
          <p className="mute">
            {killActive === true
              ? "توقف اضطراری فعال است: موتور هیچ سفارش جدیدی نمی‌سازد. موقعیت‌های بازِ کاغذی بسته نمی‌شوند و به فروش اجباری نمی‌رسند."
              : killActive === false
                ? "توقف اضطراری خاموش است؛ چرخه‌ها در همان حالت PAPER و با رعایت سقف‌های ریسک پیش می‌روند."
                : "وضعیت توقف از سرویس خوانده نشد؛ تا مشخص شدن، عددی درباره رفتار موتور ادعا نمی‌شود."}
          </p>
          {killError ? (
            <div className="notice danger" role="alert">
              <strong>تغییر وضعیت ممکن نشد.</strong>
              <span>{killError}</span>
            </div>
          ) : null}
          {killResult && !killError ? (
            <div className="notice" role="status">
              <span>
                {text(killResult.effect)}
                {killResult.file ? ` — فایل: ${killResult.file}` : ""}
                {killResult.mode ? ` — حالت: ${killResult.mode}` : ""}
              </span>
            </div>
          ) : null}
          <div className="btn-row">
            <button
              type="button"
              className={nextActive ? "btn danger" : "btn"}
              onClick={() => {
                setModalAck(false);
                setKillError(null);
                setModalOpen(true);
              }}
              disabled={killBusy}
            >
              {nextActive ? "فعال‌کردن توقف اضطراری…" : "آزادکردن توقف اضطراری…"}
            </button>
          </div>
          <p className="stat-note">
            این تنها کنترل اجرایی این رابط است. «شروع معامله زنده» در این سامانه وجود ندارد و با آزادکردن توقف نیز
            سیستم در همان حالت کاغذی ادامه می‌دهد.
          </p>
        </div>
      </section>

      {cycle && outcomeRows.length > 0 ? (
        <section className="card" aria-label="خروجی چرخه">
          <header>
            <h2>خروجی همین چرخه (نماد به نماد)</h2>
            <Badge kind="paper" label="پاسخ POST /cycles/paper" />
          </header>
          <Table
            rows={outcomeRows}
            getRowId={(row, index) => `outcome-${String(index)}-${row.symbol}`}
            caption="سرنوشت هر نماد در آخرین چرخه دستی"
            columns={outcomeColumns}
            defaultSort={{ key: "symbol", dir: "asc" }}
          />
          <p className="stat-note">
            «پر نشده» یعنی سفارش کاغذی در این چرخه fill نخورد؛ این یک حدس UI از نبود filled_qty در پاسخ موتور است،
            نه ثبت صریح وضعیت در قرارداد.
          </p>
        </section>
      ) : cycle && outcomeRows.length === 0 && !cycleBusy ? (
        <section className="card" aria-label="خروجی چرخه">
          <header>
            <h2>خروجی همین چرخه</h2>
            <Badge kind="paper" />
          </header>
          <EmptyBlock label="موردی ثبت نشده است" hint="موتور در این چرخه برای هیچ نمادی outcome برنگرداند (مثلاً توقف فعال یا نبود فرصت)." />
        </section>
      ) : null}

      <section className="card" aria-label="سفارش‌ها و پرشدن‌ها">
        <header>
          <h2>سفارش‌های کاغذی (همه وضعیت‌ها)</h2>
          <Badge kind="paper" label="GET /orders?status=all" />
        </header>
        <StateBlocks state={orders} emptyLabel={NO_DATA} emptyHint="هیچ سفارشی در فایل کارگزار کاغذی ثبت نشده است — یعنی هنوز سفارشی ساخته نشده، نه اینکه سفارشی موفق بوده باشد.">
          {() =>
            rows.length === 0 ? (
              <EmptyBlock label="۰ سفارش ثبت‌شده" hint="فایل state/paper_broker.json سفارشی ندارد. صفر واقعی است، نه داده نمونه." />
            ) : (
              <>
                <Table
                  rows={rows}
                  getRowId={(row, index) => row.client_order_id ?? `order-${String(index)}`}
                  caption="دفتر سفارش‌های شبیه‌سازی‌شده"
                  columns={orderColumns}
                  defaultSort={{ key: "updated", dir: "desc" }}
                />
                <ProvenanceNote provenance={orders.data?.provenance ?? null} fallbackKind="paper" extra="منبع: state/paper_broker.json — فقط حالت PAPER" />
                <p className="stat-note">
                  پرشدن‌ها از وضعیت همین سفارش‌ها و rows ثبت‌شده در /trades قابل ردیابی است؛ endpoint مستقل fills در
                  قرارداد v1 تعریف نشده است و عدد پرشدن از خودِ پاسخ سفارش خوانده می‌شود.
                </p>
              </>
            )
          }
        </StateBlocks>
      </section>

      <Modal
        open={modalOpen}
        title={nextActive ? "فعال‌کردن توقف اضطراری" : "آزادکردن توقف اضطراری"}
        description="این تغییر وضعیت فقط روی حالت اجرای کاغذی اثر می‌گذارد و در audit ثبت می‌شود."
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button type="button" className="btn primary" onClick={confirmKillSwitch} disabled={killBusy || !modalAck}>
              {killBusy ? "در حال ارسال…" : nextActive ? "تأیید و فعال‌کردن توقف" : "تأیید و آزادکردن توقف"}
            </button>
            <button type="button" className="btn ghost" onClick={() => setModalOpen(false)} disabled={killBusy}>
              انصراف
            </button>
          </>
        }
      >
        <p>
          {nextActive
            ? "با فعال‌کردن توقف اضطراری: هیچ سفارش جدیدی (حتی کاغذی) ثبت نمی‌شود؛ اما این کار موقعیت‌های باز را به‌زور نمی‌بندد و فروش اجباری ایجاد نمی‌کند. مدیریت خروجِ موقعیت‌های باز بر اساس rules استراتژی و با داده تازه انجام می‌شود."
            : "با آزادکردن توقف اضطراری: ثبت سفارش‌های جدید کاغذی از سر گرفته می‌شود. این به‌معنی ورود به حالت زنده نیست — سیستم در همان حالت PAPER و روی موجودی فرضی ادامه می‌دهد و هیچ مسیر سرویس زنده در این سامانه وجود ندارد."}
        </p>
        <label className="field">
          <input type="checkbox" checked={modalAck} onChange={(event) => setModalAck(event.target.checked)} />
          <span className="rtl-text">
            {nextActive
              ? "می‌دانم که توقف فقط جلوی سفارش‌های جدید را می‌گیرد و موقعیت‌های باز بسته نمی‌شوند."
              : "می‌دانم که ادامه فعالیت فقط در همان حالت کاغذی (PAPER) است."}
          </span>
        </label>
        <p className="stat-note">
          وضعیت فعلی از /health:{" "}
          {killActive === true ? "فعال" : killActive === false ? "خاموش" : NO_DATA} — پس از ارسال، پاسخ صریح سرور
          («effect») همین‌جا نمایش داده می‌شود.
        </p>
      </Modal>
    </>
  );
}
