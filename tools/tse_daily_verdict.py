#!/usr/bin/env python3
"""D-045 — the first measured ``net_per_day`` for the Iranian Bourse daily contract.

Pre-registered in ``DECISIONS.md`` (D-045) before any test or line of code was written. The grid below
is that grid, unchanged: two families x three slippages x three horizons x three windows, plus the
equal-weight buy & hold the comparison needs. Every cell is published, including the negatives.

What is shared with the crypto chain, and what is not:

* indicators, entry/exit gates, the portfolio pass and the benchmark come from ``cat.quant.features``
  and ``cat.quant.sim`` — the same tested code, so a Bourse number and a crypto number mean the same
  thing when they are written next to each other;
* the fill rule is **not** shared, because it cannot be: ``generate_trades`` buys at the open of the
  1m bar after a signal and TSETMC publishes no sub-daily bar. ``daily_candidates`` below is the
  session-granular counterpart — decision at a session close, fill at the *next* session's open —
  with the same pessimistic ordering (stop before target inside a bar) and the same clamps.

No order path exists for this venue and none is added. Nothing here reads or writes ``config.yaml``,
the crypto lake, or any deployed gate; the market-regime gate is off by contract (a BTCUSDT proxy is
not a Tehran regime), which is a difference between venues, not a loosening of a crypto rule.

Usage:
    python tools/tse_daily_verdict.py --max-symbols 30       # pilot
    python tools/tse_daily_verdict.py                          # the whole D-040 lake
    python tools/tse_daily_verdict.py --selftest               # fabricated cells, no lake read
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cat.config import load_config  # noqa: E402
from cat.markets.tse import LOT_SIZE, SPEC  # noqa: E402
from cat.quant.features import QuantParams, gate_funnel, signal_frame  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.sim import (  # noqa: E402
    SignalTrade,
    SimSettings,
    equal_weight_buy_hold,
    simulate_portfolio,
)
from cat.quant import tse_bars  # noqa: E402

OUT_DIR = ROOT / "state" / "quant" / "results" / "d045_tse_daily"

FAMILIES = ("ema_trend", "trend_pullback")
SLIPPAGES_PCT = (0.0, 0.5, 1.0)
HORIZONS_SESSIONS = (5, 10, 20)
PRIMARY_HORIZON = 20
WINDOWS = ("full", "first_half", "second_half")
#: The verdict is taken at the most pessimistic cost on the grid, so a number cannot be improved by
#: picking the cheap arm — the cheap arms are published as sensitivities, not as the answer.
VERDICT_SLIPPAGE_PCT = max(SLIPPAGES_PCT)
#: Capital is a Rial figure so a TSE position is not sized as if one unit were a dollar. The number
#: itself does not move a per-day percentage; it only sets how many whole lots a position can buy,
#: and that bound is published (``lot_rounding``) instead of being assumed away.
TSE_INITIAL_CAPITAL_RIAL = 50_000_000.0


def cell_key(family: str, window: str, slippage_pct: float, horizon_sessions: int) -> str:
    return f"{family}·{window}·{slippage_pct:g}pct·{horizon_sessions}s"


def _finite(value: Any) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(float(value))


def daily_candidates(frame, *, symbol: str, strategy: str, params: QuantParams, sim,
                     max_hold_sessions: int, entry_after_ts: int | None = None,
                     entry_before_ts: int | None = None) -> tuple[list, dict[str, int]]:
    """Fill at the next session's open; stop before target inside a session; one position per symbol.

    Mirrors ``cat.quant.sim.generate_trades`` row-for-row with the 1m rows replaced by sessions, so the
    two venues' numbers differ only where the data and the cost legs differ. A signal on the last
    session of a symbol is dropped rather than filled at the price that produced it.
    """
    signals = frame
    rejections: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejections[reason] = rejections.get(reason, 0) + 1

    if len(signals) < 2:
        return [], {"too_short": 1}
    bar_ends = signals["bar_end"].to_numpy(dtype="int64")
    entry_flags = signals["entry"].fillna(False).to_numpy(dtype=bool)
    exit_flags = signals["exit"].fillna(False).to_numpy(dtype=bool)
    atr_pct = signals["atr_pct"].to_numpy(dtype="float64")
    highs = signals["high"].to_numpy(dtype="float64")
    lows = signals["low"].to_numpy(dtype="float64")
    opens = signals["open"].to_numpy(dtype="float64")
    closes = signals["close"].to_numpy(dtype="float64")
    exec_ts = signals["exec_ts"].tolist()
    exec_open = signals["exec_open"].to_numpy(dtype="float64")
    slip = sim.slippage_pct / 100.0
    cost_gate = sim.min_edge_cost_ratio > 0.0
    required_target_pct = sim.min_edge_cost_ratio * sim.round_trip_cost_pct() / 100.0

    next_exit = np.full(len(signals), -1, dtype=np.int64)
    last_seen = -1
    for index in range(len(signals) - 1, -1, -1):
        if exit_flags[index]:
            last_seen = index
        next_exit[index] = last_seen

    trades: list[Any] = []
    busy_until = -1
    for index in range(len(signals)):
        if not entry_flags[index]:
            continue
        if not _finite(atr_pct[index]) or float(atr_pct[index]) <= 0.0:
            reject("invalid_atr")
            continue
        if not _finite(exec_ts[index]):
            reject("no_next_session")
            continue
        fill_ts = int(exec_ts[index])
        if entry_after_ts is not None and fill_ts < int(entry_after_ts):
            reject("before_window")
            continue
        if entry_before_ts is not None and fill_ts >= int(entry_before_ts):
            reject("after_window")
            continue
        if index <= busy_until:
            reject("overlapping_position")
            continue
        row = index + 1
        entry = float(exec_open[index]) * (1.0 + slip)
        stop_pct = min(max(params.atr_stop_mult * float(atr_pct[index]), 0.002), 0.10)
        target_pct = min(max(params.atr_target_mult * float(atr_pct[index]), 0.004), 0.25)
        if cost_gate and target_pct < required_target_pct:
            reject("target_inside_cost_band")
            continue
        stop = entry * (1.0 - stop_pct)
        target = entry * (1.0 + target_pct)

        exit_signal = int(next_exit[index + 1]) if index + 1 < len(signals) else -1
        signal_row: int | None = None
        if exit_signal >= 0:
            candidate = exit_signal + 1
            if candidate < len(signals) and _finite(exec_ts[candidate - 1]):
                signal_row = candidate
            else:
                reject("missing_exit_bar")

        last_row = min(row + max_hold_sessions - 1, len(signals) - 1)
        exit_row, exit_price, reason = last_row, float(closes[last_row]) * (1.0 - slip), (
            "end_of_data" if last_row == len(signals) - 1 else "time")
        lowest, highest = entry, entry
        for offset in range(row, last_row + 1):
            low, high, open_ = float(lows[offset]), float(highs[offset]), float(opens[offset])
            lowest = min(lowest, low)
            highest = max(highest, high)
            if low <= stop:
                exit_row, exit_price, reason = offset, min(open_, stop) * (1.0 - slip), "stop"
                break
            if high >= target:
                exit_row, exit_price, reason = offset, max(open_, target) * (1.0 - slip), "target"
                break
            if signal_row is not None and offset >= signal_row:
                exit_row, exit_price, reason = offset, open_ * (1.0 - slip), "signal"
                break

        trades.append(SignalTrade(
            symbol=symbol, signal_ts=int(bar_ends[index]), entry_ts=fill_ts,
            exit_ts=int(bar_ends[exit_row]), entry=entry, exit=exit_price, stop=stop, target=target,
            reason=reason, bars=exit_row - row + 1,
            mae_pct=max(0.0, 1.0 - lowest / entry), mfe_pct=max(0.0, highest / entry - 1.0)))
        busy_until = exit_row

    if not trades and not rejections:
        rejections["no_entry_signal"] = int(len(signals))
    return trades, rejections


def bourse_settings(backtest, *, slippage_pct: float) -> Any:
    """A SimSettings for this venue: its own fee and capital, everything else read from the shipped config.

    ``fee_pct`` is the TSE figure published on the venue spec, not the crypto taker rate; ``regime_symbol``
    is empty because the deployed gate's proxy (BTCUSDT above its own EMA) carries no information about
    the Tehran board. ``max_hold_hours`` is unused here — the hold cap is in sessions.
    """
    return SimSettings(
        initial_capital=TSE_INITIAL_CAPITAL_RIAL,
        fee_pct=float(SPEC.default_taker_pct or backtest.fee_pct),
        slippage_pct=float(slippage_pct),
        position_pct=float(backtest.position_pct),
        max_open_positions=int(backtest.max_open_positions),
        max_hold_hours=float(backtest.max_hold_hours),
        min_notional=0.0,
        min_edge_cost_ratio=float(backtest.min_edge_cost_ratio),
        risk_per_trade_pct=float(backtest.risk_per_trade_pct),
        regime_symbol="",
        regime_ema=int(backtest.regime_ema),
    )


def window_bounds(lake: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """The registered windows, in session-close epoch seconds, taken from the lake's own span.

    The mid-point is one global instant rather than each symbol's own middle: two windows that mean
    different date ranges per symbol could not be compared, and a per-symbol split is how a
    backtest quietly starts fitting the calendar instead of the strategy.
    """
    closes = [tse_bars.session_close_ts(value["first_date"]) for value in lake["census"].values()]
    ends = [tse_bars.session_close_ts(value["last_date"]) for value in lake["census"].values()]
    start, end = min(closes), max(ends)
    mid = (start + end) // 2
    return {"full": (start, end), "first_half": (start, mid), "second_half": (mid, end)}


def window_days(bounds: tuple[int, int]) -> float:
    return (bounds[1] - bounds[0]) / 86400.0


def window_bar_census(bars_by_code: dict[str, list[dict[str, Any]]],
                      bounds: dict[str, tuple[int, int]], *, warmup_bars: int) -> dict[str, Any]:
    """How many sessions each registered window actually holds — because thinness has to be a number.

    A half whose per-symbol bar count sits below the indicator warm-up cannot produce a signal no matter
    what the strategy thinks, so ``ready_bars`` on its own does not let a reader tell evidence from
    arithmetic. ``bars_inside`` counts ``start <= bar_end < end`` — the same slice the cells walk.
    """
    cache: dict[str, int] = {}
    out: dict[str, Any] = {}
    for name, (start, end) in bounds.items():
        counts = []
        for rows in bars_by_code.values():
            inside = 0
            for row in rows:
                day = str(row["date"])
                stamp = cache.get(day)
                if stamp is None:
                    stamp = tse_bars.session_close_ts(day)
                    cache[day] = stamp
                if start <= stamp < end:
                    inside += 1
            if inside:
                counts.append(inside)
        out[name] = {
            "bars_inside": int(sum(counts)),
            "symbols_with_bars": len(counts),
            "median_bars_per_symbol": (int(np.median(counts)) if counts else None),
            "warmup_bars": int(warmup_bars),
            "symbols_past_warmup": int(sum(1 for value in counts if value > warmup_bars)),
        }
    return out


def run_cell(marked_by_code: dict[str, dict[str, Any]], bars_by_code: dict[str, list[dict[str, Any]]],
             *, family: str, window: str, bounds: tuple[int, int], slippage_pct: float,
             horizon: int, backtest) -> dict[str, Any]:
    """One cell: the pre-computed signal frames, trimmed to the window, walked under this cost and horizon.

    Indicators are never recomputed per cell — they are backward-looking, so trimming the tail of the
    full series is the same frame the symbol would have produced on its own prefix, and the whole grid
    costs one indicator pass per (symbol, family) instead of 54.
    """
    sim = bourse_settings(backtest, slippage_pct=slippage_pct)
    start_ts, end_ts = bounds
    params = QuantParams()
    candidates: list[Any] = []
    rejections: dict[str, int] = {}
    marks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    closes: dict[str, tuple[float, float]] = {}
    ready_bars = 0
    symbols_skipped = 0
    for symbol, marked in marked_by_code[family].items():
        bar_ends = marked["bar_end"].to_numpy(dtype="int64")
        cut = int(np.searchsorted(bar_ends, np.int64(end_ts), side="left"))
        if cut < 2:
            symbols_skipped += 1
            continue
        lo = int(np.searchsorted(bar_ends, np.int64(start_ts), side="left"))
        inside = marked.iloc[lo:cut]
        ready_bars += int(gate_funnel(inside, family, params)["funnel"][0]["passing"])
        trades, rejected = daily_candidates(marked.iloc[:cut], symbol=symbol, strategy=family,
                                            params=params, sim=sim, max_hold_sessions=horizon,
                                            entry_after_ts=start_ts, entry_before_ts=end_ts)
        candidates.extend(trades)
        for reason, count in rejected.items():
            rejections[reason] = rejections.get(reason, 0) + int(count)
        marks[symbol] = (marked["bar_end"].to_numpy(dtype="int64"),
                         marked["close"].to_numpy(dtype="float64"))
        series = inside["close"].to_numpy(dtype="float64")
        if len(series) >= 2:
            closes[symbol] = (float(series[0]), float(series[-1]))

    book = simulate_portfolio(candidates, settings=sim, rejections=rejections, marks=marks)
    metrics = book["metrics"]
    days = window_days(bounds)
    net = float(metrics["net_return_pct"])
    bench = equal_weight_buy_hold(closes, fee_pct=sim.fee_pct, slippage_pct=sim.slippage_pct)
    bench_return = bench.get("return_pct")
    qty_values = [float(trade["qty"]) for trade in book["trades"]]
    return {
        "family": family, "window": window, "slippage_pct": slippage_pct, "horizon_sessions": horizon,
        "path": "tse_daily_sim",
        "fee_pct_per_side": sim.fee_pct,
        "symbols": len(bars_by_code), "symbols_with_window_bars": len(closes),
        "symbols_skipped": symbols_skipped, "ready_bars": int(ready_bars),
        "closed_trades": int(metrics["trades"]),
        "net_return_pct": round(net, 6),
        "net_per_day_pct": (round(net / days, 6) if days > 0 else None),
        "window_calendar_days": round(days, 4),
        "win_rate_pct": metrics["win_rate_pct"], "profit_factor": metrics["profit_factor"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "effective_drawdown_pct": metrics["effective_drawdown_pct"],
        "fees": metrics["fees"], "rejections": metrics["rejections"],
        "benchmark": {"symbols": bench["symbols"], "return_pct": bench_return,
                      "net_per_day_pct": (round(float(bench_return) / days, 6)
                                          if bench_return is not None and days > 0 else None)},
        # The bound on the optimism D-045 knowingly carries: positions are not floored to whole lots,
        # so a trade buying 1.4 lots is really buying 1 lot. One lot over the quantity is the error.
        "lot_rounding": {"lot_shares": LOT_SIZE,
                         "median_one_lot_over_position_pct": (
                             round(float(np.median([LOT_SIZE / value for value in qty_values])) * 100.0, 3)
                             if qty_values else None),
                         "positions_below_one_lot": int(sum(1 for value in qty_values if value < LOT_SIZE))},
        "determinate": ready_bars >= 1,
    }


def horizon_extremes(cells: dict[str, dict[str, Any]], *, family: str) -> dict[str, Any]:
    """Which horizons sit at the top of the grid — as sets, because ``max()`` on a tied column lies.

    A single ``best_horizon`` from a column where every horizon produced the same number is the F-053
    defect in a new costume: it reads like a measured ranking and is only the first row.
    """
    values = {horizon: (cells[cell_key(family, "full", VERDICT_SLIPPAGE_PCT, horizon)]["net_per_day_pct"])
              for horizon in HORIZONS_SESSIONS}
    ordered = [value for value in values.values() if value is not None]
    if not ordered:
        return {"horizons_measured": [], "grid_orders_the_horizons": False,
                "distinct_horizon_values": 0,
                "horizon_note_extra": "no horizon on the grid produced a measurable per-day number"}
    hi, lo = max(ordered), min(ordered)
    return {
        "horizons_measured": {str(horizon): values[horizon] for horizon in HORIZONS_SESSIONS},
        "best_horizons_by_full_window_net_per_day": [h for h in HORIZONS_SESSIONS
                                                     if values[h] == hi],
        "worst_horizons_by_full_window_net_per_day": [h for h in HORIZONS_SESSIONS if values[h] == lo],
        "distinct_horizon_values": len(set(ordered)),
        "grid_orders_the_horizons": hi != lo,
    }


def cells_digest(cells: dict[str, dict[str, Any]]) -> str:
    """The identity of the measured numbers, so a re-run that moved one is visible without reading 54 cells."""
    measured = {key: [cell.get("closed_trades"), cell.get("net_return_pct"), cell.get("net_per_day_pct"),
                      cell.get("ready_bars"), cell.get("benchmark", {}).get("return_pct")]
                for key, cell in sorted(cells.items())}
    return hashlib.sha256(json.dumps(measured, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def build_verdict(cells: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Apply the pre-registered rule: positive means positive in every determinate window at the worst cost."""
    per_family: dict[str, Any] = {}
    for family in FAMILIES:
        by_window: dict[str, dict[str, Any]] = {}
        for window in WINDOWS:
            for slippage in SLIPPAGES_PCT:
                cell = cells[cell_key(family, window, slippage, PRIMARY_HORIZON)]
                by_window.setdefault(window, {})[f"{slippage:g}"] = {
                    "net_per_day_pct": cell["net_per_day_pct"],
                    "closed_trades": cell["closed_trades"],
                    "determinate": cell["determinate"],
                    "benchmark_net_per_day_pct": cell["benchmark"]["net_per_day_pct"],
                }
        verdict_window = {window: values[f"{VERDICT_SLIPPAGE_PCT:g}"] for window, values in by_window.items()}
        determinate = [window for window, value in verdict_window.items() if value["determinate"]]
        positive = [window for window in determinate
                    if (verdict_window[window]["net_per_day_pct"] or 0.0) > 0.0]
        per_family[family] = {
            "windows_in_verdict": determinate,
            "windows_positive_at_worst_cost": positive,
            "positive_in_every_determinate_window": (
                len(determinate) >= 2 and len(positive) == len(determinate)),
            "verdict_at_worst_slippage": verdict_window,
            "sensitivities": by_window,
            **horizon_extremes(cells, family=family),
            "horizon_note": ("the verdict is taken at the registered horizon of "
                             f"{PRIMARY_HORIZON} sessions; naming a best horizon on this grid is "
                             "selection on the grid, not evidence, and config.yaml is not touched by it"),
        }
    accepted, contrast = FAMILIES[0], FAMILIES[1]
    accepted_clears = per_family[accepted]["positive_in_every_determinate_window"]
    contrast_clears = per_family[contrast]["positive_in_every_determinate_window"]
    bench_rows = [cells[cell_key(family, "full", VERDICT_SLIPPAGE_PCT, PRIMARY_HORIZON)]["benchmark"]
                  for family in FAMILIES]
    bench_positive = any((row["net_per_day_pct"] or 0.0) > 0.0 for row in bench_rows)
    cheap = {family: (cells[cell_key(family, "full", 0.0, PRIMARY_HORIZON)]["net_per_day_pct"],
                      cells[cell_key(family, "full", VERDICT_SLIPPAGE_PCT, PRIMARY_HORIZON)]["net_per_day_pct"])
             for family in FAMILIES}
    flips = [family for family, (free, worst) in cheap.items()
             if free is not None and worst is not None and free > 0.0 >= worst]
    return {
        "per_family": per_family,
        "hypothesis_h1_no_family_clears_the_registered_rule": not (accepted_clears or contrast_clears),
        "hypothesis_h2_buy_hold_is_positive_on_the_same_window": bench_positive,
        "hypothesis_h3_cost_flips_the_sign": bool(flips),
        "cost_flipping_families": flips,
        "bourse_lever_status": ("rejected_on_this_grid" if not (accepted_clears or contrast_clears)
                                else "not_rejected_on_this_grid"),
        "cells_measured": len(cells),
        "tests_run": len(FAMILIES) * len(SLIPPAGES_PCT) * len(HORIZONS_SESSIONS) * len(WINDOWS),
        "multiplicity": (f"{len(FAMILIES)} families x {len(SLIPPAGES_PCT)} slippages x "
                         f"{len(HORIZONS_SESSIONS)} horizons x {len(WINDOWS)} windows = "
                         f"{len(cells)} strategy cells published, plus the buy & hold benchmark"),
        "reading": ("this is one shot at a source-capped 200-session lake, not a walk-forward and not "
                    "an out-of-sample test; it cannot activate anything, and a positive number here "
                    "would still need the deployed path and its own evidence window"),
    }


