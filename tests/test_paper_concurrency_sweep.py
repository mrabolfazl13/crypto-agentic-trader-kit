"""The sweep must refuse to lie about the runs it reuses.

`tools/paper_concurrency_sweep.py` re-scores trade timelines recorded by the frequency replay,
so its only defence against comparing apples with oranges is the K=1 reproduction check: if the
schedule function no longer returns the number that was recorded, the whole curve is invalid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from paper_concurrency_sweep import (  # noqa: E402
    CONCURRENCY_SWEEP, both_windows_pass_at, curve)
from paper_frequency_replay import serial_account_schedule  # noqa: E402

LIMITS = SimpleNamespace(max_trades_per_day=4, cooldown_minutes_after_loss=60,
                         max_account_allocation_pct=5.0)


def _artifact(tmp_path: Path, timelines: list[list[dict]], days: float) -> Path:
    recorded = serial_account_schedule(timelines, window_days=days,
                                       max_trades_per_day=LIMITS.max_trades_per_day,
                                       cooldown_minutes_after_loss=LIMITS.cooldown_minutes_after_loss)
    doc = {"summary": {"timeframe": "1h", "counted_from": "2026-05-01", "counted_to": "2026-08-01",
                       "counted_days_per_symbol": days, "cadence": "production",
                       "decide_every_bars": 3, "symbols_replayed": len(timelines),
                       "months_read": ["2026-05", "2026-06", "2026-07"],
                       "data_version": {"manifest": "test", "months": ["2026-05", "2026-06", "2026-07"]},
                       "single_account_schedule": recorded},
           "per_symbol": [{"closed_trades": len(timeline), "trade_timeline": timeline}
                          for timeline in timelines]}
    path = tmp_path / "frequency-1h.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_the_sweep_reuses_recorded_timelines_and_reports_a_reproduction_flag(tmp_path):
    timelines = [[{"symbol": f"S{i}", "entry_time": i * 3600, "exit_time": i * 3600 + 5 * 3600,
                   "pnl": 1.0 if i % 4 else -1.0, "fees": 0.05, "reason": "target"}
                  for i in range(14)]]
    row = curve(_artifact(tmp_path, timelines, 3.0), limits=LIMITS, sweep=CONCURRENCY_SWEEP)
    assert row["reproduces_recorded_k1"] is True
    assert row["lake_label_covers_every_month_read"] is True
    assert [point["max_concurrent"] for point in row["curve"]] == list(CONCURRENCY_SWEEP)
    assert [point["taken"] for point in row["curve"]] == sorted(
        point["taken"] for point in row["curve"])
    assert row["curve"][0]["taken"] == row["recorded_k1"]["taken"]
    assert all(point["peak_concurrent_positions"] <= point["max_concurrent"] for point in row["curve"])


def test_a_schedule_that_no_longer_matches_the_record_is_flagged_not_silently_used(tmp_path):
    timelines = [[{"symbol": "S0", "entry_time": 0, "exit_time": 3600, "pnl": -1.0,
                   "fees": 0.05, "reason": "stop"}]]
    path = _artifact(tmp_path, timelines, 3.0)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["summary"]["single_account_schedule"]["taken"] = 99
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert curve(path, limits=LIMITS, sweep=CONCURRENCY_SWEEP)["reproduces_recorded_k1"] is False


def test_a_daily_sourced_month_is_flagged_when_the_label_only_covers_the_monthly_lake(tmp_path):
    """F-025: w3 read 2026-10 from the daily path while `data_version` stayed the monthly digest."""
    timelines = [[{"symbol": "S0", "entry_time": 0, "exit_time": 3600, "pnl": 1.0,
                   "fees": 0.05, "reason": "target"}]]
    path = _artifact(tmp_path, timelines, 3.0)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["summary"]["months_read"] = ["2026-05", "2026-06", "2026-07", "2026-10"]
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert curve(path, limits=LIMITS, sweep=CONCURRENCY_SWEEP)[
        "lake_label_covers_every_month_read"] is False


def test_the_sweep_covers_the_deployed_serial_case_and_wider_budgets():
    assert CONCURRENCY_SWEEP[0] == 1, "the deployed engine is serial; the curve must start there"
    assert 5 in CONCURRENCY_SWEEP


def _row(timeframe: str, per_day_by_k: dict[int, float]) -> dict:
    return {"timeframe": timeframe, "curve": [
        {"max_concurrent": k, "trades_per_day": per_day_by_k[k], "profit_factor": 0.7,
         "net_pnl_per_1000": -1.0, "allocation_cap_needed_pct": 5.0 * k}
        for k in sorted(per_day_by_k)]}


def test_a_timeframe_only_counts_when_every_window_clears_the_gate_at_the_same_k():
    verdicts = both_windows_pass_at([_row("1h", {1: 0.87, 2: 1.29, 3: 1.48, 5: 1.59}),
                                     _row("1h", {1: 1.04, 2: 1.96, 3: 2.23, 5: 2.54})])
    assert verdicts["1h"]["min_k_passing_everywhere"] == 2
    assert verdicts["1h"]["expectancy_positive_at_that_k"] is False
    assert verdicts["1h"]["allocation_cap_needed_pct_at_that_k"] == [10.0, 10.0]

    only_one = both_windows_pass_at([_row("15m", {1: 1.58, 2: 2.04, 3: 2.19, 5: 2.27})])
    assert only_one["15m"]["min_k_passing_everywhere"] is None
    assert only_one["15m"]["windows"] == 1

    never = both_windows_pass_at([_row("4h", {1: 0.18, 2: 0.26, 3: 0.33, 5: 0.39}),
                                  _row("4h", {1: 0.08, 2: 0.15, 3: 0.23, 5: 0.27})])
    assert never["4h"]["min_k_passing_everywhere"] is None
    assert never["4h"]["windows"] == 2
