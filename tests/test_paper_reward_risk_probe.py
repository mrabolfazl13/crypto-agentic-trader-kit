"""Tests for the D-029 reward_risk probe (read-only arithmetic + selectivity)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import load_config  # noqa: E402

TOOL = ROOT / "tools" / "paper_reward_risk_probe.py"


def _cfg():
    return load_config(root=ROOT)


def test_breakeven_identity_matches_algebra():
    """rr >= min_rr must reduce exactly to atr_pct >= K * round_trip_cost."""
    cfg = _cfg()
    p = cfg.strategy_params
    stop, target = p.atr_stop_mult, p.atr_target_mult
    min_rr = cfg.limits.min_reward_risk_ratio
    k = (1.0 + min_rr) / (target - min_rr * stop)

    for atr_pct in (0.2, 0.48, 0.6, 1.0, 2.0, 3.0):
        for cost_pct in (0.24, 0.30, 0.45, 0.60):
            a = atr_pct / 100.0
            c = cost_pct / 100.0
            rr = (target * a - c) / (stop * a + c)
            assert (rr >= min_rr) == (a >= k * c), (atr_pct, cost_pct, rr, k)


def test_breakeven_k_is_two_for_deployed_config():
    """The deployed multipliers make the identity collapse to a clean factor of 2."""
    cfg = _cfg()
    p = cfg.strategy_params
    k = (1.0 + cfg.limits.min_reward_risk_ratio) / (
        p.atr_target_mult - cfg.limits.min_reward_risk_ratio * p.atr_stop_mult
    )
    assert k == pytest.approx(2.0)


def test_cost_bounds_follow_slippage_formula():
    """Cost bounds must come from the deployed slippage formula, not a guess."""
    cfg = _cfg()
    cap = cfg.limits.max_slippage_pct
    fees = cfg.maker_fee_pct + cfg.taker_fee_pct
    c_min = (fees + 2 * 0.02) / 100.0
    c_max = (fees + 2 * cap) / 100.0
    assert c_min < c_max
    assert c_min == pytest.approx(0.0024)
    assert c_max == pytest.approx(0.0060)


def test_probe_runs_and_writes_artifact():
    """The probe must run read-only and produce its JSON artifact."""
    result = subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr

    out = ROOT / "state" / "quant" / "results" / "d029_reward_risk" / "reward_risk_gate.json"
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert payload["algebra"]["K"] == pytest.approx(2.0)
    assert payload["algebra"]["identity"].startswith("rr >= min_rr")
    sel = payload["selectivity"]
    assert sel["symbols_measured"] > 0
    # The gate must be monotone: the expensive bound admits no more than the permissive one.
    assert sel["admitted_at_expensive"] <= sel["admitted_at_permissive"]
    # The ledger limitation must be recorded, not silently ignored.
    assert "reward_risk" in payload["ledger_limitation"]


def test_probe_does_not_touch_config_or_ledger():
    """Read-only guarantee: config and the snapshot must be byte-identical after a run."""
    cfg_path = ROOT / "config.yaml"
    db_path = ROOT / "state" / "reports" / "server_platform_snapshot.sqlite3"
    before_cfg = cfg_path.read_bytes()
    before_db = db_path.read_bytes() if db_path.exists() else None

    subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180, check=True
    )

    assert cfg_path.read_bytes() == before_cfg
    if before_db is not None:
        assert db_path.read_bytes() == before_db
