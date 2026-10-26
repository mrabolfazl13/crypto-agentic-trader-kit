"""Freshness semantics: the TSE clock pauses at close, crypto stays 24/7.

The point of these tests is the *difference*: a weekend TSE snapshot must not be
labelled stale, while the same wall-clock rules must still flag an old crypto
feed. A regression in either direction is a safety bug.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cat.config import load_config
from cat.data import Kline, Quote, SymbolFilter, fetch_snapshot, validate_klines
from cat.engine import Engine
from cat.markets import calendar
from tse_helpers import FOLD_CODE, FOLD_TICKER, fixture_provider, write_tse_config, write_universe

UTC = timezone.utc
DAY = 86_400


def _daily_bars(count: int, *, last_open: int, closed: bool = True) -> list[Kline]:
    return [
        Kline(
            open_time=last_open - (count - 1 - index) * DAY,
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            base_volume=1000.0,
            quote_volume=100_000.0,
            closed=closed,
        )
        for index in range(count)
    ]


class OldCryptoFeed:
    """A 24/7 provider whose data stopped three days ago."""

    def __init__(self, last_open: int):
        self.last_open = last_open
        self.last_close = last_open + DAY

    def klines(self, symbol: str, timeframe: str, limit: int = 300) -> list[Kline]:
        return _daily_bars(40, last_open=self.last_open)

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, bid=100.0, ask=100.1, ts=self.last_close, source="test")

    def filters(self, symbol: str) -> SymbolFilter:
        return SymbolFilter(
            symbol=symbol, price_precision=2, qty_precision=6, min_qty=0.0001,
            min_quote=1.0, max_quote=1_000_000.0, tradable=True, fee_pct=0.1,
        )


def test_weekend_tse_snapshot_is_not_stale(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    friday_night = datetime(2026, 10, 23, 20, 0, tzinfo=UTC).timestamp()
    reference = calendar.staleness_reference(friday_night)

    snapshot = fetch_snapshot(
        provider, None, FOLD_CODE, "1d",
        higher_timeframe="1w", limit=300,
        staleness_seconds=345_600,
        staleness_reference_ts=reference,
        gap_ok=calendar.bar_gap_ok,
    )

    assert snapshot.quality.is_fresh is True
    assert "stale_klines" not in snapshot.quality.warnings
    assert "stale_quote" not in snapshot.quality.warnings
    assert "time_gap" not in snapshot.quality.warnings
    # The quote time is the recorded session, not the moment the test ran.
    assert snapshot.quote.ts == pytest.approx(datetime(2026, 10, 21, 12, 30, 2, tzinfo=calendar.TEHRAN).timestamp())


def test_engine_snapshot_marks_single_source_but_stays_fresh(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    write_universe(workspace)
    write_tse_config(workspace)
    config = load_config(root=workspace)
    engine = Engine(config)
    engine.gate = fixture_provider(workspace)
    engine.cross = None
    # Fixture klines are pinned to the 2026-10-21 session; anchor freshness to that
    # same fixed week, otherwise the real wall clock legitimately labels them stale
    # once a later TSE session completes.
    anchor = datetime(2026, 10, 23, 20, 0, tzinfo=UTC).timestamp()
    monkeypatch.setattr(
        Engine, "freshness_reference", lambda self: calendar.staleness_reference(anchor)
    )

    snapshot = engine.snapshot(FOLD_TICKER)

    assert snapshot.symbol == FOLD_CODE
    assert snapshot.quality.is_fresh is True
    assert "single_source_tsetmc" in snapshot.quality.warnings
    assert "stale_klines" not in snapshot.quality.warnings
    assert "stale_quote" not in snapshot.quality.warnings
    assert "time_gap" not in snapshot.quality.warnings


def test_crypto_default_still_uses_the_wall_clock():
    last_open = int(datetime(2026, 10, 20, 0, 0, tzinfo=UTC).timestamp())
    feed = OldCryptoFeed(last_open)

    stale = fetch_snapshot(
        feed, None, "BTC_USDT", "1d",
        higher_timeframe=None, limit=300, staleness_seconds=120,
    )
    assert stale.quality.is_fresh is False
    assert "stale_klines" in stale.quality.warnings
    assert "stale_quote" in stale.quality.warnings

    anchored = fetch_snapshot(
        feed, None, "BTC_USDT", "1d",
        higher_timeframe=None, limit=300, staleness_seconds=120,
        staleness_reference_ts=feed.last_close,
    )
    assert anchored.quality.is_fresh is True
    assert "stale_klines" not in anchored.quality.warnings


def test_tse_weekend_is_not_a_data_gap_but_a_missing_session_is(workspace: Path):
    write_universe(workspace)
    provider = fixture_provider(workspace)
    bars = provider.klines(FOLD_CODE, "1d", 30)

    _, strict = validate_klines(bars, "1d", bars[-1].open_time + DAY, 345_600)
    assert "time_gap" in strict  # the 24/7 rule sees Thursday/Friday as missing

    _, session_aware = validate_klines(
        bars, "1d", bars[-1].open_time + DAY, 345_600, gap_ok=calendar.bar_gap_ok,
    )
    assert "time_gap" not in session_aware

    # A genuinely missing Wednesday is still a gap and must be reported.
    broken = [bar for index, bar in enumerate(bars) if index != len(bars) - 2]
    _, broken_warnings = validate_klines(
        broken, "1d", broken[-1].open_time + DAY, 345_600, gap_ok=calendar.bar_gap_ok,
    )
    assert "time_gap" in broken_warnings
