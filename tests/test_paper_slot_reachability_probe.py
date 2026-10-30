"""Tests for the D-030 slot-reachability probe (read-only arithmetic).

The ceiling and slot locks are pure arithmetic now (D-037): a test that quoted the trades open on
one particular day broke the moment the ledger grew, so the verdict rules are pinned against
synthetic trade lists and the live artifact is only read for what it must always say.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from cat.config import load_config  # noqa: E402

import paper_slot_reachability_probe as probe  # noqa: E402

TOOL = ROOT / "tools" / "paper_slot_reachability_probe.py"
ARTIFACT = ROOT / "state" / "quant" / "results" / "d030_slots" / "slot_reachability.json"
REQUIRED = 1.1552  # a gate-B requirement; the real one is read from the artifact by the probe


def _trade(symbol: str, hold_seconds: int, *, exit_ts: int = 1_800_000_000) -> dict:
    """Whole seconds only: a truncated entry_ts changes the rounded ceiling and fakes a diff."""
    return {"symbol": symbol, "entry_ts": exit_ts - hold_seconds, "exit_ts": exit_ts,
            "reason": "exit_stop"}


ASTER = 119212  # 33.1144 h — the closed ASTER trade in the deployed snapshot
BNB = 54107     # 15.0297 h — the closed BNB trade


def _ceiling(trades: list[dict]) -> dict:
    return probe.cadence_ceiling(trades=trades, positions=[], snapshot_ts="2026-10-30T00:00:00+00:00",
                                 required=REQUIRED)


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
    """A position funded at the total cap leaves no headroom for a second one."""
    cfg = _cfg()
    total_cap = cfg.allocated_capital * cfg.limits.max_account_allocation_pct / 100.0
    position_cap = cfg.allocated_capital * cfg.limits.max_position_pct_of_allocated_capital / 100.0
    slots = probe.slot_arithmetic(allocated_cap=total_cap, position_cap=position_cap, deployed=0.0,
                                  open_positions=0, max_open=cfg.max_open_positions)
    assert slots["slots_fundable_at_full_position_cap"] < cfg.max_open_positions
    assert slots["second_slot_reachable"] is False


def test_the_zero_from_the_cap_ratio_is_not_reported_as_no_fundable_slot():
    """F-045: floor(50/200) == 0 while sizing takes min(position cap, headroom), so one 50 USDT slot runs.

    The old artifact printed `reachable = 0` next to documents saying "the real slot is 1"; both were
    true of different quantities.  Each quantity now has its own key.
    """
    slots = probe.slot_arithmetic(allocated_cap=50.0, position_cap=200.0, deployed=0.0,
                                  open_positions=0, max_open=2)
    assert slots["slots_fundable_at_full_position_cap"] == 0
    assert slots["first_slot_notional_budget"] == 50.0
    assert slots["headroom_after_one_sized_down_slot"] == 0.0
    assert slots["binding_cap_for_a_fresh_position"] == "total account allocation"
    assert slots["second_slot_reachable"] is False


def test_a_slot_is_fundable_while_headroom_remains():
    slots = probe.slot_arithmetic(allocated_cap=50.0, position_cap=200.0, deployed=10.0,
                                  open_positions=1, max_open=2)
    assert slots["first_slot_notional_budget"] == 40.0
    assert slots["headroom_after_one_sized_down_slot"] == 0.0


def test_a_position_cap_below_the_total_cap_binds_first():
    slots = probe.slot_arithmetic(allocated_cap=500.0, position_cap=200.0, deployed=0.0,
                                  open_positions=0, max_open=5)
    assert slots["binding_cap_for_a_fresh_position"] == "per-position cap"
    assert slots["slots_fundable_at_full_position_cap"] == 2
    assert slots["second_slot_reachable"] is True


def test_the_ceiling_uses_every_closed_trade_not_the_newest_one():
    """D-037: the two real trades give 0.7248 and 1.5968 around the 1.1552 requirement.

    Reading only the newest (BNB, 15.03h) said "clears"; reading only the older said "does not".
    Neither order may decide the verdict, so the mean holds the number and the band holds the verdict.
    """
    trades = [_trade("ASTER_USDT", ASTER, exit_ts=1_793_245_217),
              _trade("BNB_USDT", BNB, exit_ts=1_793_310_169)]
    out = _ceiling(trades)
    assert out["hold_source"]["closed_trades"] == 2
    assert out["hold_hours"] == pytest.approx(24.0721, abs=1e-3)
    assert out["ceiling_one_slot_per_day"] == pytest.approx(0.997, abs=1e-3)
    assert out["per_trade_band"] == {"min": 0.7248, "max": 1.5968}
    assert out["band_straddles_requirement"] is True
    assert out["one_slot_clears_requirement"] is None


def test_the_verdict_does_not_depend_on_the_order_trades_arrive_in():
    keys = ("hold_hours", "ceiling_one_slot_per_day", "per_trade_band",
            "band_straddles_requirement", "one_slot_clears_requirement")
    trades = [_trade("ASTER_USDT", ASTER), _trade("BNB_USDT", BNB)]
    forward = {key: _ceiling(trades)[key] for key in keys}
    backwards = {key: _ceiling(list(reversed(trades)))[key] for key in keys}
    assert forward == backwards


def test_a_band_below_the_requirement_answers_no():
    out = _ceiling([_trade("X", ASTER), _trade("Y", 144000)])
    assert out["band_straddles_requirement"] is False
    assert out["one_slot_clears_requirement"] is False


def test_a_band_above_the_requirement_answers_yes():
    out = _ceiling([_trade("X", 36000), _trade("Y", BNB)])
    assert out["one_slot_clears_requirement"] is True
    assert out["band_straddles_requirement"] is False


def test_one_trade_is_both_the_mean_and_the_whole_band():
    out = _ceiling([_trade("ASTER_USDT", ASTER)])
    assert out["hold_hours"] == pytest.approx(ASTER / 3600.0, abs=1e-3)
    assert out["per_trade_band"] == {"min": 0.7248, "max": 0.7248}
    assert out["band_straddles_requirement"] is False
    assert out["one_slot_clears_requirement"] is False


def test_no_closed_trade_is_not_measurable_rather_than_a_guess():
    out = _ceiling([])
    assert out["hold_source"]["kind"] == "no_closed_trade"
    assert out["hold_hours"] is None
    assert out["ceiling_one_slot_per_day"] is None
    assert out["one_slot_clears_requirement"] is None
    assert out["band_straddles_requirement"] is None


def test_an_open_or_zero_length_trade_is_skipped_not_average_in():
    out = _ceiling([{"symbol": "OPEN", "entry_ts": 1, "exit_ts": None, "reason": None},
                    _trade("ZERO", 0), _trade("REAL", 36000)])
    assert out["hold_source"]["closed_trades"] == 1
    assert [row["symbol"] for row in out["per_trade"]] == ["REAL"]


def test_the_censored_age_is_recorded_but_never_sets_the_ceiling():
    """A position that just opened makes 1/age beat the requirement — and that is not a hold time.

    This is the D-035/F-039 regression: the ceiling used to be the age of whatever was open, so the
    day the first trade closed and a fresh position started, the same one-slot arithmetic "cleared"
    gate B.  The closed trades must set the number, and the censored age may only be reported.
    """
    positions = [{"symbol": "ETH_USDT", "opened_at": "2026-10-29T23:00:00+00:00"}]
    out = probe.cadence_ceiling(trades=[_trade("ASTER_USDT", ASTER)], positions=positions,
                                snapshot_ts="2026-10-30T00:00:00+00:00", required=REQUIRED)
    age_hours = out["hold_source"]["open_position_age_hours"]
    assert 24.0 / age_hours > out["required_per_day"], "the censored number would have passed"
    assert out["hold_hours"] > age_hours
    assert out["ceiling_one_slot_per_day"] < out["required_per_day"]
    assert out["hold_source"]["censored"] is False


@pytest.mark.requires_research_state
def test_probe_runs_and_writes_artifact():
    result = subprocess.run(
        [sys.executable, str(TOOL)], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert ARTIFACT.exists()

    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["slots"]["second_slot_reachable"] is False
    ceiling = payload["cadence_ceiling"]
    assert ceiling["hold_source"]["kind"] == "closed_trades"
    assert ceiling["hold_source"]["closed_trades"] == len(ceiling["per_trade"]) >= 1
    assert ceiling["required_per_day"] > 0
    assert "rounds to zero" in payload["bnb_rejections"]["sizing_rejection"]["rationale"]


@pytest.mark.requires_research_state
def test_the_published_ceiling_is_the_artifact_s_own_arithmetic():
    """No hardcoded expected verdict: recompute the rule from the per-trade rows the artifact shows."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    ceiling = payload["cadence_ceiling"]
    holds = [row["hold_hours"] for row in ceiling["per_trade"]]
    per_trade_ceilings = [row["ceiling_one_slot_per_day"] for row in ceiling["per_trade"]]
    required = ceiling["required_per_day"]
    assert ceiling["hold_hours"] == pytest.approx(sum(holds) / len(holds), abs=1e-3)
    assert ceiling["ceiling_one_slot_per_day"] == pytest.approx(24.0 / ceiling["hold_hours"], abs=1e-3)
    band = ceiling["per_trade_band"]
    assert band == {"min": min(per_trade_ceilings), "max": max(per_trade_ceilings)}
    straddles = band["min"] < required <= band["max"]
    assert ceiling["band_straddles_requirement"] is straddles
    assert ceiling["one_slot_clears_requirement"] is (None if straddles else band["min"] >= required)


@pytest.mark.requires_research_state
def test_the_two_bnb_rejection_reasons_are_not_pooled():
    """A cooldown rejection says nothing about the allocation cap; the sizing one does."""
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    rejections = payload["bnb_rejections"]
    assert rejections["count"] == len(rejections["rationales"])
    rationales = {text.split('"')[1] for text in rejections["rationales"].values()}
    assert "computed position size rounds to zero" in rationales
    assert rejections["sizing_rejection"]["rationale"].startswith('["computed position size')


@pytest.mark.requires_research_state
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


@pytest.mark.requires_research_state
def test_live_snapshot_headroom_is_below_one_position_cap():
    """The deployed loop can never hold two positions at once: the total cap binds first."""
    if not ARTIFACT.exists():
        pytest.skip("artifact not built yet")
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["live"]["remaining_headroom"] < payload["caps"]["per_position_cap"]
    assert payload["live"]["open_positions"] < payload["config"]["max_open_positions"]
