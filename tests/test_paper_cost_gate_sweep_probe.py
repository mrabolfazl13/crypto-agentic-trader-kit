"""Locks for the D-043 cost-gate sweep: what a sensitivity over one policy knob may and may not say.

The pre-registered bar is narrow — a ratio counts only when `net_per_day` is positive in EVERY window — so
these tests pin the locked grid (an unplanned ratio is refused before any cell runs), the every-window
conjunction, the refusal to let a grid extremum read like evidence, the anchor reproduction against the
archived D-041 count (the sweep must land on the same four numbers at the in-force ratio or say so), and
the determinism digest. Everything runs offline on fabricated cells: no lake, no network, no config write.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import paper_cost_gate_sweep_probe as sweep  # noqa: E402
from cat.quant.settings import BacktestSettings, QuantSettings  # noqa: E402

FAMILIES = ["ema_trend", "trend_pullback"]
WINDOWS = ["w1", "w3"]


def _cell(*, family: str = "ema_trend", window: str = "w1", ratio: float = 2.0,
          trades_per_day: float = 4.0, aggregate: float = 14.0, closed: int = 410,
          net_per_day: float = -0.116826, excess: float = 4.0924) -> dict:
    return {
        "family": family, "window": window, "path": "research_sim",
        "min_edge_cost_ratio": ratio,
        "aggregate_rate": {"trades_per_day": aggregate},
        "shared_account": {"trades_per_day": trades_per_day, "closed_trades": closed,
                           "net_per_day_pct": net_per_day, "excess_over_benchmark_pp": excess},
        "benchmark": {"net_per_day_pct": -0.161309},
        "one_slot_ceiling": {"ceiling_one_slot_per_day": 1.2, "one_slot_clears_requirement": None},
    }


def _cells(*, accepted_net: float = -0.1, contrast_net: float = -0.06,
           ratios: tuple[float, ...] = sweep.GRID, windows: list[str] = WINDOWS) -> dict:
    out = {}
    for ratio in ratios:
        for window in windows:
            out[sweep.cell_key("ema_trend", window, ratio)] = _cell(
                family="ema_trend", window=window, ratio=ratio, net_per_day=accepted_net)
            out[sweep.cell_key("trend_pullback", window, ratio)] = _cell(
                family="trend_pullback", window=window, ratio=ratio, net_per_day=contrast_net)
    return out


def test_the_key_names_the_family_window_and_ratio():
    assert sweep.cell_key("ema_trend", "w1", 2.0) == "ema_trend·w1·2"


def _settings(**backtest_fields) -> QuantSettings:
    return QuantSettings(root=ROOT, backtest=BacktestSettings(**backtest_fields))


def test_an_unplanned_ratio_is_refused_before_any_cell_runs():
    with pytest.raises(ValueError, match="pre-registered grid"):
        sweep.apply_ratio(_settings(), 2.5)


def test_the_ratio_changes_only_the_cost_gate_on_the_object():
    settings = _settings(min_edge_cost_ratio=2.0, max_hold_hours=24.0, fee_pct=0.1, slippage_pct=0.05)
    changed = sweep.apply_ratio(settings, 0.0)
    assert changed.backtest.min_edge_cost_ratio == 0.0
    assert settings.backtest.min_edge_cost_ratio == 2.0, "the shipped settings object is never mutated"
    for field in ("max_hold_hours", "fee_pct", "slippage_pct"):
        assert getattr(changed.backtest, field) == getattr(settings.backtest, field), field


def test_a_ratio_counts_only_when_every_window_is_positive():
    rows = sweep.ratio_verdicts(
        {sweep.cell_key("ema_trend", "w1", 0.0): _cell(window="w1", ratio=0.0, net_per_day=0.2),
         sweep.cell_key("ema_trend", "w3", 0.0): _cell(window="w3", ratio=0.0, net_per_day=-0.4),
         sweep.cell_key("ema_trend", "w1", 1.0): _cell(window="w1", ratio=1.0, net_per_day=0.1),
         sweep.cell_key("ema_trend", "w3", 1.0): _cell(window="w3", ratio=1.0, net_per_day=0.05)},
        family="ema_trend", windows=WINDOWS, grid=(0.0, 1.0))
    assert [row["positive_in_every_window"] for row in rows["per_ratio"]] == [False, True]
    assert rows["ratios_positive_in_every_window"] == [1.0]
    assert rows["any_ratio_positive_in_every_window"] is True


def test_zero_is_not_positive_and_a_missing_cell_is_an_error_not_a_zero():
    rows = sweep.ratio_verdicts(
        {sweep.cell_key("ema_trend", "w1", 0.0): _cell(window="w1", ratio=0.0, net_per_day=0.0),
         sweep.cell_key("ema_trend", "w3", 0.0): _cell(window="w3", ratio=0.0, net_per_day=0.0)},
        family="ema_trend", windows=WINDOWS, grid=(0.0,))
    assert rows["ratios_positive_in_every_window"] == []
    with pytest.raises(ValueError, match="missing cell"):
        sweep.ratio_verdicts({}, family="ema_trend", windows=WINDOWS, grid=(0.0,))


def test_a_grid_where_nothing_clears_zero_rejects_the_lever_and_keeps_the_negatives():
    verdict = sweep.sweep_verdicts(_cells(), families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["cells_measured"] == 20 and verdict["tests_run"] == 20
    assert verdict["hypothesis_h1_some_ratio_clears_zero_in_every_window"] is False
    assert verdict["cost_gate_lever_rejected_on_this_grid"] is True
    assert "20 cells" in verdict["multiplicity"]


def test_the_lever_is_not_called_rejected_when_the_accepted_family_clears():
    cells = _cells(accepted_net=0.3, contrast_net=-0.06)
    verdict = sweep.sweep_verdicts(cells, families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["hypothesis_h1_some_ratio_clears_zero_in_every_window"] is True
    assert verdict["cost_gate_lever_rejected_on_this_grid"] is False


def _grid_cells(family: str, net_by_ratio_window: dict[tuple[float, str], float]) -> dict:
    """Cells with a per-ratio, per-window net so the ordering of the grid can be steered deliberately."""
    return {sweep.cell_key(family, window, ratio): _cell(family=family, window=window, ratio=ratio,
                                                          net_per_day=net_by_ratio_window[(ratio, window)])
            for ratio in sweep.GRID for window in WINDOWS}


def _flat(family: str, net: float) -> dict:
    return _grid_cells(family, {(ratio, window): net for ratio in sweep.GRID for window in WINDOWS})


def test_a_grid_extremum_is_labelled_as_selection_not_as_evidence():
    verdict = sweep.sweep_verdicts(_cells(), families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    accepted = verdict["per_family"]["ema_trend"]
    assert set(accepted["best_ratios_by_worst_window_net_per_day"]) <= set(sweep.GRID)
    assert "config.yaml" in accepted["selection_on_this_grid"]
    assert "not evidence" in accepted["selection_on_this_grid"]


def test_a_tied_grid_reports_both_extremes_as_sets_instead_of_a_fake_single_ratio():
    # every ratio reproduces the same worst-window number, so min()/max() would name the first row
    rows = sweep.ratio_verdicts(_flat("ema_trend", -0.116826), family="ema_trend",
                                windows=WINDOWS, grid=sweep.GRID)
    assert rows["worst_window_net_per_day_by_ratio"][0]["worst_window_labels"] == ["w1", "w3"]
    assert rows["best_ratios_by_worst_window_net_per_day"] == list(sweep.GRID)
    assert rows["worst_ratios_by_worst_window_net_per_day"] == list(sweep.GRID)
    assert rows["distinct_worst_window_values_on_the_grid"] == 1
    assert rows["grid_orders_the_arms"] is False
    assert rows["ranking_resolves"] is False
    assert "no ratio may be named best or worst" in rows["ranking_note"]
    for gone in ("best_ratio_by_worst_window_net_per_day", "worst_ratio_by_worst_window_net_per_day"):
        assert gone not in rows, "a single best/worst ratio on a tied column is the F-053 defect"


def test_the_worst_window_is_named_where_the_bottleneck_actually_sits():
    rows = sweep.ratio_verdicts(
        {**_grid_cells("ema_trend", {(ratio, "w1"): -0.06 for ratio in sweep.GRID}
                       | {(ratio, "w3"): -0.47 for ratio in sweep.GRID}),
         **_flat("trend_pullback", -0.06)},
        family="ema_trend", windows=WINDOWS, grid=sweep.GRID)
    assert [row["worst_window_labels"] for row in rows["worst_window_net_per_day_by_ratio"]] == [["w3"]] * 5


def test_h2_is_indeterminate_on_a_tied_grid_never_satisfied():
    verdict = sweep.sweep_verdicts(_cells(), families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["hypothesis_h2_the_anchor_is_not_the_worst"] is None
    detail = verdict["hypothesis_h2_detail"]
    assert detail["distinct_worst_window_values"] == 1
    assert "unanswerable" in detail["reason"] and "F-053" in detail["reason"]


def test_h2_is_satisfied_only_when_the_grid_orders_the_arms_and_the_anchor_is_above_the_worst():
    accepted = _grid_cells("ema_trend", {(ratio, "w1"): 0.5 for ratio in sweep.GRID}
                           | {(ratio, "w3"): (-0.9 if ratio == 5.0 else -0.1) for ratio in sweep.GRID})
    verdict = sweep.sweep_verdicts({**accepted, **_flat("trend_pullback", -0.06)},
                                   families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["hypothesis_h2_the_anchor_is_not_the_worst"] is True
    assert verdict["hypothesis_h2_detail"]["worst_ratios"] == [5.0]


def test_h2_is_refuted_when_the_anchor_is_worst_even_when_tied_for_worst():
    uniquely_worst = _grid_cells("ema_trend", {(ratio, "w1"): 0.5 for ratio in sweep.GRID}
                                 | {(ratio, "w3"): (-0.9 if ratio == 2.0 else -0.1) for ratio in sweep.GRID})
    verdict = sweep.sweep_verdicts({**uniquely_worst, **_flat("trend_pullback", -0.06)},
                                   families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["hypothesis_h2_the_anchor_is_not_the_worst"] is False
    assert "in the worst set [2.0]" in verdict["hypothesis_h2_detail"]["reason"]
    assert "tied for worst" not in verdict["hypothesis_h2_detail"]["reason"]

    tied_for_worst = _grid_cells("ema_trend", {(ratio, "w1"): 0.5 for ratio in sweep.GRID}
                                 | {(ratio, "w3"): (-0.9 if ratio in (0.0, 2.0) else -0.1)
                                    for ratio in sweep.GRID})
    tied = sweep.sweep_verdicts({**tied_for_worst, **_flat("trend_pullback", -0.06)},
                                families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert tied["hypothesis_h2_the_anchor_is_not_the_worst"] is False
    assert "tied for worst" in tied["hypothesis_h2_detail"]["reason"]


def test_the_anchor_is_compared_against_the_in_force_ratio_not_a_chosen_one():
    verdict = sweep.sweep_verdicts(_cells(), families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    accepted = verdict["per_family"]["ema_trend"]
    assert accepted["anchor_in_force"] == sweep.ANCHOR_RATIO
    assert accepted["anchor_positive_in_every_window"] is False


def test_the_anchor_reference_is_read_verbatim_and_never_recomputed():
    d041 = {"cells": {"ema_trend·w1": {
        "aggregate_rate": {"trades_per_day": 14.1848},
        "shared_account": {"trades_per_day": 4.4565, "closed_trades": 410,
                           "net_per_day_pct": -0.116826}}}}
    reference = sweep.anchor_reference(d041, grid=sweep.GRID)
    assert reference["recomputed"] is False
    assert reference["cells"]["ema_trend·w1"] == {
        "aggregate_trades_per_day": 14.1848, "shared_trades_per_day": 4.4565,
        "closed_trades": 410, "net_per_day_pct": -0.116826, "min_edge_cost_ratio_in_force": None}


def test_the_anchor_reference_refuses_a_grid_without_the_deployed_ratio():
    with pytest.raises(ValueError, match="not in the grid"):
        sweep.anchor_reference({"cells": {}}, grid=(0.0, 1.0))


def test_a_sweep_cell_that_disagrees_with_the_archived_count_is_reported_field_by_field():
    reference = {"cells": {"ema_trend·w1": {
        "aggregate_trades_per_day": 14.1848, "shared_trades_per_day": 4.4565,
        "closed_trades": 410, "net_per_day_pct": -0.116826}}}
    matching = {"ema_trend·w1·2": _cell(ratio=2.0, aggregate=14.1848, trades_per_day=4.4565,
                                        closed=410, net_per_day=-0.116826)}
    assert sweep.anchor_mismatches(matching, reference, families=FAMILIES, windows=WINDOWS) == []
    drifting = {"ema_trend·w1·2": _cell(ratio=2.0, aggregate=99.0, trades_per_day=4.4565,
                                        closed=410, net_per_day=-0.116826)}
    found = sweep.anchor_mismatches(drifting, reference, families=FAMILIES, windows=WINDOWS)
    assert found == [{"cell": "ema_trend·w1", "field": "aggregate_trades_per_day",
                      "expected": 14.1848, "measured": 99.0}]
    assert sweep.anchor_mismatches({}, reference, families=FAMILIES,
                                   windows=WINDOWS)[0]["field"] == "missing"


def test_the_response_shape_keeps_every_window_visible_per_ratio():
    verdict = sweep.sweep_verdicts(_cells(), families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    rows = sweep.response_shape(verdict, family="ema_trend")
    assert len(rows) == len(sweep.GRID)
    assert rows[0]["min_edge_cost_ratio"] == 0.0
    assert {"w1", "w3"} <= set(rows[0])
    assert all(set(row["trades_per_day"]) == {"w1", "w3"} for row in rows)


def test_the_digest_ignores_the_clock_and_moves_with_a_number():
    a = {sweep.cell_key("ema_trend", "w1", 0.0): dict(_cell(ratio=0.0), elapsed_seconds=1.2)}
    b = {sweep.cell_key("ema_trend", "w1", 0.0): dict(_cell(ratio=0.0), elapsed_seconds=9.9)}
    assert sweep.cells_digest(a) == sweep.cells_digest(b)
    b[sweep.cell_key("ema_trend", "w1", 0.0)]["shared_account"]["net_per_day_pct"] = 0.5
    assert sweep.cells_digest(a) != sweep.cells_digest(b)


def test_the_sweep_never_calls_the_acceptance_gate():
    tree = ast.parse((ROOT / "tools" / "paper_cost_gate_sweep_probe.py").read_text(encoding="utf-8"))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    named = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "acceptance" not in called | named
    modules = {node.module for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module}
    assert "cat.quant.loop" not in modules


def test_the_sweep_writes_no_config_and_only_its_own_artifact():
    source = (ROOT / "tools" / "paper_cost_gate_sweep_probe.py").read_text(encoding="utf-8")
    assert "open(" not in source
    assert source.count("write_text") == 1, "one artifact, no config write"
    assert "config.yaml" in source and "--max-hold-hours" not in source
    assert "--params" not in source


def test_main_refuses_an_off_grid_ratio_without_running_anything(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["paper_cost_gate_sweep_probe.py", "--ratios", "2.0,2.5"])
    assert sweep.main() == 3
    assert "pre-registered grid" in capsys.readouterr().out


def test_main_refuses_a_second_timeframe_or_a_single_family(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["paper_cost_gate_sweep_probe.py", "--timeframe", "1h"])
    assert sweep.main() == 3
    monkeypatch.setattr(sys, "argv", ["paper_cost_gate_sweep_probe.py", "--families", "ema_trend"])
    assert sweep.main() == 3
    assert "contrast family" in capsys.readouterr().out


def test_an_arm_identical_to_the_gate_off_arm_is_reported_as_not_binding():
    cells = _cells()  # every arm carries the same numbers by construction
    binding = sweep.arm_binding(cells, family="ema_trend", windows=WINDOWS, grid=sweep.GRID)
    assert binding["comparable"] is True
    assert binding["ratios_where_the_gate_binds"] == []
    assert binding["first_binding_ratio"] is None
    assert all(arm["identical_to_gate_off_arm"] for arm in binding["arms"])


def test_an_arm_that_rejects_even_one_trade_is_a_binding_arm():
    cells = _cells()
    cells[sweep.cell_key("ema_trend", "w1", 5.0)]["shared_account"]["closed_trades"] = 33
    cells[sweep.cell_key("ema_trend", "w3", 5.0)]["shared_account"]["closed_trades"] = 12
    binding = sweep.arm_binding(cells, family="ema_trend", windows=WINDOWS, grid=sweep.GRID)
    assert binding["ratios_where_the_gate_binds"] == [5.0]
    assert binding["first_binding_ratio"] == 5.0


def test_binding_cannot_be_judged_without_the_gate_off_arm():
    cells = {sweep.cell_key("ema_trend", w, r): _cell(window=w, ratio=r)
             for w in WINDOWS for r in (2.0, 3.0)}
    binding = sweep.arm_binding(cells, family="ema_trend", windows=WINDOWS, grid=(2.0, 3.0))
    assert binding == {"comparable": False,
                       "reason": "the gate-off arm (ratio 0.0) is not in this run, so binding cannot be judged"}


def test_a_grid_that_never_binds_says_so_instead_of_pretending_to_have_measured_the_knob():
    verdict = sweep.sweep_verdicts(_cells(), families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["grid_reaches_the_binding_region"] is False
    assert verdict["binding_note"].startswith("no arm in the registered grid")
    assert "pre-registered step" in verdict["binding_note"]


def test_a_grid_that_binds_leaves_the_note_absent():
    cells = _cells()
    cells[sweep.cell_key("trend_pullback", "w1", 5.0)]["shared_account"]["closed_trades"] = 7
    cells[sweep.cell_key("trend_pullback", "w3", 5.0)]["shared_account"]["closed_trades"] = 3
    verdict = sweep.sweep_verdicts(cells, families=FAMILIES, windows=WINDOWS, grid=sweep.GRID)
    assert verdict["grid_reaches_the_binding_region"] is True
    assert verdict["binding_note"] is None
    assert verdict["arms_where_the_gate_binds"]["trend_pullback"] == [5.0]


def test_the_registered_grid_contains_the_deployed_anchor():
    assert sweep.ANCHOR_RATIO in sweep.GRID
    assert sweep.GRID[0] == 0.0, "the grid must include the gate-off arm or the sweep proves nothing"
    assert len(sweep.GRID) == 5
