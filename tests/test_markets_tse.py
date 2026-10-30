"""Offline tests for the TSE adapter and the Tehran session calendar.

Every payload is the recorded TSETMC response in ``tests/fixtures``; no test
touches the network, an exchange clock or a broker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cat.data import DataUnavailable
from cat.markets import calendar
from cat.markets.tse import SOURCE_MAX_DAILY_ROWS, TEHRAN, TseProvider, week_anchor
from tse_helpers import (
    FOLD_CODE,
    FOLD_ISIN,
    FOLD_TICKER,
    FixtureClient,
    fixture_provider,
    load_fixture,
    marketwatch_rows,
    write_universe,
)

UTC = timezone.utc


def tehran(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TEHRAN)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def test_session_calendar_matches_the_recorded_week():
    assert calendar.phase(tehran(2026, 10, 21, 10, 0))["state"] == calendar.OPEN      # Wednesday
    assert calendar.phase(tehran(2026, 10, 21, 13, 0))["state"] == calendar.CLOSED    # after 12:30
    assert calendar.phase(tehran(2026, 10, 22, 10, 0))["state"] == calendar.CLOSED    # Thursday
    assert calendar.phase(tehran(2026, 10, 23, 10, 0))["state"] == calendar.CLOSED    # Friday
    assert calendar.phase(tehran(2026, 10, 24, 9, 30))["state"] == calendar.OPEN      # Saturday


def test_staleness_reference_stops_at_the_last_session_close():
    friday_night = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)  # 23:30 Tehran, weekend
    reference = calendar.staleness_reference(friday_night.timestamp())
    expected = tehran(2026, 10, 21, 12, 30).timestamp()       # Wednesday close
    assert reference == pytest.approx(expected)
    assert calendar.next_session_open(friday_night) == tehran(2026, 10, 24, 9, 0)


def test_bar_gap_is_measured_in_sessions_not_calendar_days():
    wednesday = tehran(2026, 10, 21, 9, 0).timestamp()
    saturday = tehran(2026, 10, 24, 9, 0).timestamp()
    thursday = tehran(2026, 10, 22, 9, 0).timestamp()
    next_saturday = tehran(2026, 10, 31, 9, 0).timestamp()

    assert calendar.bar_gap_ok(wednesday, saturday) is True          # weekend
    assert calendar.bar_gap_ok(tehran(2026, 10, 20, 9, 0).timestamp(), wednesday) is True
    assert calendar.bar_gap_ok(wednesday, thursday) is False         # no Thursday session
    assert calendar.bar_gap_ok(saturday, next_saturday) is True      # weekly bars
    assert calendar.bar_gap_ok(wednesday, tehran(2026, 10, 25, 9, 0).timestamp()) is False


# ---------------------------------------------------------------------------
# Universe / instrument identity
# ---------------------------------------------------------------------------

def test_universe_builder_keeps_ordinary_shares_and_drops_rights():
    from build_tse_universe import build_instruments, is_ordinary_share

    assert is_ordinary_share(FOLD_ISIN) is True
    assert is_ordinary_share("IRO1CRBN0003") is False   # rights series
    assert is_ordinary_share("IRT1TERME001") is False   # fund
    assert is_ordinary_share("IRB3ENTEKH01") is False   # debt

    instruments, excluded = build_instruments(marketwatch_rows(), limit=0)
    codes = {row["insCode"] for row in instruments}
    tickers = {row["ticker"] for row in instruments}
    assert FOLD_CODE in codes and FOLD_TICKER in tickers
    assert "شكربن3" not in tickers                      # a recorded rights ticker
    assert excluded["not_ordinary_share"] > 0


def test_instrument_index_resolves_ticker_isin_and_numeric_code(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)

    assert provider.canonical_symbol(FOLD_TICKER) == FOLD_CODE
    assert provider.canonical_symbol(FOLD_CODE) == FOLD_CODE
    assert provider.canonical_symbol(FOLD_ISIN) == FOLD_CODE
    with pytest.raises(DataUnavailable):
        provider.canonical_symbol("NOT_A_TSE_TICKER")

    summary = provider.index.summary()
    assert summary["available"] is True
    assert summary["count"] > 0
    assert summary["digest"]


# ---------------------------------------------------------------------------
# Quotes, book, band, filters
# ---------------------------------------------------------------------------

def test_quote_timestamp_comes_from_the_exchange_session_not_the_wall_clock(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)

    quote = provider.quote(FOLD_TICKER)
    expected = datetime(2026, 10, 21, 12, 30, 2, tzinfo=TEHRAN).timestamp()
    assert quote.ts == pytest.approx(expected)
    assert quote.bid == 4230.0 and quote.ask == 4240.0
    assert quote.source == "tsetmc"
    assert quote.spread_pct > 0


def test_quote_refuses_a_one_sided_or_inverted_book(workspace: Path):
    write_universe(workspace)
    rows = marketwatch_rows()
    fold = next(row for row in rows if row["insCode"] == FOLD_CODE)

    one_sided = dict(fold, pmo=0)
    provider = fixture_provider(workspace, {"marketwatch": {"marketwatch": [one_sided]}})
    with pytest.raises(DataUnavailable, match="two-sided"):
        provider.quote(FOLD_CODE)

    inverted = dict(fold, pmd=4300, pmo=4200)
    provider = fixture_provider(workspace, {"marketwatch": {"marketwatch": [inverted]}})
    with pytest.raises(DataUnavailable, match="inverted"):
        provider.quote(FOLD_CODE)


def test_price_band_and_filters_are_exchange_values(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)

    band = provider.price_band(FOLD_CODE)
    assert band["band_low"] == 4210.0
    assert band["band_high"] == 4470.0
    assert band["price_yesterday"] == 4340.0
    assert band["last_price"] == 4220.0
    assert band["band_source"].startswith("tsetmc:")

    filters = provider.filters(FOLD_CODE)
    assert filters.price_precision == 0      # whole Rials
    assert filters.qty_precision == 0        # whole shares
    assert filters.min_qty == 100.0          # one lot, documented venue rule
    assert filters.tradable is True
    assert filters.fee_pct == pytest.approx(0.325)


def test_book_maps_best_limits(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)

    levels = provider.book(FOLD_CODE)
    recorded = load_fixture("tse_bestlimits_foolad.json")["bestLimits"]
    assert len(levels) == len(recorded) == 5
    assert levels[0]["bid_price"] == float(recorded[0]["pMeDem"])
    assert levels[0]["ask_price"] == float(recorded[0]["pMeOf"])
    assert levels[0]["bid_qty"] == float(recorded[0]["qTitMeDem"])


def test_empty_side_of_the_book_is_an_absent_price_not_zero(workspace: Path):
    """A level with no orders publishes 0; reporting 0 ریال would be a lie."""
    write_universe(workspace)
    provider = fixture_provider(workspace, {
        "book": {
            "bestLimits": [
                {"number": 1, "pMeDem": 3050, "qTitMeDem": 81930117, "zOrdMeDem": 293,
                 "pMeOf": 0, "qTitMeOf": 0, "zOrdMeOf": 0},
            ]
        }
    })

    level = provider.book(FOLD_CODE)[0]
    assert level["bid_price"] == 3050.0
    assert level["ask_price"] is None
    assert level["ask_qty"] == 0.0


def test_one_sided_marketwatch_row_refuses_a_quote(workspace: Path):
    """Suspended/blocked names have no two-sided price, so no Quote is built."""
    write_universe(workspace)
    rows = marketwatch_rows()
    for row in rows:
        if str(row.get("insCode")) == FOLD_CODE:
            row["pmo"] = 0
    provider = fixture_provider(workspace, {"marketwatch": {"marketwatch": rows}})

    with pytest.raises(DataUnavailable, match="no two-sided quote"):
        provider.quote(FOLD_CODE)


def test_market_state_is_the_exchange_overview(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)

    state = provider.market_state(1)
    expected = load_fixture("tse_marketoverview_f1.json")["marketOverview"]
    assert state["state_title"] == expected["marketStateTitle"] == "بسته"
    assert state["activity_date"] == expected["marketActivityDEven"] == 20261021
    assert state["index_last_value"] == pytest.approx(float(expected["indexLastValue"]))
    assert state["flow"] == 1


def test_search_response_is_passed_through_without_invention(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    # The recorded payload for this query was empty; the adapter must say so
    # rather than fabricate a hit.
    assert provider.search("فولاد") == []


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------

def test_daily_klines_map_exchange_fields(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    bars = provider.klines(FOLD_CODE, "1d", 30)
    recorded = load_fixture("tse_daily_foolad.json")["closingPriceDaily"]

    assert len(bars) == 30
    assert bars[0].open_time == tehran(2026, 9, 12, 9, 0).timestamp()
    assert bars[-1].open_time == tehran(2026, 10, 21, 9, 0).timestamp()
    newest = recorded[0]
    assert bars[-1].close == float(newest["pClosing"])
    assert bars[-1].open == float(newest["priceFirst"])
    assert bars[-1].high == float(newest["priceMax"])
    assert bars[-1].low == float(newest["priceMin"])
    assert bars[-1].base_volume == float(newest["qTotTran5J"])
    assert all(bar.closed for bar in bars)


def test_weekly_bars_are_saturday_anchored_aggregates(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    daily = provider.klines(FOLD_CODE, "1d", 30)
    weekly = provider.klines(FOLD_CODE, "1w", 30)

    assert weekly
    for bar in weekly:
        moment = datetime.fromtimestamp(bar.open_time, tz=TEHRAN)
        assert moment.weekday() == 5 and (moment.hour, moment.minute) == (9, 0)

    anchor = datetime.fromtimestamp(weekly[0].open_time, tz=TEHRAN).date()
    first_week = [
        bar for bar in daily
        if week_anchor(datetime.fromtimestamp(bar.open_time, tz=TEHRAN).date()) == anchor
    ]
    assert weekly[0].open == first_week[0].open
    assert weekly[0].close == first_week[-1].close
    assert weekly[0].high == max(bar.high for bar in first_week)
    assert weekly[0].low == min(bar.low for bar in first_week)
    assert weekly[-1].closed is False   # the running week never enters signals


def test_unsupported_timeframe_is_refused(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    with pytest.raises(DataUnavailable, match="daily history only"):
        provider.klines(FOLD_CODE, "1h", 30)


def test_daily_rows_never_asks_for_more_depth_than_the_source_has(workspace: Path):
    """top>200 returns the same 200 rows (measured 2026-10-30); the request must say so."""
    write_universe(workspace)
    provider = fixture_provider(workspace)
    provider.daily_rows(FOLD_CODE, 1000)
    url = provider.client.calls[-1][0]
    assert url.endswith(f"/{SOURCE_MAX_DAILY_ROWS}")


def test_klines_range_refuses_a_window_the_source_cannot_reach(workspace: Path):
    """F-048: silently returning the last 30 sessions for a two-year request is bad data."""
    write_universe(workspace)
    provider = fixture_provider(workspace)
    start = tehran(2025, 1, 1, 9, 0).timestamp()
    end = tehran(2026, 10, 21, 12, 0).timestamp()
    with pytest.raises(DataUnavailable, match="refusing a truncated window"):
        provider.klines_range(FOLD_CODE, "1d", start, end)


def test_klines_range_returns_the_window_the_source_covers(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    start = tehran(2026, 9, 12, 0, 0).timestamp()
    end = tehran(2026, 10, 21, 12, 0).timestamp()
    bars = provider.klines_range(FOLD_CODE, "1d", start, end)
    assert len(bars) == 30
    assert bars[0].open_time >= start


def test_server_time_reads_the_constant_that_exists():
    """F-049: this method named MARKETOVERVIEW_PATH (no such constant) and no test called it."""
    provider = TseProvider(FixtureClient())
    assert provider.server_time() == 1_793_000_000.0
    assert any("GetMarketOverview" in url for url in provider.client.date_header_calls)


def test_server_time_fails_closed_without_a_date_header():
    class NoDateClient(FixtureClient):
        def date_header_epoch(self, url: str):
            self.date_header_calls.append(url)
            return None

    provider = TseProvider(NoDateClient())
    with pytest.raises(DataUnavailable, match="Date header"):
        provider.server_time()


# ---------------------------------------------------------------------------
# Spec contract
# ---------------------------------------------------------------------------

def test_tse_spec_declares_single_source_and_no_live_path():
    from cat.markets.tse import SPEC

    assert SPEC.name == "tse"
    assert SPEC.timeframes == ("1d", "1w")
    assert "single_source_tsetmc" in SPEC.quality_warnings
    assert SPEC.cross.__name__ == "no_cross"
    assert SPEC.gap_is_expected is calendar.bar_gap_ok
    provider = TseProvider(FixtureClient())
    assert provider.supports_live is False
    assert not hasattr(provider, "place_order")
    assert not hasattr(provider, "create_order")
