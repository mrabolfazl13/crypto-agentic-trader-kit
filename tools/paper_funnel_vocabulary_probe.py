"""D-035: does the funnel's ``executed:*`` vocabulary agree with the order book?  (read-only)

The gate-B decision funnel is the daily analysis layer, and until this decision it named *every*
non-``NO_TRADE``/``HOLD`` ledger row ``executed:<action>`` while ignoring the ``approved`` column the
same store writes.  On the 2026-10-29 snapshot that reported 6 ``executed:buy`` where the ``orders``
table has 2 buys.  This tool measures the difference and, more importantly, locks the two things the
vocabulary change is only allowed to do: agree with the book, and leave every published D-027/D-028
number exactly where it was.

Nothing here writes engine state, trades, or moves a threshold; the only file written is this tool's
own JSON artifact.  Exit codes: 0 = measured, 3 = the book/store does not match the pre-registered
protocol (an approved signal with no order, an order with no approved signal, or a published
artifact that fails to reproduce).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from cat.paper_evidence import _decision_reason, _rationale_lines  # noqa: E402
from paper_decisions_unarchived import (  # noqa: E402
    classify,
    groups_from_universe,
    measure as d027_measure,
)

EXIT = {"MEASURED": 0, "FAIL": 3}
SILENT_ACTIONS = ("NO_TRADE", "HOLD")
SIZE_BLOCK = "computed position size rounds to zero"


def is_signal(row: Mapping[str, Any]) -> bool:
    """A row where the engine formed an action and reached the risk engine."""
    return str(row.get("action") or "").upper() not in SILENT_ACTIONS


def legacy_reason(row: Mapping[str, Any]) -> str:
    """The pre-D-035 estimator, reconstructed exactly.

    Every non-silent action became ``executed:<action>`` whatever the ``approved`` column said; the
    silent branch is unchanged, so it delegates to the live helper.
    """
    action = str(row.get("action") or "").upper()
    if not is_signal(row):
        return _decision_reason(row)
    return f"executed:{action.lower()}"


def _extras(row: Mapping[str, Any]) -> dict:
    raw = row.get("extras_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def ledger_crosscheck(rows: list[dict], orders: list[dict]) -> dict:
    """Do the funnel's buckets and the order book say the same thing about the same rows?

    Three independent witnesses per row are compared: the ``approved`` column, whether an ``orders``
    row carries this ``decision_id``, and ``extras_json.order_id`` written at decision time.
    """
    signal_rows = [row for row in rows if is_signal(row)]
    order_ids = {str(o.get("decision_id")) for o in orders if o.get("decision_id")}
    per_side: dict[str, Counter] = {}
    approved_without_order: list[str] = []
    blocked_with_order: list[str] = []
    extras_disagree: list[str] = []
    counts: Counter = Counter()
    for row in signal_rows:
        action = str(row.get("action") or "").upper()
        approved = bool(row.get("approved"))
        has_order = str(row.get("id")) in order_ids
        counts[f"{action.lower()}|approved={int(approved)}"] += 1
        if approved and not has_order:
            approved_without_order.append(str(row.get("id")))
        if not approved and has_order:
            blocked_with_order.append(str(row.get("id")))
        if bool(_extras(row).get("order_id")) != has_order:
            extras_disagree.append(str(row.get("id")))
        if approved:
            per_side.setdefault(action.lower(), Counter())[ "approved_rows"] += 1
    order_side_counts = Counter(str(o.get("side") or "").lower() for o in orders)
    orders_without_approved_signal = sorted(
        str(o.get("decision_id")) for o in orders
        if str(o.get("decision_id")) not in {str(r.get("id")) for r in signal_rows if r.get("approved")}
    )
    consistent = not (approved_without_order or blocked_with_order or extras_disagree
                      or orders_without_approved_signal)
    return {
        "signal_rows": len(signal_rows),
        "signal_rows_by_action_and_approval": dict(sorted(counts.items())),
        "approved_signal_rows": sum(1 for r in signal_rows if r.get("approved")),
        "blocked_signal_rows": sum(1 for r in signal_rows if not r.get("approved")),
        "orders_rows": len(orders),
        "orders_by_side": dict(sorted(order_side_counts.items())),
        "approved_rows_by_side": {side: c["approved_rows"] for side, c in sorted(per_side.items())},
        "approved_signal_without_order": approved_without_order,
        "blocked_signal_with_order": blocked_with_order,
        "order_without_approved_signal": orders_without_approved_signal,
        "extras_order_id_disagreements": extras_disagree,
        "book_and_ledger_agree": consistent,
    }


def vocabulary_comparison(rows: list[dict]) -> dict:
    """Old bucket names vs new ones over the identical rows, plus the over-count."""
    old: Counter = Counter(legacy_reason(row) for row in rows)
    new: Counter = Counter(_decision_reason(row) for row in rows)
    executed_old = {r: c for r, c in old.items() if r.startswith("executed:")}
    executed_new = {r: c for r, c in new.items() if r.startswith("executed:")}
    blocked_new = {r: c for r, c in new.items() if r.startswith("blocked_after_signal:")}
    unverified_new = {r: c for r, c in new.items() if r.startswith("unverified_action:")}
    over = {r: executed_old.get(r, 0) - executed_new.get(r, 0) for r in sorted(set(executed_old) | set(executed_new))}
    return {
        "legacy_executed_counts": dict(sorted(executed_old.items())),
        "new_executed_counts": dict(sorted(executed_new.items())),
        "blocked_after_signal_counts": dict(sorted(blocked_new.items())),
        "unverified_action_counts": dict(sorted(unverified_new.items())),
        "over_count_by_bucket": over,
        "legacy_overstates_execution": any(v > 0 for v in over.values()),
        "signal_rows_total": sum(1 for row in rows if is_signal(row)),
    }


def blocked_signal_census(rows: list[dict]) -> dict:
    """Every signal the risk engine refused, with the verbatim line it wrote — no labels."""
    blocked = [row for row in rows if is_signal(row) and not row.get("approved")]
    rationale_counts: Counter = Counter()
    for row in blocked:
        lines = _rationale_lines(row)
        rationale_counts[lines[0] if lines else "<no rationale>"] += 1
    return {
        "rows": len(blocked),
        "by_rationale_counts": dict(rationale_counts.most_common()),
        "by_symbol_counts": dict(Counter(str(row.get("symbol")) for row in blocked).most_common()),
        "detail": [{"ts": str(row.get("ts")), "symbol": str(row.get("symbol")),
                    "action": str(row.get("action")), "rationale": (_rationale_lines(row) or [""])[0]}
                   for row in sorted(blocked, key=lambda r: str(r.get("ts")))],
    }


def _exposure_value(snapshot: Mapping[str, Any]) -> float | None:
    equity, pct = snapshot.get("equity"), snapshot.get("exposure_pct")
    if equity is None or pct is None:
        return None
    return round(float(equity) * float(pct) / 100.0, 6)


def cap_headroom(rows: list[dict], snapshots: list[dict], *, allocation_cap: float | None,
                 orders: list[dict] | None = None) -> dict:
    """What the 5% total-allocation cap left free at the moment each size-blocked signal fired.

    ``allocated_cap - exposure`` is the numerator of ``qty_by_exposure`` (``src/cat/risk.py:190``);
    if it is below one lot of the symbol the size rounds to zero, which is the whole reason the row
    exists.  This is the deployed-path lower bound D-030 could only derive from a replay.
    """
    blocked = [row for row in rows if is_signal(row) and not row.get("approved")]
    ordered = sorted(snapshots, key=lambda s: str(s.get("ts")))
    book = orders or []
    # The store has no exchange lot step, so the precision floor is inferred per symbol from the
    # quantities the book actually recorded for that symbol: a qty of 0.066 proves the loop can
    # submit 1e-3, and nothing in the book proves it can submit less.
    symbol_book: dict[str, dict] = {}
    for order in sorted(book, key=lambda o: str(o.get("created_ts"))):
        symbol = str(order.get("symbol"))
        entry = symbol_book.setdefault(symbol, {"prices": [], "qty_steps": []})
        if order.get("price"):
            entry["prices"].append(float(order["price"]))
        qty_text = str(order.get("qty") or "")
        if "." in qty_text:
            entry["qty_steps"].append(10.0 ** -len(qty_text.split(".")[1]))
    qty_step_overall = min((min(e["qty_steps"]) for e in symbol_book.values() if e["qty_steps"]),
                           default=None)
    notes: list[dict] = []
    for row in sorted(blocked, key=lambda r: str(r.get("ts"))):
        ts = str(row.get("ts"))
        symbol = str(row.get("symbol"))
        at_or_before = [s for s in ordered if str(s.get("ts")) <= ts]
        snapshot = at_or_before[-1] if at_or_before else None
        value = _exposure_value(snapshot) if snapshot else None
        headroom = (round(allocation_cap - value, 6) if allocation_cap is not None and value is not None else None)
        entry = symbol_book.get(symbol) or {}
        steps = entry.get("qty_steps") or []
        prices = entry.get("prices") or []
        price = min(prices) if prices else None
        step = min(steps) if steps else None
        qty_estimate = round(headroom / price, 8) if headroom is not None and price else None
        min_lot_notional = round(price * step, 6) if price and step else None
        notes.append({
            "ts": ts, "symbol": symbol,
            "rationale": (_rationale_lines(row) or [""])[0],
            "snapshot_ts": str(snapshot.get("ts")) if snapshot else None,
            "equity": snapshot.get("equity") if snapshot else None,
            "exposure_value": value,
            "headroom_usdt": headroom,
            "positions_open": len(json.loads((snapshot or {}).get("positions_json") or "[]")),
            "reference_price_from_book": price,
            "qty_step_seen_in_book": step,
            "qty_by_exposure_estimate": qty_estimate,
            "one_lot_notional_estimate": min_lot_notional,
            "headroom_below_one_lot": (bool(headroom < min_lot_notional)
                                       if headroom is not None and min_lot_notional else None),
        })
    size_blocks = [n for n in notes if n["rationale"] == SIZE_BLOCK]
    fills = sorted([row for row in rows if is_signal(row) and row.get("approved")],
                   key=lambda r: str(r.get("ts")))
    refill: list[dict] = []
    for symbol in sorted({n["symbol"] for n in size_blocks}):
        blocks = [n for n in size_blocks if n["symbol"] == symbol]
        last = blocks[-1]["ts"]
        approval = next((f for f in fills if str(f.get("symbol")) == symbol and str(f.get("ts")) > last), None)
        refill.append({"symbol": symbol, "size_blocked_cycles": len(blocks),
                       "last_size_block": last,
                       "approved_later": str(approval.get("ts")) if approval else None,
                       "hours_between": (round((_epoch(approval.get("ts")) - _epoch(last)) / 3600.0, 3)
                                         if approval else None)})
    return {
        "allocation_cap_usdt": allocation_cap,
        "book_qty_step_smallest_seen_in_any_recorded_qty": qty_step_overall,
        "rows": notes,
        "size_blocked_symbols": sorted({n["symbol"] for n in size_blocks}),
        "min_headroom_usdt": min((n["headroom_usdt"] for n in size_blocks
                                  if n["headroom_usdt"] is not None), default=None),
        "max_headroom_usdt": max((n["headroom_usdt"] for n in size_blocks
                                  if n["headroom_usdt"] is not None), default=None),
        "every_size_block_is_below_one_lot": bool(size_blocks) and all(
            n["headroom_below_one_lot"] is True for n in size_blocks),
        "serial_refill": {
            "note": "did the same symbol get approved once the held position closed? (one real slot, D-030)",
            "per_symbol": refill,
        },
    }


def _run_census(rows: list[dict], orders: list[dict], trades: list[dict], groups: dict) -> dict:
    return d027_measure(rows, orders, trades, groups)


def _with_legacy_reasons(rows: list[dict], orders: list[dict], trades: list[dict], groups: dict) -> dict:
    """Same census, same rows, only the *estimator* swapped — the controlled half of H2."""
    import paper_decisions_unarchived as unarchived

    original = unarchived._decision_reason
    try:
        unarchived._decision_reason = legacy_reason
        return d027_measure(rows, orders, trades, groups)
    finally:
        unarchived._decision_reason = original


def classify_lock(current: dict, legacy: dict) -> dict:
    """H2's controlled clause: the vocabulary change moves no class, no count, no ratio."""
    per_group: dict[str, dict] = {}
    identical = True
    for name, group in current["groups"].items():
        old = legacy["groups"][name]
        reasons_moved = {key: (old["reason_counts"].get(key, 0), group["reason_counts"].get(key, 0))
                         for key in set(old["reason_counts"]) | set(group["reason_counts"])
                         if old["reason_counts"].get(key, 0) != group["reason_counts"].get(key, 0)}
        same = (old["class_counts"] == group["class_counts"]
                and old["cleared_regime_gate"] == group["cleared_regime_gate"]
                and old["not_evaluable"] == group["not_evaluable"]
                and old["blocked_by_regime"] == group["blocked_by_regime"]
                and old["symbol_cycles"] == group["symbol_cycles"]
                and old["clear_rate_pct"] == group["clear_rate_pct"]
                and all(k.startswith(("executed:", "blocked_after_signal:", "unverified_action:"))
                        for k in reasons_moved))
        identical = identical and same
        per_group[name] = {"class_counts_match": same,
                           "class_counts": group["class_counts"],
                           "reason_keys_changed_by_vocabulary": reasons_moved}
    return {"identical_with_the_single_exception_of_the_order_buckets": identical,
            "groups": per_group,
            "prediction_class_match": current["prediction"] == legacy["prediction"]}


