"""Tests for the D-030 slot-reachability probe (read-only arithmetic)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import load_config  # noqa: E402

TOOL = ROOT / "tools" / "paper_slot_reachability_probe.py"
ARTIFACT = ROOT / "state" / "quant" / "results" / "d030_slots" / "slot_reachability.json"


def _cfg():
    return load_config(root=ROOT)


def test_total_cap_is_smaller_than_one_position_cap():
    """The config is internally inconsistent: per-position cap exceeds the total cap."""
    cfg = _cfg()
    total_cap = cfg.allocated_capital * cfg.limits.max_account_allocation_pct / 100.0
    position_cap = cfg.allocated_capital * cfg.limits.max_position_pct_of_allocated_capital / 100.0
    assert position_cap > total_cap
    # The ratio is what makes max_open_positions unreachable.
    assert total_cap / position_cap < 1.0


def test_second_slot_is_unreachable_by_construction():
    """max_open_positions cannot exceed floor(total_cap / per_position_cap)."""
    cfg = _cfg()
    total_cap = cfg.allocated_capital * cfg.limits.max_account_allocation_pct / 100.0
    position_cap = cfg.allocated_capital * cfg.limits.max_position_pct_of_allocated_capital / 100.0
    reachable = min(cfg.max_open_positions, int(total_cap / position_cap))
    assert reachable < cfg.max_open_positions
    assert reachable < 2


def test_one_slot_cadence_ceiling_is_below_gate_b_requirement():
    """With one effective slot, 1/H per day cannot reach the gate-B rate at this hold."""
    required = 1.1768  # gate-B cadence requirement, snapshot-derived
    held_days = 1.0031  # ASTER age at the last snapshot
    ceiling = 1.0 / held_days
    assert ceiling < required
    # The hold would have to be much shorter than a 4h bar multiple to clear it.
    assert 1.0 / required < held_days


def test_probe_runs_and_writes_artifact():
    result = subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert ARTIFACT.exists()

    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["slots"]["second_slot_reachable"] is False
    assert payload["slots"]["reachable"] < payload["slots"]["configured_max_open"]
    assert payload["caps"]["total_deployed_cap"] < payload["caps"]["per_position_cap"]
    assert payload["cadence_ceiling"]["one_slot_clears_requirement"] is False
    assert "rounds to zero" in payload["bnb_rejection"]


def test_probe_is_read_only():
    """Config and the snapshot must be byte-identical after a run."""
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


def test_live_snapshot_confirms_one_open_position():
    """The deployed loop held exactly one position, matching the arithmetic."""
    if not ARTIFACT.exists():
        pytest.skip("artifact not built yet")
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["live"]["open_positions"] == 1
    assert payload["live"]["remaining_headroom"] < payload["caps"]["per_position_cap"]
