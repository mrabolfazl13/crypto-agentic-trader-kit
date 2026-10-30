"""Locks for the D-041 family-rate probe: what a trades-per-day count is allowed to claim.

The question this tool answers is arithmetic — how many closed trades per day does a rule produce on a
named basket and window — so these tests pin the denominator, the slot split that D-025/D-026 blurred,
the tri-state one-slot ceiling that refuses to answer on a thin band, the verbatim-only quoting of the
deployed path, and the determinism guard that turns a nondeterministic cell into a failure.
Everything runs offline on fabricated cells: no lake, no network, no config write.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import paper_family_rate_probe as probe  # noqa: E402


def _cell(*, family: str = "ema_trend", window: str = "w1", days: float = 92.0,
          candidates: int = 40, closed: int = 20, net: float = 1.0, bench: float | None = 0.5,
          aggregate: float | None = None, net_per_day: float | None = None,
          clears: bool | None = None, ceiling: float | None = 0.8) -> dict:
    """A cell in the shape the probe emits, with the fields `verdicts` reads."""
    return {
        "family": family, "window": window, "path": "research_sim", "days": days,
        "aggregate_rate": {"per_symbol_accounts": candidates,
                           "trades_per_day": (round(candidates / days, 4) if aggregate is None
                                              else aggregate)},
        "shared_account": {"closed_trades": closed, "trades_per_day": round(closed / days, 4),
                           "net_return_pct": net,
                           "net_per_day_pct": (round(net / days, 6) if net_per_day is None
                                               else net_per_day)},
        "benchmark": {"return_pct": bench,
                      "net_per_day_pct": (round(bench / days, 6) if bench is not None else None)},
        "one_slot_ceiling": {"ceiling_one_slot_per_day": ceiling,
                             "one_slot_clears_requirement": clears},
    }


def _cells(**overrides: dict) -> dict:
    cells = {f"ema_trend·{w}": _cell(window=w, **overrides.get(w, {})) for w in ("w1", "w3")}
    cells.update({f"trend_pullback·{w}": _cell(family="trend_pullback", window=w,
                                               aggregate=0.2, net_per_day=-0.01,
                                               **overrides.get("tp_" + w, {}))
                  for w in ("w1", "w3")})
    return cells


def test_the_denominator_is_the_counted_window_not_the_data_span():
    assert probe.window_days(probe.epoch("2026-05-01"), probe.epoch("2026-08-01")) == 92.0
    assert probe.window_days(probe.epoch("2026-10-01"), probe.epoch("2026-10-27")) == 26.0


def test_a_zero_or_backwards_window_is_refused_not_divided_by():
    with pytest.raises(ValueError):
        probe.cell_rates(candidates=10, closed=5, net_return_pct=1.0,
                         benchmark_return_pct=1.0, days=0.0)
    assert probe.window_days(probe.epoch("2026-05-02"), probe.epoch("2026-05-01")) < 0


def test_required_is_read_from_the_gate_and_never_assumed():
    assert probe.required_trades_per_day({"cadence": {"required_trades_per_day": 1.1552}},
                                         source="gate.json") == 1.1552
    for payload in ({}, {"cadence": {}}, {"cadence": {"observed_trades_per_day": 0.53}}):
        with pytest.raises(ValueError):
            probe.required_trades_per_day(payload, source="gate.json")


def test_the_slot_split_stays_split():
    """D-025/D-026 mistake: quoting the per-symbol ceiling as if one account could place it."""
    rates = probe.cell_rates(candidates=400, closed=20, net_return_pct=2.0,
                             benchmark_return_pct=1.0, days=100.0)
    assert rates["aggregate_rate"]["trades_per_day"] == 4.0
    assert rates["shared_account"]["trades_per_day"] == 0.2
    assert rates["aggregate_rate"]["per_symbol_accounts"] == 400
    assert rates["shared_account"]["closed_trades"] == 20


def test_net_per_day_and_the_benchmark_share_one_denominator():
    rates = probe.cell_rates(candidates=10, closed=5, net_return_pct=-4.0,
                            benchmark_return_pct=8.0, days=92.0)
    assert rates["shared_account"]["net_per_day_pct"] == round(-4.0 / 92.0, 6)
    assert rates["benchmark"]["net_per_day_pct"] == round(8.0 / 92.0, 6)
    assert rates["benchmark"]["same_denominator_as_strategy"] is True


def test_a_missing_benchmark_is_reported_as_missing():
    rates = probe.cell_rates(candidates=10, closed=5, net_return_pct=1.0,
                            benchmark_return_pct=None, days=10.0)
    assert rates["benchmark"]["return_pct"] is None
    assert rates["benchmark"]["net_per_day_pct"] is None
    assert rates["benchmark"]["same_denominator_as_strategy"] is False


def test_h1_needs_only_one_window_but_records_which():
    cells = _cells(w1={"aggregate": 2.0}, w3={"aggregate": 0.3})
    verdict = probe.verdicts(cells, required=1.1552, accepted_family="ema_trend",
                            contrast_family="trend_pullback")
    assert verdict["accepted_family"]["h1_supported"] is True
    assert verdict["accepted_family"]["h1_windows_clearing_required"] == ["w1"]


def test_h1_is_refuted_when_no_window_reaches_the_gate():
    cells = _cells(w1={"aggregate": 1.15}, w3={"aggregate": 0.4})
    verdict = probe.verdicts(cells, required=1.1552, accepted_family="ema_trend",
                            contrast_family="trend_pullback")
    assert verdict["accepted_family"]["h1_supported"] is False
    assert verdict["h1_rejected_cells"] == 4


def test_h2_indeterminate_is_never_counted_as_a_pass():
    """A band that straddles the requirement means the sample refuses to answer (D-037)."""
    cells = _cells(w1={"clears": None}, w3={"clears": None})
    verdict = probe.verdicts(cells, required=1.1552, accepted_family="ema_trend",
                            contrast_family="trend_pullback")
    assert verdict["accepted_family"]["h2_supported"] is False
    assert verdict["accepted_family"]["h2_indeterminate_windows"] == ["w1", "w3"]


def test_h2_clears_only_with_a_true_verdict():
    cells = _cells(w1={"clears": False}, w3={"clears": True})
    verdict = probe.verdicts(cells, required=1.1552, accepted_family="ema_trend",
                            contrast_family="trend_pullback")
    assert verdict["accepted_family"]["h2_supported"] is True
    assert verdict["accepted_family"]["h2_windows_clearing_required"] == ["w3"]


def test_h3_demands_every_window_positive():
    cells = _cells(w1={"net": 1.0}, w3={"net": -0.5})
    verdict = probe.verdicts(cells, required=1.1552, accepted_family="ema_trend",
                            contrast_family="trend_pullback")
    assert verdict["accepted_family"]["h3_supported"] is False
    assert verdict["h3_negative_cells"] == 3  # w3 plus both contrast cells


def test_every_cell_is_counted_and_the_multiplicity_is_published():
    cells = _cells()
    verdict = probe.verdicts(cells, required=1.1552, accepted_family="ema_trend",
                            contrast_family="trend_pullback")
    assert verdict["cells_measured"] == 4
    assert verdict["tests_run"] == 12  # 2 families x 2 windows x 3 pre-registered measures
    assert verdict["required_trades_per_day"] == 1.1552
    assert verdict["reading"].startswith("this is a count")


def test_the_digest_ignores_the_clock_and_the_order_of_cells():
    cells = _cells()
    reordered = {key: cells[key] for key in sorted(cells, reverse=True)}
    for cell in reordered.values():
        cell["elapsed_seconds"] = 99.9
        cell["errors"] = {"BTC_USDT": "boom"}
    assert probe.cells_digest(cells) == probe.cells_digest(reordered)


def test_the_digest_moves_when_a_number_moves():
    cells = _cells()
    before = probe.cells_digest(cells)
    cells["ema_trend·w1"]["aggregate_rate"]["trades_per_day"] += 0.0001
    assert probe.cells_digest(cells) != before


def test_hold_ceiling_reuses_the_tri_state_estimator():
    trades = [{"symbol": "A", "entry_ts": 0, "exit_ts": 12 * 3600, "reason": "stop"},
              {"symbol": "B", "entry_ts": 0, "exit_ts": 96 * 3600, "reason": "target"}]
    block = probe.hold_ceiling(closed_trades=trades, required=1.1552,
                              window_end_iso="2026-08-01T00:00:00+00:00")
    assert block["band_straddles_requirement"] is True
    assert block["one_slot_clears_requirement"] is None
    assert block["hold_source"]["closed_trades"] == 2
    assert block["hold_source"]["censored"] is False


def test_hold_ceiling_with_no_closed_trade_declines_to_answer():
    block = probe.hold_ceiling(closed_trades=[], required=1.1552,
                              window_end_iso="2026-08-01T00:00:00+00:00")
    assert block["ceiling_one_slot_per_day"] is None
    assert block["one_slot_clears_requirement"] is None
    assert block["hold_source"]["kind"] == "no_closed_trade"


def test_deployed_reference_copies_without_recomputing(tmp_path, monkeypatch):
    breadth = tmp_path / "state/quant/results/allowlist_breadth/alw_w1_4h"
    breadth.mkdir(parents=True)
    (breadth / "frequency-4h.json").write_text(json.dumps(
        {"summary": {"universe_digest": "deadbeefdeadbeef"}}), encoding="utf-8")
    ceiling = tmp_path / "deployed_ceiling.json"
    ceiling.write_text(json.dumps({
        "required_per_day": 1.1781, "max_hold_days_to_clear_with_one_slot": 0.8488,
        "h1": {"confirmed": True}, "sim_path_contrast": {"ceiling_per_day": 1.4891},
        "cells": {"w1_4h": {"timeframe": "4h", "pooled_trades_per_day_upper_bound": 0.4565,
                            "source": "state/quant/results/allowlist_breadth/alw_w1_4h/frequency-4h.json"},
                  "w1_1h": {"timeframe": "1h", "source": "irrelevant"}}}), encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    block = probe.deployed_reference(ceiling_file=ceiling, universe_digest="deadbeefdeadbeef")
    assert block["recomputed"] is False
    assert block["required_per_day"] == 1.1781
    assert list(block["cells_4h"]) == ["w1_4h"]
    assert block["cells_4h"]["w1_4h"]["pooled_trades_per_day_upper_bound"] == 0.4565


def test_deployed_reference_refuses_a_different_universe(tmp_path, monkeypatch):
    breadth = tmp_path / "state/quant/results/allowlist_breadth/alw_w1_4h"
    breadth.mkdir(parents=True)
    (breadth / "frequency-4h.json").write_text(json.dumps({"summary": {"universe_digest": "other"}}),
                                               encoding="utf-8")
    ceiling = tmp_path / "deployed_ceiling.json"
    ceiling.write_text(json.dumps({"cells": {"w1_4h": {
        "timeframe": "4h",
        "source": "state/quant/results/allowlist_breadth/alw_w1_4h/frequency-4h.json"}}}),
        encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        probe.deployed_reference(ceiling_file=ceiling, universe_digest="deadbeefdeadbeef")


def test_accepted_reference_keeps_the_gap_visible(tmp_path, monkeypatch):
    run = tmp_path / "nested-wf-ema_trend-4h-train_breadth.json"
    run.write_text(json.dumps({"run_id": "nested-wf-ema_trend-4h-train_breadth",
                              "decision": {"decision": "accepted"},
                              "aggregate": {"trades": 175, "net_return_pct_sum": 3.8087},
                              "folds": [{"fold": 1, "best_params": {"ema_fast": 9}},
                                        {"fold": 2, "best_params": {"ema_fast": 19}}]}),
                              encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    block = probe.accepted_verdict_reference(run_file=run, measured_params_label="ema12/48",
                                            measured_symbols=69)
    assert block["recomputed"] is False
    assert block["decision"] == "accepted"
    assert len(block["per_fold_params"]) == 2
    assert block["measured_here"]["params_label"] == "ema12/48"
    assert len(block["divergence"]) == 3


def test_excess_is_signed_against_the_benchmark_not_against_zero():
    """A losing cell can still beat a falling market; the artifact has to say both."""
    rates = probe.cell_rates(candidates=10, closed=5, net_return_pct=-10.748,
                            benchmark_return_pct=-14.8404, days=92.0)
    assert rates["shared_account"]["excess_over_benchmark_pp"] == 4.0924
    assert rates["shared_account"]["beats_benchmark_while_losing"] is True


def test_a_profitable_cell_is_never_flagged_as_beating_while_losing():
    rates = probe.cell_rates(candidates=10, closed=5, net_return_pct=2.0,
                            benchmark_return_pct=1.0, days=10.0)
    assert rates["shared_account"]["excess_over_benchmark_pp"] == 1.0
    assert rates["shared_account"]["beats_benchmark_while_losing"] is False


def test_excess_stays_unknown_when_the_benchmark_is():
    rates = probe.cell_rates(candidates=10, closed=5, net_return_pct=1.0,
                            benchmark_return_pct=None, days=10.0)
    assert rates["shared_account"]["excess_over_benchmark_pp"] is None
    assert rates["shared_account"]["beats_benchmark_while_losing"] is False


def test_path_divergence_quotes_the_deployed_rate_without_recounting_it():
    cells = {"trend_pullback·w1": _cell(family="trend_pullback", window="w1", closed=305,
                                        days=92.0),
             "ema_trend·w1": _cell(window="w1", closed=410, days=92.0)}
    deployed = {"cells_4h": {"w1_4h": {"measured_trades_per_day_serial": 0.1848,
                                       "pooled_trades_per_day_upper_bound": 0.4565}}}
    block = probe.path_divergence(cells=cells, deployed=deployed, rule="trend_pullback")
    assert [row["window"] for row in block["cells"]] == ["w1"]  # only the shared rule is compared
    row = block["cells"][0]
    assert row["research_shared_account_per_day"] == 3.3152
    assert row["deployed_serial_per_day"] == 0.1848
    assert row["research_over_deployed_serial"] == round(3.3152 / 0.1848, 2)
    assert block["deployed_numbers_read_verbatim"] is True


def test_a_deployed_cell_that_is_not_there_yields_no_ratio():
    cells = {"trend_pullback·w3": _cell(family="trend_pullback", window="w3", closed=130, days=26.0)}
    block = probe.path_divergence(cells=cells, deployed={"cells_4h": {}}, rule="trend_pullback")
    assert block["cells"][0]["deployed_serial_per_day"] is None
    assert block["cells"][0]["research_over_deployed_serial"] is None


def test_the_probe_never_calls_the_acceptance_gate():
    """Counting is not deciding: the shipped rule stays the only thing that can say `accepted`.

    Checked on the syntax tree, because the module docstring is allowed to name the gate it refuses
    to touch.
    """
    tree = ast.parse((ROOT / "tools" / "paper_family_rate_probe.py").read_text(encoding="utf-8"))
    called = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.id if isinstance(func, ast.Name) else
                       getattr(func, "attr", ""))
        elif isinstance(node, (ast.ImportFrom, ast.Import)):
            imported.update(alias.name for alias in node.names)
    assert "acceptance" not in called
    assert "cat.quant.loop" not in {node.module for node in ast.walk(tree)
                                    if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(name.startswith("cat.quant.loop") or name == "acceptance" for name in imported)


def test_the_research_cap_and_config_are_out_of_reach():
    """The 24h cap carries the accepted verdict's structure (D-034) and config.yaml is not a lever."""
    source = (ROOT / "tools" / "paper_family_rate_probe.py").read_text(encoding="utf-8")
    assert "--max-hold-hours" not in source
    assert "--params" not in source
    assert "open(" not in source
    assert source.count("write_text") == 1  # the only file written is this artifact
    assert "config.yaml" in source  # named as the settings it reads and never writes
