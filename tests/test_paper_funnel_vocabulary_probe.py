"""Locks for the D-035 funnel-vocabulary probe: what may be called "executed", and what may not move.

The funnel is the daily analysis layer, and it used to name every non-``NO_TRADE`` ledger row
``executed:<action>`` while ignoring the ``approved`` column the same store writes.  These tests pin
the two halves of the fix: the buckets must agree with the order book, and the re-bucketing must not
move a single published D-027/D-028 number.  That second half is the point — a "correction" that
silently re-labels a *decision* is the F-030 family again.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from cat.paper_evidence import _decision_reason  # noqa: E402
from paper_decisions_unarchived import classify  # noqa: E402
from paper_funnel_vocabulary_probe import (  # noqa: E402
    SIZE_BLOCK,
    blocked_signal_census,
    cut_to_generation,
    cap_headroom,
    classify_lock,
    freeze_book,
    funnel_agrees_with_book,
    ledger_crosscheck,
    legacy_reason,
    main,
    only_book_columns_moved,
    recorded_funnel_from,
    vocabulary_comparison,
)

ARTIFACT = ROOT / "state" / "quant" / "results" / "d035_funnel_vocabulary" / "funnel_vocabulary.json"


def _row(ts: str, symbol: str, action: str, *, approved=None, rationale="", order_id=None,
         decision_id="dec-1") -> dict:
    row: dict = {"id": decision_id, "ts": ts, "symbol": symbol, "action": action,
                 "rationale_json": json.dumps([rationale] if rationale else []), "mode": "PAPER"}
    if approved is not None:
        row["approved"] = approved
    if order_id is not None:
        row["extras_json"] = json.dumps({"order_id": order_id})
    return row


def _order(decision_id: str, symbol: str, side: str, *, created_ts: str, qty=0.066, price=753.1) -> dict:
    return {"decision_id": decision_id, "symbol": symbol, "side": side, "qty": qty, "price": price,
            "status": "FILLED", "created_ts": created_ts}


def test_the_legacy_estimator_called_every_proposal_an_execution():
    """The bug reproduced, not just the fix: six proposals became six "executed:buy"."""
    blocked = _row("2026-10-28T18:37:53+00:00", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK)
    assert legacy_reason(blocked) == "executed:buy"
    assert _decision_reason(blocked) == "blocked_after_signal:other"


def test_a_row_without_an_approval_column_is_never_reported_as_a_rejection():
    """Replay and test rows have no ``approved`` column; saying "blocked" there would be a lie too."""
    blind = _row("2026-10-28T18:37:53+00:00", "BNB_USDT", "BUY", rationale=SIZE_BLOCK)
    assert "approved" not in blind
    assert legacy_reason(blind) == "executed:buy"
    assert _decision_reason(blind) == "unverified_action:buy"
    assert classify(_decision_reason(blind)) == "cleared_regime_gate"


def test_blocked_after_signal_still_counts_as_having_cleared_the_regime_gate():
    """If this bucket were dropped, five published D-027 "opportunity" rows would vanish."""
    assert classify("blocked_after_signal:other") == "cleared_regime_gate"
    assert classify("executed:buy") == "cleared_regime_gate"
    assert classify("regime_filter") == "blocked_by_regime"
    assert classify("no_atr") == "not_evaluable"


def test_vocabulary_comparison_reports_the_exact_over_count():
    rows = [
        _row("t1", "ASTER_USDT", "BUY", approved=1, rationale="regime=TRENDING_UP", decision_id="d1"),
        _row("t2", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK, decision_id="d2"),
        _row("t3", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK, decision_id="d3"),
        _row("t4", "ASTER_USDT", "SELL", approved=1, rationale="exit_stop", decision_id="d4"),
        _row("t5", "SOL_USDT", "NO_TRADE", rationale="regime_filter"),
    ]
    got = vocabulary_comparison(rows)
    assert got["legacy_executed_counts"] == {"executed:buy": 3, "executed:sell": 1}
    assert got["new_executed_counts"] == {"executed:buy": 1, "executed:sell": 1}
    assert got["blocked_after_signal_counts"] == {"blocked_after_signal:other": 2}
    assert got["over_count_by_bucket"] == {"executed:buy": 2, "executed:sell": 0}
    assert got["legacy_overstates_execution"] is True
    assert got["signal_rows_total"] == 4


def test_ledger_crosscheck_accepts_a_book_that_matches_and_refuses_one_that_does_not():
    rows = [_row("t1", "ASTER_USDT", "BUY", approved=1, rationale="x", decision_id="d1",
                 order_id="paper-1"),
            _row("t2", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK, decision_id="d2")]
    orders = [_order("d1", "ASTER_USDT", "BUY", created_ts="t1", qty=70.2, price=0.7115)]
    ok = ledger_crosscheck(rows, orders)
    assert ok["book_and_ledger_agree"] is True
    assert ok["approved_signal_without_order"] == []
    assert ok["blocked_signal_with_order"] == []
    assert ok["approved_rows_by_side"] == {"buy": 1}
    assert ok["orders_by_side"] == {"buy": 1}

    ghost = ledger_crosscheck([_row("t1", "ASTER_USDT", "BUY", approved=1, rationale="x",
                                    decision_id="d1")], [])
    assert ghost["book_and_ledger_agree"] is False
    assert ghost["approved_signal_without_order"] == ["d1"]

    orphan = ledger_crosscheck(rows, [_order("d2", "BNB_USDT", "BUY", created_ts="t2")])
    assert orphan["book_and_ledger_agree"] is False
    assert "d2" in orphan["order_without_approved_signal"]


def test_blocked_census_keeps_the_rationale_verbatim():
    rows = [_row("t1", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK),
            _row("t2", "BNB_USDT", "BUY", approved=0, rationale="cooldown after loss, 3600s remaining")]
    got = blocked_signal_census(rows + [_row("t3", "SOL_USDT", "NO_TRADE", rationale="regime_filter")])
    assert got["rows"] == 2
    assert got["by_rationale_counts"] == {SIZE_BLOCK: 1, "cooldown after loss, 3600s remaining": 1}
    assert got["by_symbol_counts"] == {"BNB_USDT": 2}
    assert [d["ts"] for d in got["detail"]] == ["t1", "t2"]


def test_cap_headroom_proves_the_rejection_is_arithmetic_not_price():
    rows = [_row("2026-10-28T18:37:53+00:00", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK,
                  decision_id="d2")]
    snapshots = [{"ts": "2026-10-28T18:37:53+00:00", "equity": 1000.0, "exposure_pct": 4.97,
                  "positions_json": json.dumps([{"symbol": "ASTER_USDT"}])}]
    orders = [_order("d9", "BNB_USDT", "BUY", created_ts="2026-10-29T06:41:02+00:00", qty=0.066,
                     price=753.1)]
    got = cap_headroom(rows, snapshots, allocation_cap=50.0, orders=orders)
    row = got["rows"][0]
    assert row["exposure_value"] == 49.7
    assert row["headroom_usdt"] == 0.3
    assert row["qty_step_seen_in_book"] == 0.001
    assert row["one_lot_notional_estimate"] == 0.7531
    assert row["headroom_below_one_lot"] is True
    assert got["every_size_block_is_below_one_lot"] is True
    assert got["serial_refill"]["per_symbol"][0]["symbol"] == "BNB_USDT"
    assert got["serial_refill"]["per_symbol"][0]["approved_later"] is None


def test_probe_refuses_a_store_that_cannot_be_cross_checked(tmp_path):
    db = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE decisions (ts TEXT, symbol TEXT, action TEXT, rationale_json TEXT, "
                       "extras_json TEXT, mode TEXT)")
    connection.execute("INSERT INTO decisions VALUES ('t1', 'BNB_USDT', 'BUY', "
                       "'[\"computed position size rounds to zero\"]', '{}', 'PAPER')")
    connection.execute("CREATE TABLE orders (symbol TEXT)")
    connection.execute("CREATE TABLE trades (symbol TEXT)")
    connection.execute("CREATE TABLE portfolio_snapshots (ts TEXT, equity REAL, exposure_pct REAL, "
                       "positions_json TEXT)")
    connection.commit()
    connection.close()
    assert main(["--db", str(db), "--out", str(tmp_path / "out.json")]) == 3


def test_recorded_funnel_reads_both_file_shapes():
    """The immutable witness wraps the funnel; a live gate-B report may be the funnel itself."""
    funnel = {"decisions": 2801, "executed": {"executed:buy": 6}}
    assert recorded_funnel_from({"decision_funnel": funnel}) == funnel
    assert recorded_funnel_from(funnel) == funnel


def test_a_store_without_approvals_does_not_fail_the_book_comparison_by_absence():
    """Empty executed buckets plus an empty book agree; the lock is the count, not the vocabulary."""
    book = {"approved_rows_by_side": {}, "approved_signal_rows": 0}
    assert funnel_agrees_with_book({}, book)["agree"] is True
    assert funnel_agrees_with_book({"executed:buy": 2}, book)["agree"] is False
    matched = funnel_agrees_with_book({"executed:buy": 1},
                                      {"approved_rows_by_side": {"buy": 1}, "approved_signal_rows": 1})
    assert matched["agree"] is True


def test_cap_headroom_survives_a_missing_cap_or_a_missing_snapshot():
    rows = [_row("t1", "BNB_USDT", "BUY", approved=0, rationale=SIZE_BLOCK)]
    assert cap_headroom(rows, [], allocation_cap=None)["rows"][0]["headroom_usdt"] is None
    assert cap_headroom(rows, [], allocation_cap=50.0)["rows"][0]["exposure_value"] is None
    assert cap_headroom([], [], allocation_cap=50.0)["every_size_block_is_below_one_lot"] is False


def test_freeze_book_only_keeps_rows_inside_the_recorded_window():
    window = ["2026-10-26T06:27:57+00:00", "2026-10-28T06:35:59+00:00"]
    orders = [_order("d1", "ASTER_USDT", "BUY", created_ts="2026-10-27T18:33:25+00:00"),
              _order("d2", "BNB_USDT", "BUY", created_ts="2026-10-29T06:41:02+00:00")]
    trades = [{"symbol": "ASTER_USDT", "entry_ts": 1, "exit_ts": 1793245217}]
    kept_orders, kept_trades = freeze_book(orders, trades, window)
    assert [o["decision_id"] for o in kept_orders] == ["d1"]
    assert kept_trades == []


def test_growth_report_separates_new_facts_from_a_changed_estimator():
    recorded = {"groups": {"archived": {"orders": 1, "closed_trades": 0, "symbol_cycles": 1173}}}
    moved_book = {"groups": {"archived": {"orders": 3, "closed_trades": 1, "symbol_cycles": 1173}}}
    assert only_book_columns_moved(recorded, moved_book)["explained_by_ledger_growth_alone"] is True

    moved_counts = {"groups": {"archived": {"orders": 1, "closed_trades": 0, "symbol_cycles": 1174}}}
    other = only_book_columns_moved(recorded, moved_counts)
    assert other["explained_by_ledger_growth_alone"] is False
    assert "archived.symbol_cycles" in other["everything_else"]


def test_classify_lock_tolerates_only_the_order_buckets_moving():
    def census(reasons):
        cleared = sum(v for k, v in reasons.items() if classify(k) == "cleared_regime_gate")
        return {"prediction": {"x": 1},
                "groups": {"archived": {"class_counts": {"blocked_by_regime": 2,
                                                         "cleared_regime_gate": cleared},
                                        "cleared_regime_gate": cleared, "not_evaluable": 0,
                                        "blocked_by_regime": 2, "symbol_cycles": 3,
                                        "clear_rate_pct": round(100 * cleared / 3, 4),
                                        "reason_counts": dict(reasons)}}}

    legacy = census({"regime_filter": 2, "executed:buy": 1})
    current = census({"regime_filter": 2, "executed:buy": 1, "blocked_after_signal:other": 0})
    assert classify_lock(current, legacy)["identical_with_the_single_exception_of_the_order_buckets"] is True

    # A blocked signal that stopped counting as an opportunity must fail the lock, not pass quietly.
    leaked = census({"regime_filter": 3})
    assert classify_lock(leaked, legacy)["identical_with_the_single_exception_of_the_order_buckets"] is False


def test_probe_measures_and_reports_both_clauses_holding(tmp_path):
    """The probe measures the witness's own generation, whatever the store has grown to since."""
    out = tmp_path / "funnel_vocabulary.json"
    code = main(["--out", str(out)])
    assert code == 0, "the funnel no longer reproduces its own generation: D-038 pin is broken"
    payload = json.loads(out.read_text(encoding="utf-8"))
    verdict = payload["verdict"]
    assert verdict["clause_a_executed_matches_book"] is True
    assert verdict["clause_b_published_numbers_reproduced"] is True
    assert verdict["change_is_allowed"] is True


