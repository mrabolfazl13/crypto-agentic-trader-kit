"""The grid aggregator must not be able to compare two cells that are not the same experiment.

`tools/paper_threshold_grid.py` only re-times trade timelines recorded by the frequency replay,
so everything it reports rests on two assumptions it cannot inherit from the replay: that a folder
called `lam1.5_w1_4h` really was replayed with thresholds 1.5x the shipped ones, and that a cell
counts toward the pre-registered rule only when *both* windows clear it at the same concurrency.
Both are checked here rather than assumed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from paper_threshold_grid import (  # noqa: E402
    LAMBDAS, REQUIRED_PF, TIMEFRAMES, WINDOWS, artifact_path, cell, expected_thresholds,
    satisfies_rule, top_rejections)

LIMITS = SimpleNamespace(max_trades_per_day=4, cooldown_minutes_after_loss=60)


def _timeline(n: int, *, first_loss_at: int = 0) -> list[dict]:
    return [{"symbol": f"S{i}", "entry_time": i * 3600, "exit_time": i * 3600 + 7200,
             "pnl": -1.0 if i == first_loss_at else 1.0, "fees": 0.05,
             "reason": "stop" if i == first_loss_at else "target"}
            for i in range(n)]


def _artifact(tmp_path: Path, *, lam: float, timeframe: str, timelines: list[list[dict]],
              days: float = 3.0) -> Path:
    thresholds = expected_thresholds(lam)
    doc = {"summary": {
        "timeframe": timeframe, "counted_days_per_symbol": days,
        "counted_from": "2026-05-01", "counted_to": "2026-08-01",
        "cadence": "production", "decide_every_bars": {"4h": 1, "1h": 3, "15m": 12}[timeframe],
        "symbols_replayed": len(timelines), "decision_bars_total": 60 * len(timelines),
        "classifier": {"label": f"trend-pullback:v1@lam{lam}", **thresholds},
        "months_read": ["2026-05", "2026-06"],
        "data_version": {"manifest": "test", "months": ["2026-05", "2026-06"]},
        "holding_hours": {"mean_of_means": 12.5},
        "rejections_pooled": {"regime_filter": 40, "LINK_USDT not in approved symbol list": 3},
    }, "per_symbol": [{"closed_trades": len(t), "trade_timeline": t} for t in timelines]}
    path = tmp_path / f"frequency-{timeframe}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_lambda_one_is_read_from_the_shipped_run_and_every_other_lambda_from_the_grid():
    grid = Path("/g")
    shipped = Path("/s")
    assert artifact_path(1.0, "w3", "1h", grid=grid, shipped=shipped) == Path(
        "/s/freqprod_w3_100_1h/frequency-1h.json")
    assert artifact_path(1.5, "w1", "4h", grid=grid, shipped=shipped) == Path(
        "/g/lam1.5_w1_4h/frequency-4h.json")


def test_the_baseline_lambda_is_the_shipped_thresholds_and_the_rest_scale_all_three_entry_gates():
    base = expected_thresholds(1.0)
    assert base == {"vol_threshold": 1.0, "atr_pct_threshold": 1.5, "slope_threshold": 0.06}
    for lam in LAMBDAS:
        scaled = expected_thresholds(lam)
        assert set(scaled) == set(base)
        assert all(scaled[key] == pytest.approx(base[key] * lam) for key in base)


def test_a_cell_re_times_the_recorded_timeline_at_the_deployed_and_the_wider_budget(tmp_path):
    path = _artifact(tmp_path, lam=0.75, timeframe="1h", timelines=[_timeline(12), _timeline(9)])
    row = cell(path, lam=0.75, window="w1", limits=LIMITS)
    assert row["thresholds"] == expected_thresholds(0.75)
    assert row["candidates"] == 21
    assert row["symbols_blocked_by_allowlist"] == 1
    assert row["lake_label_covers_every_month_read"] is True
    assert sorted(row["schedules"]) == ["1", "2"]
    assert row["schedules"]["1"]["taken"] <= row["schedules"]["2"]["taken"]


def test_a_folder_that_lies_about_its_lambda_raises_instead_of_becoming_a_cell(tmp_path):
    """Only the directory name claims a cell is lambda=X; the file itself has to agree."""
    path = _artifact(tmp_path, lam=2.0, timeframe="4h", timelines=[_timeline(4)])
    with pytest.raises(ValueError, match="do not match lambda"):
        cell(path, lam=0.5, window="w1", limits=LIMITS)


def test_an_empty_cell_keeps_the_gate_that_emptied_it_and_drops_the_allowlist_noise(tmp_path):
    """The vol/atr thresholds are ceilings, so an empty cell has to say which gate ate it."""
    pooled = {"regime_filter": 52480, "no_atr": 2342, "reward_risk": 136,
              "ZAMA_USDT not in approved symbol list": 36}
    top = top_rejections({"rejections_pooled": pooled}, limit=3)
    assert list(top) == ["regime_filter", "no_atr", "reward_risk"]
    assert all("approved symbol list" not in key for key in top)

    path = _artifact(tmp_path, lam=0.5, timeframe="15m", timelines=[[], []])
    row = cell(path, lam=0.5, window="w1", limits=LIMITS)
    assert row["candidates"] == 0 and row["decision_ticks"] == 120
    assert list(row["funnel_top"])[0] == "regime_filter"
    assert row["symbols_blocked_by_allowlist"] == 1


def _cand(lam: float, timeframe: str, window: str, *, k1: tuple, k2: tuple) -> dict:
    def side(pair):
        return {"profit_factor": pair[0], "net_pnl_per_1000": pair[1]}
    return {"lambda": lam, "timeframe": timeframe, "window": window,
            "schedules": {"1": side(k1), "2": side(k2)}}


def test_a_lambda_only_counts_when_both_windows_clear_the_rule_at_the_same_concurrency():
    clear = (1.30, 4.0)
    cells = [_cand(1.5, "1h", "w1", k1=(None, 0.0), k2=clear),
             _cand(1.5, "1h", "w3", k1=(None, 0.0), k2=clear)]
    assert satisfies_rule(cells)["lam1.5·1h"] == ["K=2"]

    half = [_cand(1.5, "1h", "w1", k1=(None, 0.0), k2=clear)]
    assert satisfies_rule(half)["lam1.5·1h"] == [], "one window is not the pre-registered rule"

    mixed_k = [_cand(0.5, "4h", "w1", k1=clear, k2=(None, 0.0)),
               _cand(0.5, "4h", "w3", k1=(None, 0.0), k2=clear)]
    assert satisfies_rule(mixed_k)["lam0.5·4h"] == [], "w1 passing at K=1 and w3 at K=2 is not one cell"


def test_the_rule_fails_on_the_expected_profit_factor_and_on_a_breakeven_net():
    both = (REQUIRED_PF, 0.1)
    cells = [_cand(2.0, "15m", "w1", k1=both, k2=(None, 0.0)),
             _cand(2.0, "15m", "w3", k1=both, k2=(None, 0.0))]
    assert satisfies_rule(cells)["lam2.0·15m"] == ["K=1"], "PF == the floor still clears it"

    below = [_cand(2.0, "15m", "w1", k1=(REQUIRED_PF - 0.01, 9.0), k2=(None, 0.0)),
             _cand(2.0, "15m", "w3", k1=(REQUIRED_PF, 9.0), k2=(None, 0.0))]
    assert satisfies_rule(below)["lam2.0·15m"] == []

    breakeven = [_cand(2.0, "1h", "w1", k1=(1.5, 0.0), k2=(None, 0.0)),
                 _cand(2.0, "1h", "w3", k1=(1.5, 0.0), k2=(None, 0.0))]
    assert satisfies_rule(breakeven).get("lam2.0·1h") == [], "net must be strictly positive"

    zero_win = [_cand(0.5, "4h", "w1", k1=(None, 0.0), k2=(None, 0.0)),
                _cand(0.5, "4h", "w3", k1=(None, 0.0), k2=(None, 0.0))]
    assert satisfies_rule(zero_win)["lam0.5·4h"] == [], "a cell with no trades cannot pass"


def test_the_grid_covers_the_shipped_threshold_level_and_two_independent_windows():
    assert 1.0 in LAMBDAS, "without the shipped level the grid measures a shift, not a curve"
    assert list(WINDOWS) == ["w1", "w3"]
    assert set(TIMEFRAMES) == {"4h", "1h", "15m"}
