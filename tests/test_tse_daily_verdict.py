"""Locks for the D-045 Bourse daily verdict: what a session-granular fill may claim, and what it must refuse.

The pre-registered bar is one sentence — a family clears only if its `net_per_day` is positive in every
determinate window at the most pessimistic slippage on the grid — so the tests below pin the machinery
that sentence depends on: no fill on the bar that produced the signal, stop before target inside a
session, the venue's own fee arriving instead of the crypto one, the regime gate being absent by
contract rather than by loosening, and a tied horizon grid reported as a tie instead of as a winner
(F-053 in a new venue). Everything runs offline on fabricated sessions: no TSETMC call, no lake read,
no config write.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import tse_daily_verdict as verdict  # noqa: E402
from cat.markets.tse import SPEC  # noqa: E402
from cat.quant import tse_bars as tb  # noqa: E402
from cat.quant.features import QuantParams, signal_frame  # noqa: E402
from cat.quant.settings import BacktestSettings  # noqa: E402
from cat.quant.sim import SimSettings  # noqa: E402

# The board is closed Thursday and Friday, and a signal needs the shipped 50-bar warm-up before any
# gate can pass, so the fabricated sessions are generated from the venue calendar for as many session
# days as the indicators need — hand-listing 30 dates would test the fixture, not the fill rule.
from cat.markets import calendar as venue_calendar  # noqa: E402


def _session_days(count: int, *, start: str = "2026-01-03") -> list[str]:
    days, cursor = [], date.fromisoformat(start)
    while len(days) < count:
        if venue_calendar.is_session_day(cursor):
            days.append(cursor.isoformat())
        cursor += venue_calendar.timedelta(days=1)
    return days


SESSION_DAYS = _session_days(75)


def _rows(prices: list[float], *, spread: float = 0.02, code: str = "1234") -> list[dict]:
    out = []
    for day, close in zip(SESSION_DAYS, prices):
        out.append({
            "insCode": code, "date": day, "session": int(day.replace("-", "")),
            "open": round(close * (1.0 - spread / 2), 4), "high": round(close * (1.0 + spread), 4),
            "low": round(close * (1.0 - spread), 4), "close": close,
            "volume_shares": 100_000.0, "traded_value_rial": close * 100_000.0, "trade_count": 50.0,
        })
    return out


def _marked(prices: list[float], *, family: str = "ema_trend", code: str = "1234"):
    return signal_frame(tb.signal_frame(_rows(prices, code=code), ins_code=code), family, QuantParams())


def _rising(n: int = len(SESSION_DAYS), *, start: float = 1000.0, step: float = 0.02) -> list[float]:
    return [round(start * (1.0 + step) ** index, 4) for index in range(n)]


def _sim(**overrides) -> SimSettings:
    values = {"initial_capital": 50_000_000.0, "fee_pct": 0.325, "slippage_pct": 0.0,
              "position_pct": 5.0, "max_open_positions": 10, "min_notional": 0.0,
              "min_edge_cost_ratio": 0.0, "risk_per_trade_pct": 0.0, "regime_symbol": ""}
    values.update(overrides)
    return SimSettings(**values)


def test_the_key_names_the_family_window_cost_and_horizon():
    assert verdict.cell_key("ema_trend", "full", 1.0, 20) == "ema_trend·full·1pct·20s"


def test_a_signal_never_fills_on_the_session_that_produced_it():
    marked = _marked(_rising())
    trades, rejections = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend",
                                                 params=QuantParams(), sim=_sim(), max_hold_sessions=20)
    bar_ends = marked["bar_end"].to_numpy(dtype="int64")
    assert trades, "a 2%/day rising board must produce an entry"
    for trade in trades:
        assert trade.entry_ts > trade.signal_ts
        assert trade.entry_ts == int(marked.loc[bar_ends == trade.signal_ts, "exec_ts"].iloc[0])
        assert trade.exit_ts >= trade.entry_ts
    assert "no_next_session" in rejections or trades, "the final session cannot be filled at all"


def test_the_fill_price_is_the_next_sessions_open_with_slippage_on_top():
    rows = _rows(_rising())
    marked = _marked(_rising())
    index = int(np.flatnonzero(marked["entry"].fillna(False).to_numpy(dtype=bool))[0])
    trades, _ = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend",
                                         params=QuantParams(), sim=_sim(slippage_pct=1.0),
                                         max_hold_sessions=20)
    expected_open = float(rows[index + 1]["open"])
    assert trades[0].entry == pytest.approx(expected_open * 1.01)
    assert trades[0].entry != pytest.approx(float(rows[index]["open"]) * 1.01), \
        "filling at the signal session's own open would be look-ahead"


def test_stop_is_booked_before_target_when_one_session_touches_both():
    base = _marked(_rising())
    index = int(np.flatnonzero(base["entry"].fillna(False).to_numpy(dtype=bool))[0])
    rows = _rows(_rising())
    # Widening a session AFTER the signal changes that session's range only: the close is untouched, so
    # the indicators — and therefore the stop and target the entry was given — stay exactly as they were.
    rows[index + 1]["low"] = round(rows[index + 1]["close"] * 0.5, 4)
    rows[index + 1]["high"] = round(rows[index + 1]["close"] * 1.5, 4)
    marked = signal_frame(tb.signal_frame(rows, ins_code="1234"), "ema_trend", QuantParams())
    assert bool(marked["entry"].fillna(False).to_numpy(dtype=bool)[index]), \
        "the widened session must not move the signal being tested"
    trades, _ = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend", params=QuantParams(),
                                         sim=_sim(), max_hold_sessions=20)
    assert trades, "the widened session is the fill bar, so the position has to be booked on it"
    assert trades[0].reason == "stop", "a session that touched both levels is a stop, exactly as sim.py books it"
    assert trades[0].exit_ts == int(marked["bar_end"].iloc[index + 1])


def test_one_position_per_symbol_at_a_time():
    marked = _marked(_rising(step=0.03))
    trades, rejections = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend",
                                                 params=QuantParams(), sim=_sim(), max_hold_sessions=20)
    ordered = sorted(trades, key=lambda trade: trade.entry_ts)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.entry_ts > earlier.exit_ts, "entries may not overlap the open position"
    assert rejections.get("overlapping_position", 0) >= 0


def test_a_window_edge_rejects_the_entry_it_cannot_fill():
    marked = _marked(_rising())
    last_signal = int(np.flatnonzero(marked["entry"].fillna(False).to_numpy(dtype=bool))[-1])
    start = int(marked["bar_end"].iloc[last_signal]) + 1
    trades, rejections = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend",
                                                 params=QuantParams(), sim=_sim(),
                                                 max_hold_sessions=20, entry_after_ts=start)
    assert not trades
    assert sum(rejections.values()) > 0


def test_the_cost_gate_is_measured_in_the_venues_round_trip_not_the_cryptos():
    marked = _marked(_rising(step=0.004))
    ungated = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend", params=QuantParams(),
                                       sim=_sim(min_edge_cost_ratio=0.0), max_hold_sessions=20)[0]
    assert ungated, "the same board must be tradable when no cost floor is applied"

    # A floor so wide that no 0.4%-a-day target can clear it proves the gate is live in this venue;
    # the required edge is the venue's own round trip (0.325%/side fee + slippage), not the crypto one.
    strict, rejections = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend",
                                                  params=QuantParams(),
                                                  sim=_sim(fee_pct=0.325, slippage_pct=1.0,
                                                          min_edge_cost_ratio=500.0),
                                                  max_hold_sessions=20)
    assert not strict and rejections.get("target_inside_cost_band", 0) > 0, \
        "an unreachable cost floor must reject every candidate rather than trade through it"

    shipped, _ = verdict.daily_candidates(marked, symbol="1234", strategy="ema_trend", params=QuantParams(),
                                          sim=_sim(fee_pct=0.325, slippage_pct=1.0,
                                                  min_edge_cost_ratio=2.0), max_hold_sessions=20)
    required = 2.0 * _sim(fee_pct=0.325, slippage_pct=1.0).round_trip_cost_pct() / 100.0
    for trade in shipped:
        assert trade.target / trade.entry - 1.0 >= min(required, 0.25) - 1e-9, \
            "a booked target below the venue round trip is a guaranteed loss"


def test_bourse_settings_take_the_published_tse_fee_and_switch_the_crypto_only_gates_off():
    backtest = BacktestSettings(fee_pct=0.10, slippage_pct=0.05, position_pct=5.0,
                                max_open_positions=10, max_hold_hours=24.0, min_edge_cost_ratio=2.0,
                                risk_per_trade_pct=1.0, regime_symbol="BTCUSDT", regime_ema=200)
    sim = verdict.bourse_settings(backtest, slippage_pct=0.5)
    assert sim.fee_pct == float(SPEC.default_taker_pct), "the crypto taker rate is not the TSE fee"
    assert sim.slippage_pct == 0.5
    assert sim.position_pct == backtest.position_pct and sim.max_open_positions == 10
    assert sim.min_edge_cost_ratio == 2.0 and sim.risk_per_trade_pct == 1.0
    assert sim.regime_symbol == "", "a BTCUSDT EMA is not a Tehran regime; the gate is absent by contract"
    assert sim.initial_capital == verdict.TSE_INITIAL_CAPITAL_RIAL


def test_the_windows_tile_the_lake_and_the_halves_share_one_boundary():
    lake = {"census": {code: {"first_date": SESSION_DAYS[0], "last_date": SESSION_DAYS[-1]}
                       for code in ("1234", "5678")}}
    bounds = verdict.window_bounds(lake)
    start, mid, end = bounds["full"][0], bounds["first_half"][1], bounds["second_half"][1]
    assert bounds["first_half"][0] == bounds["second_half"][0] == mid or bounds["second_half"][0] == mid
    assert bounds["full"][1] == bounds["second_half"][1] == end
    assert verdict.window_days(bounds["full"]) == pytest.approx(
        verdict.window_days(bounds["first_half"]) + verdict.window_days(bounds["second_half"]), abs=0.01)
    assert start < mid < end


def test_a_window_below_the_warmup_is_counted_as_bars_not_as_evidence():
    prices = _rising()
    bars = {"1234": _rows(prices)}
    bounds = {"all": (tb.session_close_ts(SESSION_DAYS[0]), tb.session_close_ts(SESSION_DAYS[-1])),
              "short": (tb.session_close_ts(SESSION_DAYS[0]), tb.session_close_ts(SESSION_DAYS[10]))}
    census = verdict.window_bar_census(bars, bounds, warmup_bars=QuantParams().warmup_bars())
    assert census["all"]["bars_inside"] == len(SESSION_DAYS) - 1, "the boundary bar is not inside"
    assert census["all"]["median_bars_per_symbol"] == len(SESSION_DAYS) - 1
    assert census["all"]["symbols_past_warmup"] == 1
    assert census["short"]["bars_inside"] <= 10 and census["short"]["symbols_past_warmup"] == 0, \
        "eleven sessions cannot warm a 48-bar EMA, so a thin window is arithmetic, not evidence"


def test_a_tied_horizon_grid_is_reported_as_a_tie_not_as_a_winner():
    cells = {}
    for family in verdict.FAMILIES:
        for window in verdict.WINDOWS:
            for slippage in verdict.SLIPPAGES_PCT:
                for horizon in verdict.HORIZONS_SESSIONS:
                    cells[verdict.cell_key(family, window, slippage, horizon)] = {
                        "net_per_day_pct": -0.03, "closed_trades": 4, "determinate": True,
                        "benchmark": {"net_per_day_pct": -0.01, "return_pct": -1.0, "symbols": 2}}
    rows = verdict.build_verdict(cells)["per_family"][verdict.FAMILIES[0]]
    assert "best_horizon_on_this_grid" not in rows, "a single best ratio on a tied column is the F-053 defect"
    assert rows["best_horizons_by_full_window_net_per_day"] == list(verdict.HORIZONS_SESSIONS)
    assert rows["grid_orders_the_horizons"] is False
    assert rows["distinct_horizon_values"] == 1


def test_a_horizon_grid_that_orders_the_arms_names_one_best_and_one_worst():
    cells = {}
    for family in verdict.FAMILIES:
        for window in verdict.WINDOWS:
            for slippage in verdict.SLIPPAGES_PCT:
                for horizon in verdict.HORIZONS_SESSIONS:
                    net = {5: -0.2, 10: -0.1, 20: -0.05}[horizon]
                    cells[verdict.cell_key(family, window, slippage, horizon)] = {
                        "net_per_day_pct": net, "closed_trades": 4, "determinate": True,
                        "benchmark": {"net_per_day_pct": -0.01, "return_pct": -1.0, "symbols": 2}}
    rows = verdict.build_verdict(cells)["per_family"][verdict.FAMILIES[0]]
    assert rows["grid_orders_the_horizons"] is True
    assert rows["best_horizons_by_full_window_net_per_day"] == [20]
    assert rows["worst_horizons_by_full_window_net_per_day"] == [5]


def _cells_with(overrides: dict[tuple[str, str, float], dict] | None = None) -> dict:
    cells = {}
    for family in verdict.FAMILIES:
        for window in verdict.WINDOWS:
            for slippage in verdict.SLIPPAGES_PCT:
                for horizon in verdict.HORIZONS_SESSIONS:
                    cell = {"net_per_day_pct": -0.05, "closed_trades": 9, "determinate": True,
                            "benchmark": {"net_per_day_pct": -0.01, "return_pct": -1.0, "symbols": 2}}
                    cell.update((overrides or {}).get((family, window, slippage), {}))
                    cells[verdict.cell_key(family, window, slippage, horizon)] = cell
    return cells


def _all_windows(family: str, slippage: float, values: dict) -> dict:
    for window in verdict.WINDOWS:
        values[(family, window, slippage)] = {"net_per_day_pct": 0.02}
    return values


def test_positive_needs_every_window_at_the_worst_cost_not_the_cheapest_one():
    cells = _cells_with(_all_windows(verdict.FAMILIES[0], verdict.VERDICT_SLIPPAGE_PCT,
                                     _all_windows(verdict.FAMILIES[1], verdict.VERDICT_SLIPPAGE_PCT, {})))
    rows = verdict.build_verdict(cells)
    assert rows["per_family"][verdict.FAMILIES[0]]["positive_in_every_determinate_window"] is True
    assert rows["bourse_lever_status"] == "not_rejected_on_this_grid"
    assert rows["hypothesis_h1_no_family_clears_the_registered_rule"] is False


def test_one_non_positive_window_at_the_verdict_cost_is_enough_to_keep_the_rejection():
    other = verdict.WINDOWS[1]
    cells = _cells_with(_all_windows(verdict.FAMILIES[0], verdict.VERDICT_SLIPPAGE_PCT,
                                     _all_windows(verdict.FAMILIES[1], verdict.VERDICT_SLIPPAGE_PCT, {})))
    cells[verdict.cell_key(verdict.FAMILIES[0], other, verdict.VERDICT_SLIPPAGE_PCT,
                           verdict.PRIMARY_HORIZON)]["net_per_day_pct"] = -0.01
    rows = verdict.build_verdict(cells)
    assert rows["per_family"][verdict.FAMILIES[0]]["positive_in_every_determinate_window"] is False
    assert rows["per_family"][verdict.FAMILIES[0]]["windows_positive_at_worst_cost"] == \
           [window for window in verdict.WINDOWS if window != other]


def test_zero_is_not_positive_and_an_indeterminate_window_is_excluded_not_counted():
    worst = verdict.VERDICT_SLIPPAGE_PCT
    cells = _cells_with({(family, window, worst): {"net_per_day_pct": 0.0}
                         for family in verdict.FAMILIES for window in verdict.WINDOWS})
    assert verdict.build_verdict(cells)["per_family"][verdict.FAMILIES[0]][
        "positive_in_every_determinate_window"] is False

    cells2 = _cells_with(_all_windows(verdict.FAMILIES[0], worst,
                                      _all_windows(verdict.FAMILIES[1], worst, {})))
    thin = verdict.WINDOWS[0]
    for slippage in verdict.SLIPPAGES_PCT:
        for horizon in verdict.HORIZONS_SESSIONS:
            cell = cells2[verdict.cell_key(verdict.FAMILIES[0], thin, slippage, horizon)]
            cell["determinate"] = False
            cell["net_per_day_pct"] = None
    rows2 = verdict.build_verdict(cells2)
    assert rows2["per_family"][verdict.FAMILIES[0]]["windows_in_verdict"] == list(verdict.WINDOWS[1:])
    assert rows2["per_family"][verdict.FAMILIES[0]]["positive_in_every_determinate_window"] is True


def test_fewer_than_two_determinate_windows_cannot_clear_the_lever():
    worst = verdict.VERDICT_SLIPPAGE_PCT
    cells = _cells_with(_all_windows(verdict.FAMILIES[0], worst,
                                     _all_windows(verdict.FAMILIES[1], worst, {})))
    for window in verdict.WINDOWS[1:]:
        for slippage in verdict.SLIPPAGES_PCT:
            for horizon in verdict.HORIZONS_SESSIONS:
                cells[verdict.cell_key(verdict.FAMILIES[0], window, slippage, horizon)]["determinate"] = False
    rows = verdict.build_verdict(cells)
    assert rows["per_family"][verdict.FAMILIES[0]]["positive_in_every_determinate_window"] is False, \
        "one window is an anecdote; the locked rule needs at least two"


def test_the_cost_sensitivity_that_flips_the_sign_is_published_as_h3():
    cells = _cells_with({(verdict.FAMILIES[0], "full", 0.0): {"net_per_day_pct": 0.04},
                         (verdict.FAMILIES[0], "full", verdict.VERDICT_SLIPPAGE_PCT):
                             {"net_per_day_pct": -0.02}})
    rows = verdict.build_verdict(cells)
    assert rows["hypothesis_h3_cost_flips_the_sign"] is True
    assert rows["cost_flipping_families"] == [verdict.FAMILIES[0]]


def test_the_digest_moves_when_a_measured_number_moves_and_not_when_a_label_does():
    cells = _cells_with()
    before = verdict.cells_digest(cells)
    relabelled = {key: dict(value, note="regenerated") for key, value in cells.items()}
    assert verdict.cells_digest(relabelled) == before
    moved = dict(cells)
    first_key = sorted(moved)[0]
    moved[first_key] = dict(moved[first_key], net_per_day_pct=-0.04)
    assert verdict.cells_digest(moved) != before


def test_nan_becomes_null_because_a_json_file_with_nan_in_it_is_not_a_record():
    payload = verdict._jsonable({"a": float("nan"), "b": np.float64(1.5), "c": np.int64(3),
                                 "d": [float("inf")], "e": {"f": np.nan}})
    assert payload["a"] is None and payload["d"][0] is None and payload["e"]["f"] is None
    assert payload["b"] == 1.5 and isinstance(payload["b"], float)
    assert payload["c"] == 3 and isinstance(payload["c"], int)
    import json
    assert "NaN" not in json.dumps(payload)


def test_run_cell_on_two_fabricated_symbols_produces_a_book_and_labels_its_path(tmp_path: Path):
    prices = _rising(step=0.02)
    bars = {code: _rows(prices, code=code) for code in ("1234", "5678")}
    marked = {family: {code: _marked(prices, family=family, code=code)
                       for code in bars} for family in verdict.FAMILIES}
    bounds = {"full": (tb.session_close_ts(SESSION_DAYS[0]), tb.session_close_ts(SESSION_DAYS[-1]))}
    cell = verdict.run_cell(marked, bars, family="ema_trend", window="full",
                           bounds=bounds["full"], slippage_pct=1.0, horizon=20,
                           backtest=BacktestSettings(min_edge_cost_ratio=2.0, risk_per_trade_pct=1.0,
                                                     fee_pct=0.10, slippage_pct=0.05))
    assert cell["path"] == "tse_daily_sim"
    assert cell["fee_pct_per_side"] == float(SPEC.default_taker_pct)
    assert cell["symbols"] == 2 and cell["determinate"] is True
    assert cell["ready_bars"] > 0, "the window slice must count the bars it actually evaluated"
    assert cell["window_calendar_days"] == pytest.approx(
        verdict.window_days(bounds["full"]), 0.01)
    assert cell["net_per_day_pct"] == pytest.approx(cell["net_return_pct"] / cell["window_calendar_days"],
                                                    abs=1e-6), "the D-036 unit, published rounded"
    assert cell["lot_rounding"]["lot_shares"] > 0
    assert math.isfinite(float(cell["benchmark"]["return_pct"] or 0.0))