def test_the_generation_is_pinned_to_the_witness_and_published(tmp_path):
    """D-038: the daily audit overwrites the snapshot, so the pair is rebuilt by cutting, not by luck."""
    witness = json.loads((ROOT / "state" / "quant" / "results" / "d035_funnel_vocabulary"
                          / "gate_b_pre_fix_funnel.json").read_text(encoding="utf-8"))
    counted = witness["decision_funnel"]["decisions"]
    out = tmp_path / "funnel_vocabulary.json"
    assert main(["--out", str(out)]) == 0
    provenance = json.loads(out.read_text(encoding="utf-8"))["provenance"]
    assert provenance["as_of"] == witness["evaluated_at"]
    assert provenance["as_of_source"] == "witness"
    decisions = provenance["generation_cut"]["decisions"]
    assert decisions["in_generation"] == counted
    assert decisions["in_store"] >= counted, "the store only grows; the cut is what keeps the pair valid"


def test_an_unpinned_whole_store_audit_still_refuses(tmp_path):
    """Refusing a mismatched pair is the gate; --as-of none must hit it, not silently pass (D-038/الف)."""
    out = tmp_path / "funnel_vocabulary.json"
    assert main(["--as-of", "none", "--out", str(out)]) == 3
    assert not out.exists(), "a refused audit must not publish an artifact"