def artifact_reproduction(current: dict, recorded: Mapping[str, Any], *, window: list[str]) -> dict:
    """H2's absolute clause: the *published* D-027 numbers must reproduce exactly on its own window."""
    differences: dict[str, Any] = {}
    for name, group in recorded.get("groups", {}).items():
        now = current["groups"].get(name, {})
        for key, want in group.items():
            if key == "per_symbol":
                for symbol, recorded_symbol in (want or {}).items():
                    if now.get("per_symbol", {}).get(symbol) != recorded_symbol:
                        differences[f"{name}.per_symbol.{symbol}"] = {
                            "recorded": recorded_symbol, "reproduced": now.get("per_symbol", {}).get(symbol)}
                continue
            got = now.get(key)
            if got != want:
                differences[f"{name}.{key}"] = {"recorded": want, "reproduced": got}
    if recorded.get("window") != current.get("window"):
        differences["window"] = {"recorded": recorded.get("window"), "reproduced": current.get("window")}
    if recorded.get("prediction") != current.get("prediction"):
        differences["prediction"] = {"recorded": recorded.get("prediction"), "reproduced": current.get("prediction")}
    return {"recorded_generated_at": recorded.get("generated_at"),
            "window_replayed": window,
            "reproduced_exactly": not differences,
            "differences": differences}


