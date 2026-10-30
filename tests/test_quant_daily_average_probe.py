"""Locks for the D-036 per-day-average probe: what a "positive daily average" may be built from.

The owner asked for a script with a positive daily mean; AGENTS.md rule 2 forbids promising profit,
so the only honest answer is the number itself.  These tests pin the arithmetic (weighted by the
same denominator the benchmark uses), the four pre-registered clauses, and the two traps this
measure has: a sample of one, and two files that are actually one computation.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import quant_daily_average_probe as probe  # noqa: E402

START = "2026-03-01T00:00:00+00:00"
_BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _fold(index: int, *, days: float = 7.0, net: float, bench: float | None = None,
          trades: int = 10) -> dict:
    start = _BASE + timedelta(days=14 * index)
    end = start + timedelta(days=days)
    return {"fold": index, "test": {"start": start.isoformat(), "end": end.isoformat()},
            "metrics": {"net_return_pct": net, "trades": trades},
            "benchmark": ({"return_pct": bench} if bench is not None else {})}


def _run(folds: list[dict], *, decision: str = "accepted", cap: float | None = 24.0,
         run_id: str | None = None,
         window: dict | None = None) -> dict:
    payload = {"run_id": run_id or f"nested-wf-test-{decision}", "code_version": "deadbeef",
               "window": window or {"start": START, "end": "2026-04-21T18:00:00+00:00"},
               "final_test_read": False, "folds": folds,
               "decision": {"decision": decision, "reason": "fixture"}}
    if cap is not None:
        payload["max_hold_hours"] = cap
    return payload


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_fold_daily_divides_net_by_the_actual_test_window(tmp_path: Path):
    row = probe.fold_daily(_fold(0, days=7.0, net=1.4))
    assert row["days"] == 7.0
    assert row["net_per_day_pct"] == 0.2
    assert row["benchmark_net_per_day_pct"] is None, "no benchmark recorded means no benchmark number"


def test_a_fold_without_a_window_or_a_net_is_skipped_not_guessed():
    assert probe.fold_daily({"test": {"start": START}, "metrics": {"net_return_pct": 1.0}}) is None
    assert probe.fold_daily({"test": {"start": START, "end": "2026-03-08T00:00:00+00:00"},
                             "metrics": {}}) is None
    assert probe.fold_daily({"test": {"start": "2026-03-08T00:00:00+00:00", "end": START},
                             "metrics": {"net_return_pct": 1.0}}) is None, "a reversed window is not a day count"


def test_the_mean_is_weighted_so_a_short_win_cannot_carry_a_long_loss(tmp_path: Path):
    """Three 1-day folds made +1.0 each and one 30-day fold lost 10.0: the unweighted mean of the
    per-day numbers is flattering (+0.667), the weighted mean over the same days is negative."""
    folds = [_fold(i, days=1.0, net=1.0) for i in range(3)] + [_fold(3, days=30.0, net=-10.0)]
    payload = _write(tmp_path, "nested-wf-weighted.json", _run(folds))
    run = probe.summarise_run(payload)
    assert run["positive_folds_pct"] == 75.0
    assert run["net_per_day_pct_mean"] > 0, "the unweighted mean is the flattering one"
    assert run["net_per_day_pct_weighted"] < 0
    assert run["clauses"]["positive_mean"] is False
    assert run["positive_daily_average_claim_supported"] is False


def test_a_positive_mean_over_too_small_a_sample_does_not_support_the_claim(tmp_path: Path):
    folds = [_fold(i, days=1.0, net=0.9, bench=0.1) for i in range(4)]
    payload = _write(tmp_path, "nested-wf-short.json", _run(folds))
    run = probe.summarise_run(payload)
    assert run["net_per_day_pct_weighted"] == 0.9
    assert run["clauses"]["sample"] is False, "4 days is not a 28-day sample"
    assert run["failed_clauses"] == ["sample"]


def test_benchmark_is_compared_on_the_same_denominator(tmp_path: Path):
    winning = _write(tmp_path, "nested-wf-a.json",
                     _run([_fold(i, days=7.0, net=1.0, bench=0.1) for i in range(4)]))
    losing = _write(tmp_path, "nested-wf-b.json",
                    _run([_fold(i, days=7.0, net=0.5, bench=0.55) for i in range(4)]))
    a, b = probe.summarise_run(winning), probe.summarise_run(losing)
    assert a["clauses"]["beats_benchmark"] is True
    assert b["clauses"]["beats_benchmark"] is False, "0.5/7 days per day loses to 0.55/7 days per day"
    assert b["net_per_day_pct_weighted"] > 0, "it is still a positive mean: the clause that fails is the benchmark"


def test_a_run_that_records_no_exit_cap_cannot_claim_the_deployed_path(tmp_path: Path):
    """D-034/H3: 73 of the recorded runs never wrote the cap, so their number is a bound on the
    research path — the probe must say so instead of assuming today's config applied."""
    payload = _write(tmp_path, "nested-wf-nocap.json", _run([_fold(i, net=1.0) for i in range(4)], cap=None))
    run = probe.summarise_run(payload)
    assert run["clauses"]["path_labelled"] is False
    assert "path_labelled" in run["failed_clauses"]


