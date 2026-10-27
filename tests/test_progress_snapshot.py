"""The recovery file's acceptance rate must count verdicts, not result files.

`progress.json` is what a fresh session reads first, so an inflated denominator
(or a replay counted as a second success) would silently change how the whole
project's evidence looks. Pure-function tests on synthetic tally rows only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import progress_snapshot as snap  # noqa: E402


def _run(*, run_id: str, dir_: str, decision: str, window: str = "2026-03-01",
         window_end: str = "2026-04-21", trades: int = 175, net: float = 3.8087) -> dict:
    return {
        "id": run_id,
        "dir": dir_,
        "window": window,
        "window_end": window_end,
        "decision": decision,
        "trades": trades,
        "net_sum_pct": net,
        "excess_pp": round(net - 1.5093, 4),
        "profit_factor": 1.2549,
        "positive_fold_pct": 75.0,
    }


def test_a_replayed_verdict_counts_once_but_keeps_every_run_id_and_directory():
    tally = snap.acceptance_tally([
        _run(run_id="nested-wf-ema_trend-4h", dir_="nested_wf_determinism_a", decision="accepted"),
        _run(run_id="nested-wf-ema_trend-4h-train_breadth", dir_="nested_walk_forward_breadth_may_v2",
             decision="accepted"),
    ])
    assert tally["runs_with_decision"] == 2
    assert tally["accepted_files"] == 2
    assert tally["accepted_verdicts"] == 1
    detail = tally["accepted_detail"][0]
    assert detail["dirs"] == ["nested_walk_forward_breadth_may_v2", "nested_wf_determinism_a"]
    assert len(detail["run_ids"]) == 2
    assert tally["note"]


def test_the_same_verdict_on_a_different_window_is_a_different_verdict():
    tally = snap.acceptance_tally([
        _run(run_id="nested-wf-ema_trend-4h", dir_="win_a", decision="accepted"),
        _run(run_id="nested-wf-ema_trend-4h", dir_="win_b", decision="accepted",
             window_end="2026-08-08", trades=191, net=-11.0537),
    ])
    assert tally["accepted_verdicts"] == 2
    assert tally["distinct_verdicts"] == 2


def test_rejections_are_counted_in_the_denominator_and_never_listed_as_accepted():
    tally = snap.acceptance_tally([
        _run(run_id="nested-wf-breakout-4h", dir_="guarded", decision="rejected", trades=220, net=-2.5),
        _run(run_id="nested-wf-ema_trend-4h", dir_="guarded", decision="accepted"),
    ])
    assert tally["rejected_files"] == 1
    assert tally["distinct_verdicts"] == 2
    assert [row["run_ids"] for row in tally["accepted_detail"]] == [["nested-wf-ema_trend-4h"]]
    assert tally["accepted_detail"][0]["dirs"] == ["guarded"]


def test_undecided_runs_are_excluded():
    runs = [_run(run_id="nested-wf-momentum-4h", dir_="smoke", decision=None)]
    tally = snap.acceptance_tally(runs)
    assert tally["runs_with_decision"] == 0
    assert tally["distinct_verdicts"] == 0
    assert tally["accepted_detail"] == []


def test_gate_b_summary_carries_the_decision_funnel(tmp_path, monkeypatch):
    """The recovery file must show *why* the loop stayed silent, not just that it did.

    Diagnostic only: these keys come straight from the recorded funnel and the
    status shown is whatever the gate itself decided.
    """
    reports = tmp_path / "state" / "reports"
    reports.mkdir(parents=True)
    (reports / "gate_b_evidence_server.json").write_text(json.dumps({
        "status": "INCOMPLETE", "min_days": 28.0, "evaluated_at": "2026-10-27T18:40:00+00:00",
        "database": "state/reports/server_platform_snapshot.sqlite3",
        "window": {"span_days": 1.504, "on_time_fraction": 100.0, "cycles_observed": 13,
                   "downtime_gaps": 0, "days_remaining": 26.496, "milestone_at": None},
        "ledger": {"trades": 0},
        "cadence": {"state": "stalled", "required_trades_per_day": 1.1322},
        "decision_funnel": {"decisions": 1300, "cycles": 13, "symbols": 100,
                            "by_reason_pct": {"regime_filter": 93.15, "no_atr": 3.23,
                                              "executed:buy": 0.08},
                            "executed": {"executed:buy": 1}},
    }), encoding="utf-8")
    monkeypatch.setattr(snap, "ROOT", tmp_path)
    summary = snap.gate_b()["server"]
    assert summary["decision_symbol_cycles"] == 1300
    assert summary["top_decision_reason"] == "regime_filter"
    assert summary["top_decision_reason_pct"] == 93.15
    assert summary["executed_decisions"] == 1
    assert summary["status"] == "INCOMPLETE"


def test_gate_b_summary_survives_an_evidence_file_without_a_funnel(tmp_path, monkeypatch):
    """Older reports predate the funnel: the keys are absent/None, never a crash."""
    reports = tmp_path / "state" / "reports"
    reports.mkdir(parents=True)
    (reports / "gate_b_evidence_server.json").write_text(json.dumps(
        {"status": "INCOMPLETE", "window": {}, "ledger": {}, "cadence": {}}), encoding="utf-8")
    monkeypatch.setattr(snap, "ROOT", tmp_path)
    summary = snap.gate_b()["server"]
    assert summary["decision_symbol_cycles"] is None
    assert summary["top_decision_reason"] is None
    assert summary["executed_decisions"] == 0
