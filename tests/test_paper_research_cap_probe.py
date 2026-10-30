"""Tests for the D-034 research-side hold-cap probe and the flag it reads.

The probe is arithmetic over artifacts, so every test builds its own artifacts in
``tmp_path``: nothing here depends on the state lake being present.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import paper_research_cap_probe as probe  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.quant.engine import simulate_settings  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402

TOOL = ROOT / "tools" / "paper_research_cap_probe.py"
WF_TOOL = ROOT / "tools" / "quant_nested_walk_forward.py"


def _artifact(*, cap: float | None, overridden: bool, net: float, decision: str = "accepted",
              trades: int = 40, data_version: str = "abc123",
              code_version: str = "deadbeef") -> dict:
    protocol = {"trials_per_fold": 6, "finalists": 3, "inner_selection_fraction": 0.2,
                "search_symbols": 30, "scored_symbols": 100, "folds": 4, "test_days": 7,
                "regime_symbol": "BTCUSDT", "regime_ema": 200,
                "symbol_selection": "train_breadth", "symbol_top_k": 25,
                "symbol_min_candidates": 1}
    if cap is not None:
        protocol["max_hold_hours"] = cap
        protocol["max_hold_hours_configured"] = 24.0
        protocol["max_hold_hours_overridden"] = overridden
    gates = {"min_trades": {"required": 30, "actual": trades, "pass": trades >= 30},
             "profit_factor": {"required": 1.1, "actual": 1.25, "pass": True},
             "max_drawdown_pct": {"required": 10.0, "actual": 4.4, "pass": True},
             "positive_folds_pct": {"required": 60.0, "actual": 75.0, "pass": True}}
    doc = {
        "run_id": f"run-cap-{cap}", "code_version": code_version, "data_version": data_version,
        "protocol": protocol,
        "window": {"start": "2026-03-01T00:00:00+00:00", "end": "2026-04-21T18:00:00+00:00"},
        "final_test_start": "2026-04-21T18:00:00+00:00", "final_test_read": False,
        "folds": [{"fold": i, "train": {}, "search": {}, "selection": {}, "test": {},
                   "symbols_evaluated": 100, "search_basket": 30, "scored_basket": 100,
                   "trials": 6, "stop_reason": "trial budget",
                   "max_hold_hours": cap, "metrics": {"avg_hold_hours": 9.0}}
                  for i in range(4)],
        "aggregate": {"folds": 4, "positive_fold_pct": 75.0, "net_return_pct_sum": net,
                      "trades": trades, "profit_factor_mean": 1.25, "max_drawdown_pct": 4.4,
                      "max_effective_drawdown_pct": 4.4, "benchmark_return_pct_sum": 1.5},
        "decision": {"decision": decision, "gates": gates},
    }
    if cap is not None:
        # Artifacts written before the flag exists carry no cap key anywhere, which is
        # exactly what the H3 census has to distinguish a recorded cap from an inferred one.
        doc["max_hold_hours"] = cap
    return doc


def _write(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_guard_accepts_a_pair_that_differs_only_in_the_cap():
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0),
                                 _artifact(cap=720.0, overridden=True, net=3.4))
    assert guard["ok"], guard["problems"]
    assert guard["cap_control"] == 24.0 and guard["cap_treatment"] == 720.0


def test_guard_rejects_a_different_data_version():
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0),
                                 _artifact(cap=720.0, overridden=True, net=3.4,
                                           data_version="other"))
    assert not guard["ok"]
    assert any("data_version" in p for p in guard["problems"])


def test_guard_rejects_a_build_drift():
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0),
                                 _artifact(cap=720.0, overridden=True, net=3.4,
                                           code_version="fadedbeef"))
    assert not guard["ok"]
    assert any("code_version" in p for p in guard["problems"])


def test_guard_rejects_a_protocol_field_that_is_not_the_cap():
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    treatment["protocol"]["trials_per_fold"] = 12
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0), treatment)
    assert not guard["ok"]
    assert any("trials_per_fold" in p for p in guard["problems"])


def test_guard_rejects_two_identical_caps():
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0),
                                 _artifact(cap=24.0, overridden=False, net=3.4))
    assert not guard["ok"]
    assert any("not a cap contrast" in p for p in guard["problems"])


def test_guard_refuses_an_artifact_that_records_no_cap():
    """Old runs cannot be paired: their cap is inference, not a recorded field."""
    guard = probe.protocol_guard(_artifact(cap=None, overridden=False, net=3.0),
                                 _artifact(cap=720.0, overridden=True, net=3.4))
    assert not guard["ok"]
    assert any("cannot be attributed to a cap" in p for p in guard["problems"])


def test_guard_rejects_a_fold_that_does_not_echo_the_run_cap():
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    treatment["folds"][2]["max_hold_hours"] = 24.0
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0), treatment)
    assert not guard["ok"]
    assert any("fold does not record the run-level cap" in p for p in guard["problems"])


def test_guard_tolerates_the_outcomes_the_cap_is_expected_to_move():
    """A different cap changes trial counts, early stops and timings; that is the effect."""
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    for fold in treatment["folds"]:
        fold["trials"] = 4
        fold["search_elapsed_seconds"] = 9.9
        fold["stop_reason"] = "three consecutive trials without enough trades"
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0), treatment)
    assert guard["ok"], guard["problems"]
    moved = guard["outcome_moves"]["trials"][0]
    assert moved["fold"] == 0 and moved["control"] == 6 and moved["treatment"] == 4


def test_guard_summarises_a_real_trial_list_to_its_count():
    """Run artifacts store the whole trial record list; the report must carry the count and
    the stop reason, not two copies of every trial."""
    control = _artifact(cap=24.0, overridden=False, net=3.0)
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    for doc, stop in ((control, "trial budget reached"),
                      (treatment, "three consecutive trials without enough trades")):
        for fold in doc["folds"]:
            fold["trials"] = [{"number": n, "score": -100.0} for n in range(6)]
            fold["stop_reason"] = stop
    guard = probe.protocol_guard(control, treatment)
    assert guard["outcome_moves"]["trials"][0] == {
        "fold": 0, "control": 6, "treatment": 6,
        "stop_control": "trial budget reached",
        "stop_treatment": "three consecutive trials without enough trades"}
    cell = probe.compare_cell("c", control, treatment, materiality_pct=20.0)
    assert cell["search_channel"]["folds"][0]["trials_control"] == 6


def test_guard_still_rejects_a_fold_whose_input_window_moved():
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    treatment["folds"][1]["test"] = {"start": "2026-04-01T00:00:00+00:00"}
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0), treatment)
    assert not guard["ok"]
    assert any("input field test differs" in p for p in guard["problems"])


def test_guard_rejects_a_moved_field_that_is_neither_input_nor_declared_outcome():
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    treatment["folds"][0]["newly_added_field"] = 1
    guard = probe.protocol_guard(_artifact(cap=24.0, overridden=False, net=3.0), treatment)
    assert not guard["ok"]
    assert any("neither inputs nor declared outcomes" in p for p in guard["problems"])


def test_compare_cell_records_the_mediated_search_channel():
    treatment = _artifact(cap=720.0, overridden=True, net=3.4)
    treatment["folds"][0]["params_label"] = "ema_trend:v9@deadbeef"
    cell = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=3.0), treatment,
                              materiality_pct=20.0)
    assert cell["search_channel"]["params_changed_folds"] == 1
    assert cell["search_channel"]["folds"][0]["params_changed"] is True


def test_h1_a_flipped_verdict_is_load_bearing():
    cell = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=3.0),
                              _artifact(cap=720.0, overridden=True, net=3.1, decision="rejected"),
                              materiality_pct=20.0)
    assert cell["verdict_changed"] is True
    assert cell["load_bearing"] is True


def test_h1_materiality_uses_the_relative_change_of_the_control():
    material = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=5.0),
                                  _artifact(cap=720.0, overridden=True, net=7.0),
                                  materiality_pct=20.0)
    assert material["net_return_pct_relative_change"] == 40.0
    assert material["material_by_20pct_rule"] is True
    immaterial = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=5.0),
                                    _artifact(cap=720.0, overridden=True, net=5.5),
                                    materiality_pct=20.0)
    assert immaterial["material_by_20pct_rule"] is False
    assert immaterial["load_bearing"] is False


def test_h1_a_sign_flip_is_material():
    cell = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=5.0),
                              _artifact(cap=720.0, overridden=True, net=-0.1),
                              materiality_pct=20.0)
    assert cell["material_by_20pct_rule"] is True
    assert cell["load_bearing"] is True


def test_h1_a_zero_control_cannot_scale_but_a_nonzero_treatment_is_material():
    cell = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=0.0),
                              _artifact(cap=720.0, overridden=True, net=1.2),
                              materiality_pct=20.0)
    assert cell["net_return_pct_relative_change"] is None
    assert cell["material_by_20pct_rule"] is True


def test_h1_a_zero_pair_is_indeterminate():
    cell = probe.compare_cell("c", _artifact(cap=24.0, overridden=False, net=0.0),
                              _artifact(cap=720.0, overridden=True, net=0.0),
                              materiality_pct=20.0)
    assert cell["material_by_20pct_rule"] is None
    assert cell["load_bearing"] is False


def test_two_labels_that_score_identically_are_one_computation():
    """The breadth ranker can fall back to the pre-registered universe basket, and then two
    protocol labels are the same run; counting them twice would double the sample size."""
    cells = [probe.compare_cell(name, _artifact(cap=24.0, overridden=False, net=3.0),
                                 _artifact(cap=720.0, overridden=True, net=3.4),
                                 materiality_pct=20.0)
             for name in ("train_breadth", "universe")]
    distinct = probe.distinct_cells(cells)
    assert distinct["pairs"] == 2
    assert distinct["measured_pairs"] == 2
    assert distinct["distinct_computations"] == 1
    assert distinct["duplicate_groups"] == [{"cells": ["train_breadth", "universe"]}]
    apart = probe.distinct_cells([cells[0], probe.compare_cell(
        "other", _artifact(cap=24.0, overridden=False, net=9.0),
        _artifact(cap=720.0, overridden=True, net=3.4), materiality_pct=20.0)])
    assert apart["distinct_computations"] == 2
    assert apart["duplicate_groups"] == []


def _dump(*, cap: float, spans_hours: list[float]) -> dict:
    trades = [{"symbol": "XUSDT", "reason": "time" if abs(h - cap) < 1.0 else "stop",
               "entry_ts": 1_700_000_000, "exit_ts": 1_700_000_000 + int(h * 3600)}
              for h in spans_hours]
    return {"run_id": f"dump-{cap}", "max_hold_hours": cap, "folds": {"0": trades}}


def test_h2_census_counts_time_exits_and_the_censored_share():
    census = probe.hold_census([_dump(cap=24.0, spans_hours=[24.0, 24.0, 4.0, 10.0])])
    assert census["trades"] == 4
    assert census["time_exit_share_pct"] == 50.0
    assert census["at_declared_cap_share_pct"] == 50.0
    assert census["share_over_24h_pct"] == 0.0


def test_h2_the_treatment_release_allows_holds_past_the_control_cap():
    census = probe.hold_census([_dump(cap=720.0, spans_hours=[24.0, 60.0, 100.0, 2.0])])
    assert census["share_over_24h_pct"] == 50.0
    assert census["mean_hold_hours"] > 24.0


def test_h2_an_empty_dump_is_reported_as_not_measured():
    census = probe.hold_census([])
    assert census["trades"] == 0
    assert "NOT MEASURED" in census["note"]


def test_h3_census_separates_recorded_caps_from_inferred_ones(tmp_path: Path):
    root = tmp_path / "state"
    run_dir = root / "nested_walk_forward_test"
    run_dir.mkdir(parents=True)
    _write(run_dir / "nested-wf-old.json", _artifact(cap=None, overridden=False, net=3.0))
    _write(run_dir / "nested-wf-new.json", _artifact(cap=720.0, overridden=True, net=3.4))
    (run_dir / "nested-wf-new.partial.json").write_text(json.dumps({"run_id": "p", "folds": []}),
                                                         encoding="utf-8")
    census = probe.cap_label_census(root)
    assert census["run_files"] == 2
    assert census["partial_files"] == 1
    assert census["runs_recording_cap"] == 1
    assert census["runs_without_cap_field"] == 1
    assert census["runs_with_cap_in_filename"] == 0
    assert census["accepted_runs"] == 2
    assert census["patterns"] == ("**/nested-wf-*.json",)


def test_report_conclusion_is_blocked_when_a_guard_fails(tmp_path: Path):
    control = _write(tmp_path / "c.json", _artifact(cap=24.0, overridden=False, net=3.0))
    treatment = _write(tmp_path / "t.json", _artifact(cap=720.0, overridden=True, net=3.4,
                                                      data_version="other"))
    out = tmp_path / "report.json"
    result = subprocess.run([sys.executable, str(TOOL), "--cell",
                             f"cell|{control}|{treatment}", "--d033", str(tmp_path / "absent.json"),
                             "--results-root", str(tmp_path), "--out", str(out)],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 3, result.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["all_guards_pass"] is False
    assert report["conclusion"] == "not measured"
    assert report["h2"]["control_census"]["note"] == "not measured"
    assert report["read_only"] is True


def test_report_passes_and_measures_every_hypothesis(tmp_path: Path):
    control = _write(tmp_path / "c.json", _artifact(cap=24.0, overridden=False, net=5.0))
    treatment = _write(tmp_path / "t.json", _artifact(cap=720.0, overridden=True, net=7.0))
    _write(tmp_path / "c.trades.json", _dump(cap=24.0, spans_hours=[24.0, 24.0, 6.0]))
    _write(tmp_path / "t.trades.json", _dump(cap=720.0, spans_hours=[24.0, 90.0, 6.0]))
    out = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(TOOL), "--cell", f"cell|{control}|{treatment}",
         "--trades-control", str(tmp_path / "c.trades.json"),
         "--trades-treatment", str(tmp_path / "t.trades.json"),
         "--d033", str(tmp_path / "absent.json"), "--results-root", str(tmp_path),
         "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["all_guards_pass"] is True
    assert report["h1"]["material_cells"] == ["cell"]
    assert report["h2"]["cap_binds_in_control"] is True
    assert report["conclusion"] == "the cap is load-bearing for the research verdict"


def test_report_says_when_both_cells_are_the_same_computation(tmp_path: Path):
    control = _write(tmp_path / "c.json", _artifact(cap=24.0, overridden=False, net=5.0))
    treatment = _write(tmp_path / "t.json", _artifact(cap=720.0, overridden=True, net=7.0))
    out = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(TOOL),
         "--cell", f"breadth|{control}|{treatment}",
         "--cell", f"universe|{control}|{treatment}",
         "--d033", str(tmp_path / "absent.json"), "--results-root", str(tmp_path),
         "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["h1"]["material_cells"] == ["breadth", "universe"]
    assert report["h1"]["distinct"]["distinct_computations"] == 1
    assert "1 distinct computation" in report["conclusion"]


def test_configured_cap_is_still_twenty_four_hours():
    """The flag must not have moved the default the deployed research protocol runs under."""
    settings = load_quant_settings(load_config(root=ROOT))
    assert settings.backtest.max_hold_hours == 24


def test_override_reaches_the_simulator_and_the_search_also_honours_it():
    import inspect
    from cat.quant.optimizer import optimize_windows
    settings = load_quant_settings(load_config(root=ROOT))
    assert simulate_settings(settings).max_hold_hours == 24.0
    assert simulate_settings(settings, max_hold_hours=720.0).max_hold_hours == 720.0
    # Without this parameter the inner search would keep the 24h cap while only the
    # scored leg moved, and the artifact would claim a cap the search never used.
    assert "sim" in inspect.signature(optimize_windows).parameters


def test_hold_cap_is_a_wall_clock_hours_ceiling_on_one_minute_bars():
    source = (ROOT / "src" / "cat" / "quant" / "sim.py").read_text(encoding="utf-8")
    assert "max_hold_bars = max(1, int(round(settings.max_hold_hours * 60)))" in source
    assert "last = min(start + max_hold_bars - 1, len(timestamps) - 1)" in source


def test_walk_forward_rejects_a_cap_outside_the_config_envelope():
    for value in ("5000", "0.05"):
        result = subprocess.run([sys.executable, str(WF_TOOL), "--max-hold-hours", value],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        assert result.returncode != 0
        assert "0.1..720" in (result.stderr + result.stdout)


def test_walk_forward_names_the_treatment_run_by_its_cap():
    """The run_id carries the cap so a treatment artifact can never overwrite the control."""
    source = (WF_TOOL).read_text(encoding="utf-8")
    assert 'run_id += f"-hold{sim.max_hold_hours:g}h"' in source
    assert '"max_hold_hours": sim.max_hold_hours,' in source