def test_the_one_computation_written_twice_is_counted_once(tmp_path: Path):
    """F-042: two protocol labels can be the same run of folds; doubling them would fake a sample."""
    folds = [_fold(i, net=1.0) for i in range(4)]
    _write(tmp_path, "nested-wf-label-a.json", _run(folds, run_id="run-under-universe"))
    _write(tmp_path, "nested-wf-label-b.json", _run(folds, run_id="run-under-train_breadth"))
    report = probe.build_report(tmp_path, tmp_path / "missing-gate-b.json")
    assert report["run_files_scanned"] == 2
    assert report["distinct_computations"] == 1
    assert report["duplicate_groups"] == [{"runs": ["run-under-universe", "run-under-train_breadth"]}]


def test_two_different_fold_outcomes_are_two_computations(tmp_path: Path):
    _write(tmp_path, "nested-wf-a.json", _run([_fold(i, net=1.0) for i in range(4)], run_id="a"))
    _write(tmp_path, "nested-wf-b.json", _run([_fold(i, net=1.1) for i in range(4)], run_id="b"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json")
    assert report["distinct_computations"] == 2 and report["duplicate_groups"] == []


def test_trade_and_partial_dumps_are_not_runs(tmp_path: Path):
    folds = [_fold(i, net=1.0) for i in range(4)]
    _write(tmp_path, "nested-wf-run.json", _run(folds))
    _write(tmp_path, "nested-wf-run.trades.json", {"folds": folds})
    _write(tmp_path, "nested-wf-run.partial.json", {"folds": folds})
    _write(tmp_path, "some_other_report.json", {"folds": folds})
    assert [path.name for path in probe.scan_runs(tmp_path)] == ["nested-wf-run.json"]


def test_unmeasurable_runs_are_counted_not_silently_dropped(tmp_path: Path):
    _write(tmp_path, "nested-wf-empty.json", {"run_id": "x", "folds": []})
    _write(tmp_path, "nested-wf-ok.json", _run([_fold(i, net=1.0) for i in range(4)]))
    report = probe.build_report(tmp_path, tmp_path / "missing.json")
    assert report["run_files_scanned"] == 2 and report["runs_measurable"] == 1
    assert report["runs_unmeasurable"] == 1


def test_the_paper_ledger_is_an_observation_not_an_average(tmp_path: Path):
    gate = _write(tmp_path, "gate_b.json", {
        "evaluated_at": "2026-10-30T00:00:00+00:00", "status": "INCOMPLETE",
        "window": {"span_days": 3.385},
        "ledger": {"orders": 3, "fills": 3, "trades": 1, "net_pnl": -1.1019, "fees": 0.0489},
        "cadence": {"observed_trades_per_day": 0.2954, "required_trades_per_day": 1.1781}})
    ledger = probe.paper_ledger(gate, 1000.0)
    assert ledger["average_claimable"] is False, "n=1 is one observation, never a mean"
    assert ledger["realized_net_pct_per_day_on_allocated_capital"] == round(
        100.0 * -1.1019 / 1000.0 / 3.385, 6)
    assert "one observation" in ledger["note"]


def test_a_missing_gate_b_report_is_reported_as_missing(tmp_path: Path):
    assert probe.paper_ledger(tmp_path / "absent.json", 1000.0) == {
        "source": "gate_b", "available": False, "reason": f"{tmp_path / 'absent.json'} not found"}


def test_the_answer_says_supported_only_when_every_clause_holds(tmp_path: Path):
    good = [_fold(i, days=7.0, net=1.0, bench=0.2) for i in range(4)]
    _write(tmp_path, "nested-wf-good.json", _run(good, decision="accepted"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json", capital_usdt=1000.0)
    assert report["runs_supporting_the_claim"] and report["answer"]["positive_daily_average_proven"] is True
    assert report["answer"]["accepted_and_positive"] is True
    assert report["accepted_summary"][0]["failed_clauses"] == []

    bad = _write(tmp_path, "nested-wf-bad.json", _run([_fold(i, days=7.0, net=-1.0, bench=-2.0)
                                                       for i in range(4)], decision="rejected"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json", capital_usdt=1000.0)
    runs = {run["file"]: run for run in report["runs"]}
    assert runs[str(bad)]["clauses"]["beats_benchmark"] is True, "it beats a worse benchmark..."
    assert runs[str(bad)]["clauses"]["positive_mean"] is False, "...but a negative mean is not a positive daily average"
    assert report["answer"]["accepted_and_positive"] is True, "the accepted run is unchanged by adding a rejected one"


def test_the_answer_says_which_window_carries_the_positive_mean(tmp_path: Path):
    """A supported claim over one 28-day slice is not a cross-window result — the report must say so."""
    march = {"start": "2026-03-01T00:00:00+00:00", "end": "2026-04-21T18:00:00+00:00"}
    june = {"start": "2026-06-01T00:00:00+00:00", "end": "2026-08-31T12:00:00+00:00"}
    _write(tmp_path, "nested-wf-march.json", _run([_fold(i, net=1.0, bench=0.2) for i in range(4)], window=march))
    _write(tmp_path, "nested-wf-june.json", _run([_fold(i, net=-1.0, bench=0.2) for i in range(4)],
                                                 window=june, decision="rejected"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json", capital_usdt=1000.0)
    assert report["distinct_windows_scored"] == 2
    assert report["supported_distinct_windows"] == 1
    assert report["answer"]["single_window_warning"] is True
    assert report["per_window"][f"{june['start']}..{june['end']}"]["claim_supported_runs"] == 0
    assert report["per_window"][f"{march['start']}..{march['end']}"]["net_per_day_pct_best"] > 0
    assert report["per_window"][f"{june['start']}..{june['end']}"]["net_per_day_pct_worst"] < 0

    _write(tmp_path, "nested-wf-june-good.json", _run([_fold(i, days=20.0, net=20.0, bench=0.2)
                                                      for i in range(4)], window=june))
    report = probe.build_report(tmp_path, tmp_path / "missing.json", capital_usdt=1000.0)
    assert report["supported_distinct_windows"] == 2
    assert report["answer"]["single_window_warning"] is False


def test_a_supported_claim_written_twice_is_one_computation(tmp_path: Path):
    folds = [_fold(i, net=1.0, bench=0.2) for i in range(4)]
    _write(tmp_path, "nested-wf-a.json", _run(folds, run_id="label-a"))
    _write(tmp_path, "nested-wf-b.json", _run(folds, run_id="label-b"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json")
    assert len(report["runs_supporting_the_claim"]) == 2
    assert report["supported_distinct_computations"] == 1


def test_protocol_labels_are_one_strategy_family(tmp_path: Path):
    assert probe.strategy_family("nested-wf-ema_trend-4h-train_breadth-hold720h") == "ema_trend-4h"
    assert probe.strategy_family("nested-wf-ema_trend-4h-hold24h") == "ema_trend-4h"
    assert probe.strategy_family("nested_wf-breakout-1h") == "breakout-1h"
    assert probe.strategy_family(None) is None


def test_a_family_positive_on_one_window_is_not_cross_window_evidence(tmp_path: Path):
    march = {"start": "2026-03-01T00:00:00+00:00", "end": "2026-04-21T18:00:00+00:00"}
    june = {"start": "2026-06-01T00:00:00+00:00", "end": "2026-08-31T12:00:00+00:00"}
    _write(tmp_path, "nested-wf-m1.json",
           _run([_fold(i, net=1.0, bench=0.2) for i in range(4)], window=march, run_id="nested-wf-ema_trend-4h"))
    _write(tmp_path, "nested-wf-m2.json",
           _run([_fold(i, net=1.0, bench=0.2) for i in range(4)], window=march,
                run_id="nested-wf-ema_trend-4h-train_breadth-hold24h"))
    _write(tmp_path, "nested-wf-j1.json",
           _run([_fold(i, net=-1.0, bench=0.2) for i in range(4)], window=june,
                run_id="nested-wf-ema_trend-4h", decision="rejected"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json")
    family = report["per_family"]["ema_trend-4h"]
    assert family["windows_scored"] == 2, "two protocol labels are still one strategy"
    assert family["windows_claim_supported"] == [f"{march['start']}..{march['end']}"]
    assert family["positive_daily_average_on_every_scored_window"] is False
    assert report["families_positive_on_two_or_more_windows"] == []

    _write(tmp_path, "nested-wf-j2.json",
           _run([_fold(i, days=20.0, net=20.0, bench=0.2) for i in range(4)], window=june,
                run_id="nested-wf-ema_trend-4h"))
    report = probe.build_report(tmp_path, tmp_path / "missing.json")
    assert report["families_positive_on_two_or_more_windows"] == ["ema_trend-4h"]


def test_the_probe_never_touches_the_acceptance_gate():
    """Reporting measure, not a gate: the shipped acceptance rule must stay exactly as written."""
    import inspect

    from cat.quant.loop import acceptance
    source = inspect.getsource(acceptance)
    assert "positive_folds_pct" in source
    assert "net_per_day" not in source, "D-036 must not have become an acceptance gate"


def _replay_row(**over) -> dict:
    row = {"universe_file": "universe_allowlist.json", "lake_symbols": 103, "timeframe": "4h",
           "cadence": "production", "counted_from": "2026-05-01", "counted_to": "2026-08-01",
           "counted_days_per_symbol": 92.0, "serial_trades": 17, "serial_trades_per_day": 0.1848,
           "profit_factor": 1.173, "net_pnl_per_1000": 1.9721,
           "classifier_key": json.dumps({"max_hold_bars": 0, "name": "trend-pullback"})}
    row.update(over)
    return row


def _write_replay(tmp_path: Path, rows: dict[str, dict]) -> Path:
    return _write(tmp_path, "compare.json", {"rows": rows, "pairs": [], "note": "fixture"})


def test_the_deployed_path_replay_divides_the_recorded_net_by_its_own_window(tmp_path: Path):
    replay = probe.deployed_path_replay(_write_replay(tmp_path, {"alw_w1_4h": _replay_row()}), 1000.0)
    cell = replay["cells"][0]
    assert replay["available"] is True and replay["cells_measured"] == 1
    assert cell["net_pct_of_capital"] == round(100.0 * 1.9721 / 1000.0, 6)
    assert cell["net_per_day_pct"] == round(100.0 * 1.9721 / 1000.0 / 92.0, 6)
    assert cell["max_hold_bars"] == 0, "the path label is read from the recorded classifier, not assumed"
    assert cell["time_exit_in_deployment"] is False
    assert replay["best_per_day_pct"] == cell["net_per_day_pct"]


def test_a_replay_row_without_a_cap_recorded_is_labelled_as_the_research_path(tmp_path: Path):
    replay = probe.deployed_path_replay(
        _write_replay(tmp_path, {"cell": _replay_row(classifier_key=json.dumps({"max_hold_bars": 6}))}), 1000.0)
    assert replay["cells"][0]["time_exit_in_deployment"] is True


def test_a_foldless_in_sample_replay_can_never_carry_the_claim(tmp_path: Path):
    """Replay rows have no train/test split, so the pre-registered sample clause is undefined for
    them.  A large positive replay number must not move the answer, in either direction."""
    replay_path = _write_replay(tmp_path, {"alw_w1_4h": _replay_row(net_pnl_per_1000=9200.0)})
    report = probe.build_report(tmp_path, tmp_path / "missing.json", capital_usdt=1000.0,
                                replay_path=replay_path)
    replay = report["deployed_path_replay"]
    assert replay["cells"][0]["net_per_day_pct"] == 10.0
    assert replay["can_support_the_d036_claim"] is False
    assert "fold" in replay["reason"]
    assert report["answer"]["positive_daily_average_proven"] is False
    assert report["answer"]["per_day_by_path"]["deployed_path_best_pct"] == 10.0
    assert report["answer"]["per_day_by_path"]["research_path_best_pct"] is None


def test_the_answer_reports_each_path_on_its_own_number(tmp_path: Path):
    good = [_fold(i, days=7.0, net=1.0, bench=0.2) for i in range(4)]
    _write(tmp_path, "nested-wf-good.json", _run(good))
    replay_path = _write_replay(tmp_path, {"alw_w1_4h": _replay_row(net_pnl_per_1000=-50.0),
                                           "alw_w3_4h": _replay_row(net_pnl_per_1000=20.0)})
    report = probe.build_report(tmp_path, tmp_path / "missing.json", capital_usdt=1000.0,
                                replay_path=replay_path)
    per_day = report["answer"]["per_day_by_path"]
    assert per_day["research_path_best_pct"] == round(4.0 / 28.0, 6)
    assert per_day["deployed_path_best_pct"] == round(100.0 * 20.0 / 1000.0 / 92.0, 6)
    assert per_day["deployed_path_cells_positive"] == 1 and per_day["deployed_path_cells_measured"] == 2
    assert per_day["paper_book_pct"] is None, "no gate-B artifact means no book number, not a zero"


def test_a_missing_replay_artifact_is_reported_as_missing(tmp_path: Path):
    replay = probe.deployed_path_replay(tmp_path / "absent.json", 1000.0)
    assert replay == {"source": str(tmp_path / "absent.json"), "available": False,
                      "reason": "replay artifact not found"}


def test_the_replay_denominator_is_the_configured_capital_not_the_field_name(tmp_path: Path):
    """The recorded field is named per_1000 because the account was 1000 USDT; if the allocation ever
    changes, the conversion follows the config and the report says so (F-039)."""
    replay = probe.deployed_path_replay(_write_replay(tmp_path, {"cell": _replay_row()}), 2500.0)
    assert replay["capital_basis_usdt"] == 2500.0
    assert replay["capital_warning"].startswith("the replay field is named per_1000")
    assert replay["cells"][0]["net_per_day_pct"] == round(100.0 * 1.9721 / 2500.0 / 92.0, 6)


def test_two_universes_with_the_same_recorded_outcome_are_one_positive_observation(tmp_path: Path):
    """The allowlist replay and the research replay landed on identical trades in the 4h cell, so
    "2 positive cells" would be one observation counted twice (F-042, same trap as the WF labels)."""
    replay = probe.deployed_path_replay(
        _write_replay(tmp_path, {"res_w1_4h": _replay_row(), "alw_w1_4h": _replay_row()}), 1000.0)
    assert replay["cells_positive"] == 2
    assert replay["distinct_positive_outcomes"] == 1
    assert "collapse" in replay["positive_collapse_note"]

    different = _replay_row(net_pnl_per_1000=2.0)
    replay = probe.deployed_path_replay(
        _write_replay(tmp_path, {"res_w1_4h": _replay_row(), "alw_w1_4h": different}), 1000.0)
    assert replay["distinct_positive_outcomes"] == 2, "a different recorded outcome is a second observation"
    assert "positive_collapse_note" not in replay
