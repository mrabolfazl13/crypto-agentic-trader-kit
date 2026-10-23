/**
 * Persian formatting helpers.
 *
 * Every number in this UI is Latin-digit but grouped the way a Persian reader
 * expects (`fa-IR-u-nu-latn`). A missing value is NEVER substituted with a
 * sample number: it renders as `NO_DATA`.
 */

/** The only allowed rendering of an absent value. */
export const NO_DATA = "داده‌ای موجود نیست";

const LOCALE = "fa-IR-u-nu-latn";

export function isAbsent(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "number" && !Number.isFinite(value)) return true;
  if (typeof value === "string") {
    const t = value.trim();
    return t === "" || t === "no_data" || t.toLowerCase() === "none" || t === "n/a";
  }
  return false;
}

type NumOpts = {
  min?: number;
  max?: number;
  sign?: "auto" | "always";
};

export function num(value: number | null | undefined, opts: NumOpts = {}): string {
  if (isAbsent(value) || typeof value !== "number") return NO_DATA;
  const max = opts.max ?? 2;
  const out = value.toLocaleString(LOCALE, {
    minimumFractionDigits: opts.min ?? 0,
    maximumFractionDigits: max,
  });
  return opts.sign === "always" && value > 0 ? `+${out}` : out;
}

/** Signed value: used for P&L where the sign carries meaning. */
export function signed(value: number | null | undefined, opts: NumOpts = {}): string {
  if (isAbsent(value) || typeof value !== "number") return NO_DATA;
  const base = num(Math.abs(value), opts);
  if (base === NO_DATA) return base;
  if (value > 0) return `+${base}`;
  if (value < 0) return `−${base}`;
  return base;
}

export function pct(value: number | null | undefined, opts: NumOpts = {}): string {
  if (isAbsent(value) || typeof value !== "number") return NO_DATA;
  return `${signed(value, opts)}%`;
}

export function plainPct(value: number | null | undefined, opts: NumOpts = {}): string {
  if (isAbsent(value) || typeof value !== "number") return NO_DATA;
  return `${num(value, opts)}%`;
}

export function money(value: number | null | undefined, unit = "USDT", opts: NumOpts = { max: 2 }): string {
  if (isAbsent(value) || typeof value !== "number") return NO_DATA;
  return `${num(value, opts)} ${unit}`;
}

export function signedMoney(value: number | null | undefined, unit = "USDT", opts: NumOpts = { max: 2 }): string {
  if (isAbsent(value) || typeof value !== "number") return NO_DATA;
  return `${signed(value, opts)} ${unit}`;
}

export function text(value: string | null | undefined, fallback = NO_DATA): string {
  return isAbsent(value) ? fallback : String(value);
}

/** Tone for a P&L-like number. `null` is deliberately neutral. */
export function tone(value: number | null | undefined): "pos" | "neg" | "neutral" {
  if (isAbsent(value) || typeof value !== "number") return "neutral";
  if (value > 0) return "pos";
  if (value < 0) return "neg";
  return "neutral";
}

function toDate(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && isAbsent(value)) return null;
  const d = typeof value === "number"
    // Backend stores epoch seconds; be forgiving about milliseconds.
    ? new Date(value < 1e11 ? value * 1000 : value)
    : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function dateTime(value: string | number | null | undefined): string {
  const d = toDate(value);
  if (!d) return NO_DATA;
  return d.toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short", timeZone: "UTC" });
}

export function dateOnly(value: string | number | null | undefined): string {
  const d = toDate(value);
  if (!d) return NO_DATA;
  return d.toLocaleDateString(LOCALE, { dateStyle: "short", timeZone: "UTC" });
}

export function timeOnly(value: string | number | null | undefined): string {
  const d = toDate(value);
  if (!d) return NO_DATA;
  return d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC" });
}

/** ISO date (yyyy-mm-dd) in UTC — used for report routes and filters. */
export function isoDay(value: string | number | Date | null | undefined): string {
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  const d = toDate(value);
  return d ? d.toISOString().slice(0, 10) : "";
}

/** Relative "چند دقیقه پیش" copy for the freshness indicator. */
export function sinceLabel(value: string | number | null | undefined, now = Date.now()): string {
  const d = toDate(value);
  if (!d) return NO_DATA;
  const seconds = Math.max(0, Math.round((now - d.getTime()) / 1000));
  if (seconds < 60) return `${num(seconds, { max: 0 })} ثانیه پیش`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${num(minutes, { max: 0 })} دقیقه پیش`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${num(hours, { max: 0 })} ساعت پیش`;
  return `${num(Math.round(hours / 24), { max: 0 })} روز پیش`;
}

export function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}