def selftest_cells() -> dict[str, dict[str, Any]]:
    """Fabricated cells so the verdict rule itself is testable without the lake."""
    cells: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        for window in WINDOWS:
            for slippage in SLIPPAGES_PCT:
                for horizon in HORIZONS_SESSIONS:
                    net = -0.05 if family == FAMILIES[0] else -0.02
                    cells[cell_key(family, window, slippage, horizon)] = {
                        "family": family, "window": window, "slippage_pct": slippage,
                        "horizon_sessions": horizon, "determinate": True,
                        "net_per_day_pct": net, "closed_trades": 7,
                        "benchmark": {"net_per_day_pct": -0.01, "symbols": 3},
                    }
    return cells


def _jsonable(value: Any) -> Any:
    """NaN becomes null, because a JSON file containing ``NaN`` is not JSON and cannot be audited.

    An unavailable measurement has exactly one representation in this repo's artifacts: null.
    """
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (np.ndarray,)):
        return [_jsonable(item) for item in value.tolist()]
    return value


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--out", default=str(OUT_DIR / "tse_daily_verdict.json"))
    parser.add_argument("--selftest", action="store_true", help="fabricated cells; reads no lake")
    parser.add_argument("--force", action="store_true", help="overwrite an existing artifact")
    args = parser.parse_args()

    started = time.monotonic()
    if args.selftest:
        cells = selftest_cells()
        lake_info: dict[str, Any] = {"selftest": True}
    else:
        lake = tse_bars.load_lake(ROOT, max_symbols=args.max_symbols)
        bars_by_code = lake["bars"]
        if not bars_by_code:
            print("no usable symbols; refusing an empty verdict")
            return 2
        backtest = load_quant_settings(load_config(ROOT / "config.yaml")).backtest
        bounds = window_bounds(lake)
        marked_by_code: dict[str, dict[str, Any]] = {}
        for family in FAMILIES:
            frames = {}
            for symbol, rows in bars_by_code.items():
                frames[symbol] = signal_frame(tse_bars.signal_frame(rows, ins_code=symbol),
                                              family, QuantParams())
            marked_by_code[family] = frames
        cells = {}
        for family in FAMILIES:
            for window in WINDOWS:
                for slippage in SLIPPAGES_PCT:
                    for horizon in HORIZONS_SESSIONS:
                        key = cell_key(family, window, slippage, horizon)
                        cells[key] = run_cell(marked_by_code, bars_by_code, family=family,
                                              window=window, bounds=bounds[window],
                                              slippage_pct=slippage, horizon=horizon,
                                              backtest=backtest)
                        print(f"  {key}: {cells[key]['closed_trades']} trades, "
                              f"{cells[key]['net_per_day_pct']}%/day", flush=True)
        lake_info = {key: value for key, value in lake.items() if key not in ("bars", "census")}
        lake_info["windows"] = {name: {"start_epoch": start, "end_epoch": end,
                                       "calendar_days": round(window_days((start, end)), 4)}
                                for name, (start, end) in bounds.items()}
        lake_info["window_bar_census"] = window_bar_census(bars_by_code, bounds,
                                                           warmup_bars=QuantParams().warmup_bars())
        lake_info["census_totals"] = {
            "absent_weekday_sessions": sum(v["absent_weekday_sessions"] for v in lake["census"].values()),
            "multi_session_gaps": sum(v["multi_session_gaps"] for v in lake["census"].values()),
            "big_move_sessions": sum(v["big_move_sessions"] for v in lake["census"].values()),
        }
        lake_info["census_examples"] = {code: lake["census"][code] for code in sorted(lake["census"])[:3]}

    verdict = build_verdict(cells)
    payload = {
        "decision": "D-045", "venue": "tsetmc (Iranian Bourse)", "grid": {
            "families": list(FAMILIES), "slippages_pct": list(SLIPPAGES_PCT),
            "horizons_sessions": list(HORIZONS_SESSIONS), "primary_horizon": PRIMARY_HORIZON,
            "windows": list(WINDOWS), "verdict_slippage_pct": VERDICT_SLIPPAGE_PCT},
        "safety": {"mode": "research backtest", "orders_placed": 0, "live": False,
                   "credentials_used": False, "config_written": False,
                   "crypto_lake_read": False, "crypto_gates_changed": False},
        "limitations": ["depth_capped_by_source_200_sessions", "unadjusted_for_corporate_actions",
                        "single_source_tsetmc", "no_order_book_so_slippage_is_assumed",
                        "positions_not_floored_to_whole_lots", "no_walk_forward_no_holdout"],
        "lake": lake_info, "cells": cells, "cells_digest": cells_digest(cells), "verdict": verdict,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(f"refusing to overwrite {out_path} (use --force; the published artifact is immutable)")
        return 3
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=1, default=float))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