def _epoch(ts: Any) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return None


def freeze_book(orders: list[dict], trades: list[dict], window: list[str]) -> tuple[list[dict], list[dict]]:
    """The order/trade rows that existed when a published artifact was written.

    The D-027 census counts ``orders`` and ``closed_trades`` over the whole tables, so re-running it
    on a later snapshot reports new facts, not a new estimator.  Freezing the book to the recorded
    window is what makes clause (ب) a test of the *vocabulary*; the unfrozen delta is reported
    alongside it so the freeze is never used to hide a real difference.
    """
    end = _epoch(window[1])
    kept_orders = [o for o in orders if (_epoch(o.get("created_ts")) or 0.0) <= end]
    kept_trades = [t for t in trades if (_epoch(t.get("exit_ts")) or 0.0) <= end]
    return kept_orders, kept_trades


def only_book_columns_moved(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> dict:
    """Which recorded fields the later snapshot alone explains — must be nothing but the book."""
    moved: dict[str, dict] = {}
    other: dict[str, dict] = {}
    for name, group in (recorded.get("groups") or {}).items():
        now = (current.get("groups") or {}).get(name, {})
        for key, want in group.items():
            if key == "per_symbol":
                continue
            got = now.get(key)
            if got != want:
                item = {"recorded": want, "reproduced": got}
                (moved if key in ("orders", "closed_trades") else other)[f"{name}.{key}"] = item
    return {"book_columns": moved, "everything_else": other,
            "explained_by_ledger_growth_alone": not other}


def d028_ledger_lock(current: dict, recorded: Mapping[str, Any], window: list[str]) -> dict:
    """The D-028 ledger census consumed ``classify``; its not-evaluable totals may not move."""
    census = recorded.get("ledger_census") or {}
    groups = current["groups"]
    totals_now: Counter = Counter()
    for group in groups.values():
        for reason, count in (group.get("reason_counts") or {}).items():
            if classify(reason) == "not_evaluable":
                totals_now[reason] += count
    recorded_totals = Counter(census.get("not_evaluable_totals") or {})
    return {"recorded_generated_at": recorded.get("generated_at"),
            "recorded_window": window,
            "recorded_not_evaluable_totals": dict(sorted(recorded_totals.items())),
            "reproduced_not_evaluable_totals": dict(sorted(totals_now.items())),
            "matches": dict(sorted(recorded_totals.items())) == dict(sorted(totals_now.items())),
            "note": "only the ledger half of D-028 is re-checkable offline; its replay half needs the market"}


def funnel_agrees_with_book(funnel_executed: Mapping[str, Any], book: Mapping[str, Any]) -> dict:
    """Clause (A): the new ``executed:*`` counts equal the approved rows, side by side.

    A store with no ``approved`` column yields an empty funnel (every signal row is
    ``unverified_action:*``); that is reported as "the funnel does not claim an execution" rather
    than as a mismatch with a book it cannot read.
    """
    if not book["approved_signal_rows"]:
        return {"funnel_executed": dict(sorted(funnel_executed.items())),
                "book_approved_rows": {},
                "agree": not funnel_executed,
                "note": "the store records no approvals, so the book half is vacuous"}
    want = {f"executed:{side}": count for side, count in book["approved_rows_by_side"].items()}
    return {"funnel_executed": dict(sorted(funnel_executed.items())),
            "book_approved_rows": dict(sorted(want.items())),
            "agree": funnel_executed == want}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _select(connection: sqlite3.Connection, table: str, wanted: tuple, *,
            order: str | None = None) -> str:
    """Read only the columns this store actually has, so an older snapshot is still auditable."""
    available = set(_columns(connection, table))
    if not available:
        raise SystemExit(f"{table} is not in this store: cannot audit the funnel without it")
    fields = [name for name in wanted if name in available]
    sql = f"SELECT {', '.join(fields)} FROM {table}"
    if order and order in available:
        sql += f" ORDER BY {order}"
    return sql


_TIME_COLUMN = {"decisions": "ts", "orders": "created_ts", "portfolio_snapshots": "ts"}


def cut_to_generation(rows: list[dict], *, table: str, as_of: str | None) -> list[dict]:
    """Keep only rows that existed at the instant the witness was written (D-038).

    The live snapshot is a superset — the daily audit overwrites it — so the generation the witness
    counted is rebuilt from the timestamp the witness itself carries.  A row with no placeable
    timestamp is dropped rather than assumed to belong to the generation.
    """
    if as_of is None:
        return rows
    limit = datetime.fromisoformat(as_of)
    if table == "trades":
        epoch = limit.timestamp()
        return [row for row in rows if (row.get("entry_ts") or 0) <= epoch
                and (row.get("exit_ts") is None or float(row["exit_ts"]) <= epoch)]
    column = _TIME_COLUMN[table]
    kept = []
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        if datetime.fromisoformat(str(value)) <= limit:
            kept.append(row)
    return kept


def recorded_funnel_from(payload: Mapping[str, Any]) -> dict:
    """The funnel as the *pre-D-035* estimator wrote it, from either file shape.

    This tool's witness file wraps it under ``decision_funnel`` — an immutable copy, because the live
    gate-B report is overwritten every day and carries the new vocabulary from the next run onward —
    while a gate-B evidence file can be the funnel itself.
    """
    inner = payload.get("decision_funnel")
    return inner if isinstance(inner, dict) else dict(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(ROOT / "state/reports/server_platform_snapshot.sqlite3"))
    parser.add_argument("--universe", default=str(ROOT / "state/quant/universe_allowlist.json"))
    parser.add_argument("--recorded-funnel",
                        default=str(ROOT / "state/quant/results/d035_funnel_vocabulary/gate_b_pre_fix_funnel.json"),
                        help="the gate-B funnel written before the D-035 vocabulary fix")
    parser.add_argument("--d027", default=str(ROOT / "state/quant/results/breadth_audit/d027_unarchived_groundtruth.json"))
    parser.add_argument("--d028", default=str(ROOT / "state/quant/results/d028_precision/unevaluability.json"))
    parser.add_argument("--allocation-cap", type=float, default=50.0,
                        help="allocated_capital * max_account_allocation_pct / 100 (config.yaml:123-124)")
    parser.add_argument("--as-of", default=None,
                        help="rebuild the witness's generation by cutting the store at this ISO instant; "
                             "default: the witness's own evaluated_at; 'none' to audit the whole store")
    parser.add_argument("--out", default=str(ROOT / "state/quant/results/d035_funnel_vocabulary/funnel_vocabulary.json"))
    args = parser.parse_args(argv)

    recorded_payload = read_json(Path(args.recorded_funnel))
    recorded_funnel = recorded_funnel_from(recorded_payload)
    if args.as_of == "none":
        as_of, as_of_source = None, "disabled"
    elif args.as_of:
        as_of, as_of_source = args.as_of, "explicit"
    else:
        as_of, as_of_source = recorded_payload.get("evaluated_at"), "witness"

    connection = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    loaded = {table: [dict(r) for r in connection.execute(_select(connection, table, cols, order=order))]
              for table, cols, order in (
                  ("decisions", ("id", "ts", "symbol", "action", "approved", "rationale_json",
                                 "extras_json", "mode"), "ts"),
                  ("orders", ("decision_id", "symbol", "side", "qty", "price", "status",
                              "created_ts"), "created_ts"),
                  ("trades", ("symbol", "entry_ts", "exit_ts"), None),
                  ("portfolio_snapshots", ("ts", "equity", "exposure_pct", "positions_json"), "ts"))}
    columns = {table: _columns(connection, table) for table in
               ("decisions", "orders", "trades", "portfolio_snapshots")}
    connection.close()

    if "approved" not in columns["decisions"] or "decision_id" not in columns["orders"]:
        print(f"REFUSED: this store cannot be cross-checked — decisions/approved or orders/decision_id "
              f"is missing ({columns['decisions']} / {columns['orders']})")
        return EXIT["FAIL"]
    if {str(row.get("mode") or "") for row in loaded["decisions"]} - {"PAPER"}:
        print(f"REFUSED: decisions carry a non-PAPER mode: "
              f"{sorted({str(r.get('mode') or '') for r in loaded['decisions']})}")
        return EXIT["FAIL"]

    rows = cut_to_generation(loaded["decisions"], table="decisions", as_of=as_of)
    orders = cut_to_generation(loaded["orders"], table="orders", as_of=as_of)
    trades = cut_to_generation(loaded["trades"], table="trades", as_of=as_of)
    snapshots = cut_to_generation(loaded["portfolio_snapshots"], table="portfolio_snapshots", as_of=as_of)

    if recorded_funnel.get("decisions") != len(rows):
        print(f"REFUSED: {args.recorded_funnel} counted {recorded_funnel.get('decisions')} decisions, "
              f"this snapshot has {len(rows)} (as-of {as_of or 'whole store'}) — the recorded funnel "
              f"is not from this generation")
        return EXIT["FAIL"]

    groups = groups_from_universe(json.loads(Path(args.universe).read_text(encoding="utf-8")))
    d027_recorded = read_json(Path(args.d027))
    d028_recorded = read_json(Path(args.d028))
    window = [d027_recorded["window"]["first_cycle"], d027_recorded["window"]["last_cycle"]]
    window_rows = [row for row in rows if window[0] <= str(row["ts"]) <= window[1]]

    h1_book = ledger_crosscheck(rows, orders)
    h1_vocab = vocabulary_comparison(rows)
    legacy_on_full = Counter(legacy_reason(row) for row in rows)
    legacy_executed = {r: c for r, c in legacy_on_full.items() if r.startswith("executed:")}
    recorded_legacy = {r: c for r, c in (recorded_funnel.get("executed") or {}).items()}
    if legacy_executed != recorded_legacy:
        print(f"REFUSED: the reconstructed legacy estimator gives {legacy_executed} but "
              f"{args.recorded_funnel} recorded {recorded_legacy} — provenance of the fix is not established")
        return EXIT["FAIL"]

    frozen_orders, frozen_trades = freeze_book(orders, trades, window)
    census_frozen = _run_census(window_rows, frozen_orders, frozen_trades, groups)
    census_frozen_legacy = _with_legacy_reasons(window_rows, frozen_orders, frozen_trades, groups)
    census_asof_now = _run_census(window_rows, orders, trades, groups)
    h2 = {
        "input_freeze": {"rule": "orders by created_ts, closed trades by exit_ts, both <= window end",
                         "orders_kept": len(frozen_orders), "orders_in_snapshot": len(orders),
                         "trades_kept": len(frozen_trades), "trades_in_snapshot": len(trades),
                         "unfrozen_vs_recorded": only_book_columns_moved(d027_recorded, census_asof_now)},
        "controlled_differential": classify_lock(census_frozen, census_frozen_legacy),
        "d027_reproduction": artifact_reproduction(census_frozen, d027_recorded, window=window),
        "d028_ledger_reproduction": d028_ledger_lock(census_frozen, d028_recorded, window=window),
    }

    cut_sizes = {table: {"in_store": len(loaded[table]), "in_generation": len(kept)}
                 for table, kept in (("decisions", rows), ("orders", orders), ("trades", trades),
                                     ("portfolio_snapshots", snapshots))}

    blocked = blocked_signal_census(rows)
    h3 = {
        "blocked_after_signal": blocked,
        "cap_headroom": cap_headroom(rows, snapshots, allocation_cap=args.allocation_cap, orders=orders),
    }

    verdict = {
        "clause_a_executed_matches_book": funnel_agrees_with_book(
            h1_vocab["new_executed_counts"], h1_book)["agree"],
        "clause_b_published_numbers_reproduced": bool(
            h2["d027_reproduction"]["reproduced_exactly"] and h2["d028_ledger_reproduction"]["matches"]
            and h2["controlled_differential"]["identical_with_the_single_exception_of_the_order_buckets"]
            and h2["input_freeze"]["unfrozen_vs_recorded"]["explained_by_ledger_growth_alone"]),
        "book_and_ledger_agree": h1_book["book_and_ledger_agree"],
        "change_is_allowed": bool(h1_book["book_and_ledger_agree"]
                                  and h2["d027_reproduction"]["reproduced_exactly"]),
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision": "D-035",
        "kind": "vocabulary audit of the gate-B decision funnel (no expectancy measured here)",
        "provenance": {"db": str(Path(args.db)),
                       "recorded_funnel_file": str(Path(args.recorded_funnel)),
                       "recorded_funnel_evaluated_at": recorded_payload.get("evaluated_at"),
                       "d027": str(Path(args.d027)), "d028": str(Path(args.d028)),
                       "universe": str(Path(args.universe)),
                       "decision_rows": len(rows), "orders_rows": len(orders),
                       "as_of": as_of, "as_of_source": as_of_source,
                       "generation_cut": {table: {"in_store": len(loaded[table]), "in_generation": len(kept)}
                                          for table, kept in (("decisions", rows), ("orders", orders),
                                                              ("trades", trades),
                                                             ("portfolio_snapshots", snapshots))},
                       "store_columns": columns,
                       "allocation_cap_usdt": args.allocation_cap, "read_only": True},
        "recorded_funnel_before_the_fix": recorded_funnel,
        "h1": {"book_and_ledger": h1_book, "vocabulary": h1_vocab,
               "reproduced_legacy_executed": legacy_executed,
               "funnel_agrees_with_book": funnel_agrees_with_book(
                   h1_vocab["new_executed_counts"], h1_book)},
        "h2": h2,
        "h3": h3,
        "verdict": verdict,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"generation: as-of {as_of} ({as_of_source}); "
          + "  ".join(f"{table} {cut['in_store']}->{cut['in_generation']}"
                      for table, cut in cut_sizes.items()))
    print(f"signal rows: {h1_book['signal_rows']} "
          f"(approved {h1_book['approved_signal_rows']}, blocked {h1_book['blocked_signal_rows']}); "
          f"orders rows: {h1_book['orders_rows']} {h1_book['orders_by_side']}")
    print(f"legacy executed: {h1_vocab['legacy_executed_counts']} -> "
          f"new {h1_vocab['new_executed_counts']} + {h1_vocab['blocked_after_signal_counts']}"
          f" (over-count {h1_vocab['over_count_by_bucket']})")
    print(f"clause (A) funnel==book: {verdict['clause_a_executed_matches_book']}; "
          f"clause (B) D-027 reproduced exactly: {h2['d027_reproduction']['reproduced_exactly']} "
          f"(differences: {list(h2['d027_reproduction']['differences'])}); "
          f"D-028 ledger totals match: {h2['d028_ledger_reproduction']['matches']}")
    print(f"blocked rationales: {blocked['by_rationale_counts']}")
    print(f"verdict: {verdict}")
    print(f"wrote {out}")
    return EXIT["MEASURED"] if verdict["book_and_ledger_agree"] else EXIT["FAIL"]


if __name__ == "__main__":
    raise SystemExit(main())
