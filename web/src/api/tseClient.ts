/**
 * Typed client for the read-only TSE view (`/api/v1/tse/*`).
 *
 * Same rules as the crypto client: every optional number is `number | null`
 * (nothing is rendered as a fabricated zero), and there is no order, mode or
 * configuration mutation here — only GET.
 */

import { unwrap, url, type Provenance } from "./client";

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
  quote: {
    symbol: string;
    bid: number;
    ask: number;
    mid: number;
    spread_pct: number;
    ts: string;
    source: string;
  };
  venue_rules: { lot_size: number | null; price_currency: string; quantity_unit: string };
  provenance: Provenance & { single_source?: boolean | null };
}

export interface TseBookLevel {
  level: number | null;
  bid_price: number;
  bid_qty: number;
  bid_orders: number;
  ask_price: number;
  ask_qty: number;
  ask_orders: number;
}

export interface TseBook {
  symbol: string;
  levels: TseBookLevel[];
  count: number;
  provenance: Provenance & { single_source?: boolean | null };
}

async function getJSON<T>(path: string, query?: Record<string, string | number | null | undefined>, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url(path, query), {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  return unwrap<T>(response);
}

export const getTseMarket = (flow: 1 | 2 = 1, signal?: AbortSignal) =>
  getJSON<TseMarket>("/tse/market", { flow }, signal);

export const getTseInstruments = (limit = 500, signal?: AbortSignal) =>
  getJSON<TseInstrumentsPage>("/tse/instruments", { limit }, signal);

export const getTseInstrument = (symbol: string, signal?: AbortSignal) =>
  getJSON<TseInstrumentDetail>("/tse/instrument", { symbol }, signal);

export const getTseBook = (symbol: string, signal?: AbortSignal) =>
  getJSON<TseBook>("/tse/book", { symbol }, signal);
