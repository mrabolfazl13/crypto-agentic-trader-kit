"""Tests for the D-031 hold-distribution probe (read-only).

These lock the correction to D-030. Three facts matter and all are asserted here:

  1. The `bars` field is elapsed MINUTES (inclusive), not a bar count. Treating it
     as a bar count inflates hold times by the timeframe factor (240x at 4h).
  2. The 4h backtest dumps are right-censored at `quant.backtest.max_hold_hours`
     (24h), while the DEPLOYED strategy has `max_hold_bars = 0`, i.e. no time
     exit. So the measured distribution is a lower bound on deployed hold time.
  3. Because of that censoring, the one-slot cadence verdict is INDETERMINATE,
     not the flat "arithmetically impossible" D-030 claimed.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TOOL = ROOT / "tools" / "paper_hold_distribution_probe.py"
ARTIFACT = ROOT / "state" / "quant" / "results" / "d031_hold" / "hold_distribution.json"
SAMPLE_DUMP = (
    ROOT / "state/quant/results/nested_walk_forward_regime/nested-wf-trend_pullback-1h.trades.json"
)


def test_bars_field_is_minutes_not_bar_count():
    """`bars` tracks elapsed minutes (off-by-one inclusive), never elapsed/bar_seconds."""
    d = json.loads(SAMPLE_DUMP.read_text(encoding="utf-8"))
    trades = [t for v in d["folds"].values() if isinstance(v, list) for t in v]
    sample = [t for t in trades if t.get("bars") and t.get("entry_ts") and t.get("exit_ts")][:20]
    assert sample
    for t in sample:
        elapsed_min = (t["exit_ts"] - t["entry_ts"]) / 60.0
        assert t["bars"] == pytest.approx(elapsed_min + 1.0, abs=0.01)


def test_backtest_cap_exists_and_is_24_hours():
    """The 24h cap is a BACKTEST setting; it is what censors the distribution."""
    from cat.config import load_config

    cfg = load_config(root=ROOT)
    assert cfg.raw["quant"]["backtest"]["max_hold_hours"] == 24


def test_deployed_strategy_has_no_time_exit():
    """max_hold_bars = 0 in deployed params: holds are uncapped, so the backtest
    distribution understates deployed hold time."""
    from cat.config import load_config

    cfg = load_config(root=ROOT)
    assert cfg.strategy_params.max_hold_bars == 0


def test_measured_holds_respect_the_backtest_cap():
    """No measured 4h hold should exceed the backtest cap by more than one bar."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    cap_days = 24 / 24.0
    for dump in payload["dumps"]:
        assert dump["max_days"] <= cap_days + 0.05, (dump["strategy"], dump["max_days"])


def test_one_slot_ceiling_arithmetic_is_consistent():
    """Recompute the ceiling from the stored mean rather than trusting the flag."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    ceil_ = payload["one_slot_ceiling"]
    required = payload["required_per_day"]

    mean_hold = payload["pooled"]["mean_days"]
    recomputed = 1.0 / mean_hold
    assert recomputed == pytest.approx(ceil_["at_pooled_mean_per_day"], rel=0.01)
    assert ceil_["clears_requirement_at_mean"] is (recomputed >= required)


def test_censoring_is_reported_and_break_even_is_consistent():
    """46% of measured trades hit the 24h cap, so the pooled mean is a lower bound.
    The break-even censored mean must reproduce the stated arithmetic."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    cen = payload["censoring"]
    pooled = payload["pooled"]
    required = payload["required_per_day"]

    assert cen["n_censored"] > 0
    assert cen["n_censored"] + cen["n_uncensored"] == pooled["n"]
    assert 0.3 < cen["n_censored"] / pooled["n"] < 0.6
    assert "indeterminate" in cen["verdict"]

    n_u, n_c = cen["n_uncensored"], cen["n_censored"]
    N = pooled["n"]
    hc = cen["break_even_censored_mean_days"]
    overall = (n_u * cen["uncensored_mean_days"] + n_c * hc) / N
    assert overall == pytest.approx(1.0 / required, rel=0.01)

    clears = [s["clears"] for s in cen["sensitivity"]]
    assert clears[0] is True and clears[-1] is False


def test_probe_runs_and_writes_artifact():
    result = subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert ARTIFACT.exists()

    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["pooled"]["n"] > 500
    assert payload["pooled"]["mean_days"] > 0
    assert len(payload["dumps"]) >= 5
    assert "minutes" in payload["bars_field_note"]


def test_probe_is_read_only():
    """The trade dumps and config must be byte-identical after a run."""
    cfg_path = ROOT / "config.yaml"
    before_cfg = cfg_path.read_bytes()
    dumps = sorted((ROOT / "state" / "quant" / "results").rglob("*trades*.json"))
    before = {p: p.read_bytes() for p in dumps}

    subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180, check=True
    )

    assert cfg_path.read_bytes() == before_cfg
    for p, b in before.items():
        assert p.read_bytes() == b, p
