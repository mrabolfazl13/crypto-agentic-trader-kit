/**
 * Typed client for the read-only TSE view (`/api/v1/tse/*`).
 *
 * Same rules as the crypto client: every optional number is `number | null`
 * (nothing is rendered as a fabricated zero), and there is no order, mode or
 * configuration mutation here — only GET.
 */
import { ApiError, unwrap, url, type Provenance } from "./client";

export type TseSessionState = "open" | "closed" | "pre_open";

export interface TseSession {
  state: TseSessionState;
  label: string;
  session_day: boolean;
  session_open: string | null;
  session_close: string | null;
  next_open: string;
  tehran_now: string;
}

export interface TseBoard {
  flow: number;
  index_last_value: number | null;
  index_change: number | null;
  index_equal_weighted: number | null;
  trade_count: number | null;
  volume_shares: number | null;
  value_rial: number | null;
  market_value: number | null;
  activity_date: number | null;
  activity_time: number | null;
  state_code: string | null;
  state_title: string | null;
}

export interface TseMarket {
  session: TseSession;
  freshness_reference: string;
  board: TseBoard;
  board_size: number;
  allowlist: string[];
  currency: string;
  quantity_unit: string;
  provenance: Provenance & { single_source?: boolean | null };
}

export interface TseInstrumentRow {
  ins_code: string;
  ticker: string | null;
  name: string | null;
  isin: string | null;
  group: string | null;
  last_price: number | null;
  price_yesterday: number | null;
  change_pct: number | null;
  best_bid: number | null;
  best_ask: number | null;
  volume_shares: number | null;
  trade_value_rial: number | null;
  trade_count: number | null;
  base_volume: number | null;
  band_low: number | null;
  band_high: number | null;
  session_date: number | null;
  session_time: number | null;
}

export interface TseUniverseSummary {
  available: boolean;
  count: number;
  generated_at: string | null;
  source: Record<string, unknown> | string | null;
  digest: string | null;
  error: string | null;
}

export interface TseInstrumentsPage {
  count: number;
  returned: number;
  instruments: TseInstrumentRow[];
  universe: TseUniverseSummary;
  provenance: Provenance & { single_source?: boolean | null };
}

export interface TseQuote {
  symbol: string;
  bid: number;
  ask: number;
  mid: number;
  spread_pct: number;
  ts: string;
  source: string;
}

export interface TseInstrumentDetail {
  display: { insCode: string; ticker: string | null; name: string | null; isin: string | null };
  band: {
    insCode: string;
    ticker: string | null;
    price_yesterday: number | null;
    band_low: number | null;
    band_high: number | null;
    band_pct: number | null;
    last_price: number | null;
    base_volume: number | null;
    group: string | null;
    band_source: string;
  };
  // A board with only one side published has no two-sided quote. That is absent
  // data, so the API returns null plus the reason rather than failing the card.
  quote: TseQuote | null;
  quote_note: string | null;
  venue_rules: { lot_size: number | null; price_currency: string; quantity_unit: string };
  provenance: Provenance & { single_source?: boolean | null };
}

export interface TseBookLevel {
  level: number | null;
  bid_price: number | null;
  bid_qty: number;
  bid_orders: number;
  ask_price: number | null;
  ask_qty: number;
  ask_orders: number;
}

export interface TseBook {
  symbol: string;
  levels: TseBookLevel[];
  count: number;
  // Set when the exchange published no depth for this symbol — an absent value,
  // not a failed request.
  note: string | null;
  provenance: Provenance & { single_source?: boolean | null };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * A 200 whose body is not the shape we declared is not a data gap — it means a
 * stale server build is answering, so say that instead of rendering blanks.
 */
function requireShape<T>(value: unknown, path: string, valid: (body: Record<string, unknown>) => boolean): T {
  if (!isRecord(value) || !valid(value)) {
    throw new ApiError(
      200,
      `پاسخ ${path} ساختار مورد انتظار را ندارد؛ احتمالاً سرور با نسخه‌ی قدیمی در حال اجراست — سرویس را دوباره راه‌اندازی کنید.`,
      path,
    );
  }
  return value as T;
}

async function getJSON<T>(
  path: string,
  query?: Record<string, string | number | null | undefined>,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(url(path, query), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  const contentType = response.headers.get("content-type") ?? "";
  if (response.ok && !contentType.toLowerCase().includes("application/json")) {
    throw new ApiError(
      response.status,
      `پاسخ ${path} JSON نیست (${contentType || "بدون content-type"})؛ احتمالاً نسخه‌ی قدیمی سرور در حال اجراست — آن را دوباره راه‌اندازی کنید.`,
      new URL(response.url).pathname,
    );
  }
  return unwrap<T>(response);
}

export const getTseMarket = async (flow: 1 | 2 = 1, signal?: AbortSignal): Promise<TseMarket> =>
  requireShape<TseMarket>(
    await getJSON<TseMarket>("/tse/market", { flow }, signal),
    "/tse/market",
    (body) => isRecord(body["session"]) && isRecord(body["board"]),
  );

export const getTseInstruments = async (limit = 500, signal?: AbortSignal): Promise<TseInstrumentsPage> =>
  requireShape<TseInstrumentsPage>(
    await getJSON<TseInstrumentsPage>("/tse/instruments", { limit }, signal),
    "/tse/instruments",
    (body) => Array.isArray(body["instruments"]),
  );

export const getTseInstrument = async (symbol: string, signal?: AbortSignal): Promise<TseInstrumentDetail> =>
  requireShape<TseInstrumentDetail>(
    await getJSON<TseInstrumentDetail>("/tse/instrument", { symbol }, signal),
    "/tse/instrument",
    (body) => isRecord(body["band"]) && (body["quote"] === null || isRecord(body["quote"])),
  );

export const getTseBook = async (symbol: string, signal?: AbortSignal): Promise<TseBook> =>
  requireShape<TseBook>(
    await getJSON<TseBook>("/tse/book", { symbol }, signal),
    "/tse/book",
    (body) => Array.isArray(body["levels"]),
  );