def test_cutting_a_generation_keeps_only_the_rows_that_existed_then():
    limit = "2026-10-29T17:23:46+00:00"
    rows = [{"ts": limit}, {"ts": "2026-10-29T18:00:00+00:00"}, {"ts": None}]
    assert cut_to_generation(rows, table="decisions", as_of=limit) == rows[:1]
    assert cut_to_generation(rows, table="decisions", as_of=None) == rows, "no pin, no cut"

    far = 9_999_999_999
    trades = [{"entry_ts": 1, "exit_ts": 2}, {"entry_ts": 1, "exit_ts": far},
              {"entry_ts": far, "exit_ts": None}]
    assert cut_to_generation(trades, table="trades", as_of=limit) == trades[:1], (
        "a trade closed after the instant is not in that generation, and neither is one opened after it")


def test_committed_artifact_carries_the_measured_numbers():
    if not ARTIFACT.exists():
        raise SystemExit("run tools/paper_funnel_vocabulary_probe.py before the test suite")
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    h1 = payload["h1"]
    assert h1["reproduced_legacy_executed"] == {"executed:buy": 6, "executed:sell": 1}
    assert h1["vocabulary"]["new_executed_counts"] == {"executed:buy": 2, "executed:sell": 1}
    assert h1["vocabulary"]["over_count_by_bucket"] == {"executed:buy": 4, "executed:sell": 0}
    assert h1["book_and_ledger"]["orders_by_side"] == {"buy": 2, "sell": 1}
    assert h1["book_and_ledger"]["book_and_ledger_agree"] is True

    h2 = payload["h2"]
    assert h2["d027_reproduction"]["reproduced_exactly"] is True
    assert h2["d027_reproduction"]["differences"] == {}
    assert h2["d028_ledger_reproduction"]["matches"] is True
    assert h2["controlled_differential"]["identical_with_the_single_exception_of_the_order_buckets"] is True
    assert h2["input_freeze"]["unfrozen_vs_recorded"]["explained_by_ledger_growth_alone"] is True

    h3 = payload["h3"]
    assert h3["blocked_after_signal"]["by_rationale_counts"] == {SIZE_BLOCK: 3,
                                                                "cooldown after loss, 3600s remaining": 1}
    assert h3["cap_headroom"]["every_size_block_is_below_one_lot"] is True
    refill = h3["cap_headroom"]["serial_refill"]["per_symbol"]
    assert [r["symbol"] for r in refill] == ["BNB_USDT"]
    assert refill[0]["size_blocked_cycles"] == 3
    assert refill[0]["approved_later"] == "2026-10-29T06:41:02+00:00"
