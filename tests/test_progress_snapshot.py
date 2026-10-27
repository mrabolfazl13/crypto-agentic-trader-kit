"""The recovery file's acceptance rate must count verdicts, not result files.

`progress.json` is what a fresh session reads first, so an inflated denominator
(or a replay counted as a second success) would silently change how the whole
project's evidence looks. Pure-function tests on synthetic tally rows only.
"""

from __future__ import annotations

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
