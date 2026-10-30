"""Locks for the Bourse daily-bar reader (D-045): what it may trust, and what it must refuse.

Everything here runs offline on fabricated ``state/tse/bars`` files — no network, no TSETMC call, no
crypto lake touched. The locks that matter are the ones a wrong answer would silently cost money over:
the fill timestamp must be a *later session* than the bar that produced it (look-ahead), a lake whose
digest differs from its manifest must stop the run instead of being read as "the same data", and a
single untrustworthy row must remove one symbol rather than corrupt every symbol next to it.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import tse_ingest_daily as ingest  # noqa: E402
from cat.quant import tse_bars as tb  # noqa: E402

# 2026-01-03 is a Saturday, 05 a Monday, 06 a Tuesday, 07 a Wednesday; 08 Thursday and
# 09 Friday are closures. Weekdays are picked from the real board calendar on purpose: a
# reader that only "works" on dates the author imagined is not measuring this venue.
SAT, SUN, MON, TUE, WED, THU, FRI = ("2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06",
                                     "2026-01-07", "2026-01-08", "2026-01-09")
SAT2 = "2026-01-10"


def _row(day: str, code: str = "1234", *, opening: float = 1000.0, high: float = 1050.0,
         low: float = 980.0, close: float = 1020.0, volume: float = 50_000.0) -> dict:
    return {
        "insCode": code, "date": day, "session": int(day.replace("-", "")),
        "open": opening, "high": high, "low": low, "close": close,
        "volume_shares": volume, "traded_value_rial": close * volume, "trade_count": 40.0,
    }


def _lake(root: Path, bars: dict[str, list[dict]]) -> Path:
    bars_dir = root / tb.BARS_DIRNAME
    bars_dir.mkdir(parents=True, exist_ok=True)
    for code, rows in bars.items():
        lines = "\n".join(json.dumps(row) for row in rows)
        (bars_dir / f"{code}.jsonl").write_text(lines + "\n", encoding="utf-8")
    manifest = root / tb.MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "venue": "tsetmc:test", "bars_digest": tb.bars_digest(bars),
        "safety": {"mode": "read-only ingest", "orders_placed": 0, "live": False},
        "source_contract": {"source_row_cap": 200},
    }), encoding="utf-8")
    return root


def test_the_session_clock_is_the_venues_own_and_closes_on_the_weekend():
    open_ts, close_ts = tb.session_open_ts(SAT), tb.session_close_ts(SAT)
    assert close_ts - open_ts == 3 * 3600 + 30 * 60, "09:00-12:30 Tehran"
    assert close_ts > open_ts
    from cat.markets import calendar
    from datetime import date
    assert calendar.is_session_day(date.fromisoformat(SAT)) is True
    for closure in (THU, FRI):
        with pytest.raises(tb.TseBarError, match="no session"):
            tb.session_close_ts(closure)


def test_a_symbol_that_skips_sessions_reports_the_skip_instead_of_hiding_it():
    # The board trades Saturday through Wednesday, so Sunday is a session day and the only weekly
    # closure is Thursday-night/Friday. These dates say so out loud because a test that guesses the
    # calendar is a test of the guess.
    assert tb.multi_session_gaps([_row(SAT), _row(SUN)]) == 0
    assert tb.multi_session_gaps([_row(SAT), _row(MON)]) == 1
    assert tb.multi_session_gaps([_row(WED), _row(SAT2)]) == 0, "Saturday is Wednesday's next session"
    assert tb.absent_weekday_sessions([_row(SAT), _row(MON)]) == 1, "only Sunday is missing"
    assert tb.absent_weekday_sessions([_row(WED), _row(SAT2)]) == 0


def test_a_row_jumping_more_than_the_daily_band_is_counted_not_filtered():
    rows = [_row(SAT), _row(MON, close=600.0)]
    assert tb.big_move_sessions(rows) == 1
    assert tb.big_move_sessions([_row(SAT), _row(MON, close=1030.0)]) == 0


def test_unordered_rows_are_sorted_and_the_reordering_is_reported():
    rows, reordered = tb.validate_bars([_row(MON), _row(SAT), _row(TUE)], ins_code="1234")
    assert [row["date"] for row in rows] == [SAT, MON, TUE]
    assert reordered == 1, "sorting is allowed, silently pretending the file was ordered is not"


def test_a_duplicate_or_unlabelled_session_refuses_the_whole_symbol():
    with pytest.raises(tb.TseBarError, match="duplicate sessions"):
        tb.validate_bars([_row(SAT), _row(SAT)], ins_code="1234")
    stale = _row(SAT)
    stale["session"] = 20260104
    with pytest.raises(tb.TseBarError, match="does not match date"):
        tb.validate_bars([stale], ins_code="1234")
    with pytest.raises(tb.TseBarError, match="no bars"):
        tb.validate_bars([], ins_code="1234")


@pytest.mark.parametrize("field,value,reason", [
    ("close", 0.0, "non-positive"),
    ("high", float("nan"), "non-finite"),
    ("low", -5.0, "non-positive"),
])
def test_an_unusable_price_stops_the_symbol(field: str, value: float, reason: str):
    row = _row(SAT)
    row[field] = value
    with pytest.raises(tb.TseBarError, match=reason):
        tb.validate_bars([row], ins_code="1234")


def test_a_bar_whose_high_is_below_its_own_close_is_not_data():
    with pytest.raises(tb.TseBarError, match="high below open/close"):
        tb.validate_bars([_row(SAT, high=1000.0, close=1020.0)], ins_code="1234")
    with pytest.raises(tb.TseBarError, match="low above open/close"):
        tb.validate_bars([_row(SAT, low=1030.0, opening=1000.0, close=1020.0)], ins_code="1234")


def test_a_signal_bar_is_labelled_at_the_session_close_and_shares_one_column_set_with_the_engine():
    frame = tb.signal_frame([_row(SAT), _row(MON)], ins_code="1234")
    assert list(frame.columns[:8]) == ["bar_end", "open", "high", "low", "close", "volume", "bars", "complete"]
    assert frame["bar_end"].tolist() == [tb.session_close_ts(SAT), tb.session_close_ts(MON)]
    assert frame["complete"].all() and frame["bars"].tolist() == [1, 1]
    assert frame["volume"].tolist() == [50_000.0, 50_000.0]


def test_no_decision_can_be_filled_on_the_bar_that_produced_it():
    frame = tb.signal_frame([_row(SAT), _row(MON), _row(TUE)], ins_code="1234")
    for index in range(len(frame) - 1):
        assert frame.loc[index, "exec_ts"] > frame.loc[index, "bar_end"], "fill strictly after the signal bar"
        assert frame.loc[index, "exec_open"] == frame.loc[index + 1, "open"], "the next session's open, never this one"
    last = len(frame) - 1
    assert frame.loc[last, "exec_ts"] is None
    assert math.isnan(float(frame.loc[last, "exec_open"])), "the final bar has no venue left to trade"


def test_the_digest_matches_the_writer_and_moves_when_the_lake_moves():
    bars = {"1234": [_row(SAT), _row(MON)]}
    assert ingest.bars_digest(bars) == tb.bars_digest(bars)
    assert tb.bars_digest(bars) != tb.bars_digest({"1234": [_row(SAT)]})
    assert tb.bars_digest(bars) != tb.bars_digest({"1235": [_row(SAT), _row(MON)]})


def test_a_lake_that_no_longer_matches_its_manifest_stops_the_run(tmp_path: Path):
    root = _lake(tmp_path, {"1234": [_row(SAT), _row(MON)]})
    extra = root / tb.BARS_DIRNAME / "5678.jsonl"
    extra.write_text(json.dumps(_row(SAT, code="5678")) + "\n", encoding="utf-8")
    with pytest.raises(tb.TseBarError, match="does not match the manifest"):
        tb.load_lake(root)


def test_the_lake_reads_and_max_symbols_narrows_the_universe_not_the_digest(tmp_path: Path):
    root = _lake(tmp_path, {
        "1234": [_row(SAT), _row(MON)],
        "5678": [_row(SAT, code="5678"), _row(MON, code="5678")],
    })
    full = tb.load_lake(root)
    assert full["digest_matches_manifest"] is True
    assert full["symbols_in_lake"] == 2 and full["bars_total"] == 4
    pilot = tb.load_lake(root, max_symbols=1)
    assert pilot["symbols_usable"] == 1 and pilot["bars_selected"] == 2
    assert pilot["digest"] == full["digest"], "a pilot is a smaller universe, not a different lake"
    assert pilot["sessions_per_symbol"] == {"min": 2, "median": 2, "max": 2}


def test_one_broken_file_costs_one_symbol_not_the_run(tmp_path: Path):
    bars = {"1234": [_row(SAT), _row(MON)], "5678": [_row(SAT, code="5678")]}
    root = _lake(tmp_path, bars)
    (root / tb.BARS_DIRNAME / "9999.jsonl").write_text('{"date": "2026-01-03"}\n', encoding="utf-8")
    lake = tb.load_lake(root)
    assert lake["symbols_usable"] == 2
    assert list(lake["errors"]) == ["9999"] and "missing" in lake["errors"]["9999"]


def test_a_lake_without_a_manifest_is_refused(tmp_path: Path):
    (tmp_path / tb.BARS_DIRNAME).mkdir(parents=True, exist_ok=True)
    with pytest.raises(tb.TseBarError, match="unlabelled lake"):
        tb.load_lake(tmp_path)
    with pytest.raises(tb.TseBarError, match="run tools/tse_ingest_daily.py"):
        tb.load_lake(tmp_path / "nothing-here")
