"""Tests for the D-032 hold-divergence probe (read-only).

Locks the finding that the backtest and the deployment are two different systems:

  backtest  applies a 24h time exit unconditionally (sim.py:120,197)
  deployed  has max_hold_bars = 0, absent from config.yaml, so the exit never fires

53.34% of 4h backtest trades exit on that time cap. None of them would in
deployment. Nothing here changes config, thresholds or strategy.py.
"""

import json
import pytest
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TOOL = ROOT / "tools" / "paper_hold_divergence_probe.py"
ARTIFACT = ROOT / "state" / "quant" / "results" / "d032_hold_divergence" / "hold_divergence.json"


def test_deployed_strategy_has_no_time_exit():
    """max_hold_bars = 0 in deployed params, so strategy.py:253 never fires."""
    from cat.config import load_config

    cfg = load_config(root=ROOT)
    assert cfg.strategy_params.max_hold_bars == 0


def test_max_hold_bars_is_absent_from_config_yaml():
    """The deployed exit is off by omission, not by an explicit 0."""
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "max_hold_bars" not in text
    assert "max_hold_hours" in text


def test_backtest_has_a_24_hour_cap():
    """The backtest cap is what censors the distribution D-031 measured."""
    from cat.config import load_config

    cfg = load_config(root=ROOT)
    assert cfg.raw["quant"]["backtest"]["max_hold_hours"] == 24


def test_backtest_cap_is_applied_unconditionally_in_sim():
    """sim.py must compute and apply the hold cap without a >0 guard."""
    src = (ROOT / "src" / "cat" / "quant" / "sim.py").read_text(encoding="utf-8")
    assert "max_hold_bars = max(1, int(round(settings.max_hold_hours * 60)))" in src
    assert "last = min(start + max_hold_bars - 1" in src


def test_deployed_exit_is_guarded_by_a_positive_check():
    """strategy.py only acts when max_hold_bars > 0, which is never true here."""
    src = (ROOT / "src" / "cat" / "strategy.py").read_text(encoding="utf-8")
    assert "if p.max_hold_bars > 0 and horizon is not None" in src


@pytest.mark.requires_research_state
def test_time_exit_share_is_material():
    """The cap closes more than half of all 4h backtest trades."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    share = payload["time_exit_share_pct"]
    assert share > 40.0
    assert payload["h2_cap_binds"] is True

    by_reason = {r["reason"]: r for r in payload["by_reason"]}
    assert "time" in by_reason
    assert by_reason["time"]["n"] > 900


@pytest.mark.requires_research_state
def test_cap_actually_binds_at_the_boundary():
    """Most time exits land at the 24h cap, not merely somewhere before it."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["time_exits_at_cap_pct"] > 80.0


@pytest.mark.requires_research_state
def test_live_position_exceeded_the_backtest_cap():
    """ASTER was older than 24h and still open, so the backtest would have closed it."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    live = payload["live"]
    assert live["age_hours"] > 24.0
    assert live["backtest_would_have_closed"] is True
    assert payload["h4_live_evidence"] is True


@pytest.mark.requires_research_state
def test_verdict_is_validation_divergence():
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["verdict"] == "validation-divergence"
    assert payload["deployed"]["time_exit_can_fire"] is False
    assert payload["backtest"]["applied_unconditionally"] is True


@pytest.mark.requires_research_state
def test_probe_runs_and_is_read_only():
    cfg_path = ROOT / "config.yaml"
    strat = ROOT / "src" / "cat" / "strategy.py"
    before = {p: p.read_bytes() for p in (cfg_path, strat)}

    result = subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert ARTIFACT.exists()

    for p, b in before.items():
        assert p.read_bytes() == b, p

    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["h1_divergence_real"] is True
    assert "owner decision" in payload["note"]
