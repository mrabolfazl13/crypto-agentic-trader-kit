import { Clock, Info } from "lucide-react";
import { getDecisions, useAsync, type Decision } from "../api/client";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "./StateBlocks";
import { Badge as UiBadge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "./ui/dialog";
import { NO_DATA, dateTime, num, text } from "../format";

const LIMIT = 50;

const REASON_FA: Record<string, string> = {
  regime_filter: "فیلتر رژیم بازار: شرایط ورود تأیید نشد",
  no_signal: "استراتژی سیگنال ورودی نداد",
  confidence: "اطمینان تصمیم زیر آستانه بود",
  risk_gate: "ریسک‌گیت: ورود رد شد",
  kill_switch: "توقف اضطراری فعال است",
  stale_data: "داده قدیمی بود؛ سیستم در حالت امن ماند",
  open_position: "یک موقعیت باز از قبل وجود دارد",
};

function reasonCode(reason: string): string {
  const code = reason.includes(":") ? reason.slice(reason.lastIndexOf(":") + 1) : reason;
  return code.trim();
}

function explain(reason: string): string {
  const code = reasonCode(reason);
  return REASON_FA[code] ?? reason;
}

function actionTone(action: string | null): "pos" | "neg" | "warn" | "neutral" {
  switch ((action ?? "").toUpperCase()) {
    case "BUY":
      return "pos";
    case "SELL":
      return "neg";
    case "NO_TRADE":
      return "warn";
    default:
      return "neutral";
  }
}

function actionLabel(action: string | null): string {
  switch ((action ?? "").toUpperCase()) {
    case "BUY":
      return "خرید";
    case "SELL":
      return "فروش";
    case "NO_TRADE":
      return "بدون معامله";
    default:
      return text(action);
  }
}

interface DecisionGroup {
  key: string;
  symbol: string;
  action: string | null;
  reason: string;
  count: number;
  approved: number;
  last: Decision;
}

/** Repeated identical decisions (same symbol, action, reason) collapse into one row. */
function groupDecisions(rows: Decision[]): DecisionGroup[] {
  const map = new Map<string, DecisionGroup>();
  for (const row of rows) {
    const reason = row.rationale[0] ?? "";
    const key = `${row.symbol}|${row.action ?? ""}|${reasonCode(reason)}`;
    const previous = map.get(key);
    if (previous) {
      previous.count += 1;
      if (row.approved) previous.approved += 1;
    } else {
      map.set(key, { key, symbol: row.symbol, action: row.action, reason, count: 1, approved: row.approved ? 1 : 0, last: row });
    }
  }
  return [...map.values()];
}

export default function DecisionsFeed() {
  const decisions = useAsync((signal) => getDecisions({ limit: LIMIT }, signal), []);
  const rows = decisions.data ?? [];
  const groups = groupDecisions(rows);
  const approved = rows.filter((row) => row.approved).length;
  const noTrade = rows.filter((row) => (row.action ?? "").toUpperCase() === "NO_TRADE").length;

  return (
    <Card aria-label="آخرین تصمیم‌های موتور">
      <CardHeader>
        <div className="flex flex-col gap-0.5">
          <CardTitle className="flex items-center gap-2">
            <Clock size={17} className="text-accent" aria-hidden="true" />
            تصمیم‌های موتور
          </CardTitle>
          <p className="m-0 text-[12px] text-mute">{`آخرین ${num(LIMIT, { max: 0 })} تصمیم ثبت‌شده در /decisions — داخل کادر اسکرول کنید`}</p>
        </div>
        <div className="flex items-center gap-1.5">
          <UiBadge tone={approved > 0 ? "pos" : "neutral"} size="sm">{`تأییدشده: ${num(approved, { max: 0 })}`}</UiBadge>
          <UiBadge tone="warn" size="sm">{`بدون معامله: ${num(noTrade, { max: 0 })}`}</UiBadge>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-2">
        {decisions.loading ? <LoadingBlock label="خواندن تصمیم‌ها…" /> : null}
        {decisions.error ? <ErrorBlock message={decisions.error} onRetry={decisions.reload} /> : null}

        {!decisions.loading && !decisions.error && rows.length === 0 ? (
          <EmptyBlock
            label={NO_DATA}
            hint="هنوز تصمیمی ثبت نشده است؛ تا زمانی که چرخه‌ی پیپر اجرا نشود، موتور تصمیمی نمی‌سازد."
          />
        ) : null}

        {rows.length > 0 ? (
          <div className="feed-scroll" role="region" aria-label="فهرست تصمیم‌ها" tabIndex={0}>
            <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
              {groups.map((group) => (
                <li key={group.key}>
                  <DecisionGroupRow group={group} />
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <p className="m-0 flex items-start gap-1.5 rounded-md border border-line bg-surface2 px-2.5 py-2 text-[12px] leading-6 text-dim">
          <Info size={13} className="mt-1 shrink-0 text-info" aria-hidden="true" />
          <span>
            «بدون معامله» یعنی موتور داده را دید و یکی از فیلترها (رژیم بازار یا ریسک‌گیت) ورود را رد کرد؛ نه اینکه داده‌ای نبوده باشد. هر رد شدن با دلیل در
            لاگ تصمیم ثبت می‌شود و هیچ سفارشی ساخته نمی‌شود.
          </span>
        </p>
      </CardContent>
    </Card>
  );
}

function DecisionGroupRow({ group }: { group: DecisionGroup }) {
  const row = group.last;
  const tone = actionTone(row.action);
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm" className="h-auto w-full items-start justify-start gap-2 border border-transparent px-2.5 py-1.5 hover:border-line">
          <span aria-hidden="true" className={`dot mt-1.5 ${row.approved ? "ok" : "warn"}`} />
          <span className="flex min-w-0 grow flex-col gap-0.5 text-start">
            <span className="flex flex-wrap items-center gap-1.5">
              <span className="num text-[12px] font-semibold">{text(row.symbol)}</span>
              <UiBadge tone={tone} size="sm">
                {actionLabel(row.action)}
              </UiBadge>
              {group.count > 1 ? <span className="num text-[11px] text-mute">{`×${num(group.count, { max: 0 })}`}</span> : null}
              <span className="grow" />
              <span className="num text-[11px] text-mute">{dateTime(row.ts)}</span>
            </span>
            <span className="truncate text-[11px] text-dim">{explain(group.reason)}</span>
          </span>
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`جزئیات تصمیم ${text(row.symbol)}`}</DialogTitle>
          <DialogDescription>
            {`شناسه‌ی آخرین رکورد: ${row.id}`}
            {group.count > 1 ? ` · این ردیف ${num(group.count, { max: 0 })} تصمیم یکسان را خلاصه می‌کند` : ""}
          </DialogDescription>
        </DialogHeader>

        <dl className="m-0 grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1 text-[13px]">
          <dt className="text-dim">زمان</dt>
          <dd className="m-0">{dateTime(row.ts)}</dd>
          <dt className="text-dim">نماد</dt>
          <dd className="num m-0">{text(row.symbol)}</dd>
          <dt className="text-dim">اقدام</dt>
          <dd className="m-0">
            <UiBadge tone={tone}>{actionLabel(row.action)}</UiBadge>
          </dd>
          <dt className="text-dim">تأیید ریسک</dt>
          <dd className="m-0">{row.approved ? "تأییدشده" : "رد شده"}</dd>
          <dt className="text-dim">حالت</dt>
          <dd className="m-0">{text(row.mode)}</dd>
          <dt className="text-dim">استراتژی</dt>
          <dd className="m-0">{text(row.strategy_label)}</dd>
          <dt className="text-dim">منبع</dt>
          <dd className="m-0">{text(row.source)}</dd>
        </dl>

        <section>
          <h3 className="mb-1 mt-0 text-[13px] font-semibold">دلیل‌ها</h3>
          {row.rationale.length === 0 ? (
            <p className="m-0 text-[12px] text-mute">{NO_DATA}</p>
          ) : (
            <ul className="m-0 list-disc ps-5 text-[13px]">
              {row.rationale.map((reason) => (
                <li key={reason}>
                  {explain(reason)} <code className="num text-[11px] text-mute">{reasonCode(reason)}</code>
                </li>
              ))}
            </ul>
          )}
        </section>

        {Array.isArray(row.risk.reasons) && row.risk.reasons.length > 0 ? (
          <section>
            <h3 className="mb-1 mt-0 text-[13px] font-semibold">دلایل ریسک‌گیت</h3>
            <ul className="m-0 list-disc ps-5 text-[13px] text-neg">
              {row.risk.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </section>
        ) : null}

        {Array.isArray(row.risk.checks_passed) && row.risk.checks_passed.length > 0 ? (
          <section>
            <h3 className="mb-1 mt-0 text-[13px] font-semibold">بررسی‌های ریسک</h3>
            <ul className="m-0 list-disc ps-5 text-[13px] text-dim">
              {row.risk.checks_passed.map((check) => (
                <li key={check}>{check}</li>
              ))}
            </ul>
          </section>
        ) : null}

        <p className="m-0 text-[12px] text-mute">
          این جزئیات مستقیماً از رکورد تصمیم API می‌آید؛ هیچ عدد یا دلیلی در رابط ساخته نمی‌شود.
        </p>
      </DialogContent>
    </Dialog>
  );
}
