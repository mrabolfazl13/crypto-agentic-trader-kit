"""What the deployed loop itself recorded about the un-replayable part of the allowlist (read-only).

D-026 measured the deployed basket and found that 31 of the 100 orderable symbols have no
archive on the research source, so no replay can ever speak about them.  The only remaining
witness is the engine's own ``decisions`` table: did the loop even see those symbols, and did
any of them clear the regime gate in a real cycle?  This tool counts that.  It is an
*opportunity* census — it reports no profit factor, no net and no per-symbol "discovery", and
it never opens a live store for writing.

Exit codes: 0 = measured, 3 = the evidence is not PAPER-only or the inputs are inconsistent.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.paper_evidence import _decision_reason  # noqa: E402  (single source for the reason bucket)

EXIT = {"MEASURED": 0, "FAIL": 3}

REGIME_BLOCK = "regime_filter"
# A row is only a *real* opportunity when the engine evaluated the symbol and moved past the
# regime gate.  "no_atr" / "insufficient_history" / "source_price_disagreement" are the engine
# saying it could not look at the symbol at all — counting those as gate passes would turn a
# data problem into a fake signal, which is exactly what the D-027 pre-registration forbids.
NOT_EVALUABLE = ("no_atr", "insufficient_history", "source_price_disagreement")
PASSED_REGIME = ("hold_position",)
# The pre-registered band: the two groups may differ by at most this factor before the
# prediction ("the missing evidence is a data gap, not a signal gap") is falsified.
BAND_LOW, BAND_HIGH = 0.5, 2.0


def classify(reason: str) -> str:
    if reason == REGIME_BLOCK:
        return "blocked_by_regime"
    if reason in NOT_EVALUABLE:
        return "not_evaluable"
    # A row that reached sizing (and was blocked there, D-035) did clear the regime gate, so it
    # must keep counting here; only the *order* vocabulary changed.
    if reason in PASSED_REGIME or reason.startswith(("executed:", "blocked_after_signal:",
                                                     "unverified_action:")):
        return "cleared_regime_gate"
    return f"other:{reason}"


def canonical(symbol: str) -> str:
    """`AERO_USDT` (as the store writes it) and `AEROUSDT` (as the universe writes it) match."""
    return str(symbol).replace("_", "").upper()


def groups_from_universe(universe: dict) -> dict[str, list[str]]:
    unarchived = sorted({str(row["symbol"]) for row in universe.get("probe_errors", [])})
    archived = sorted({str(row["binance_symbol"]) for row in universe["entries"]})
    overlap = sorted(set(canonical(s) for s in unarchived) & set(canonical(s) for s in archived))
    if overlap:
        raise SystemExit(f"symbol in both groups (universe file is inconsistent): {overlap}")
    return {"unarchived": unarchived, "archived": archived}


def _measure_group(rows: list[dict], group: list[str]) -> dict:
    wanted = {canonical(s): s for s in group}
    hits: dict[str, dict] = {}
    stamps: set[str] = set()
    reasons: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    for row in rows:
        key = canonical(row["symbol"])
        if key not in wanted:
            continue
        symbol = wanted[key]
        entry = hits.setdefault(symbol, {"cycles": 0, "cleared_regime": 0, "not_evaluable": 0,
                                         "reasons": Counter()})
        entry["cycles"] += 1
        stamps.add(str(row["ts"]))
        reason = _decision_reason(row)
        kind = classify(reason)
        entry["reasons"][reason] += 1
        reasons[reason] += 1
        classes[kind] += 1
        if kind == "cleared_regime_gate":
            entry["cleared_regime"] += 1
        elif kind == "not_evaluable":
            entry["not_evaluable"] += 1
    seen = len(hits)
    cycles = sum(entry["cycles"] for entry in hits.values())
    cleared = sum(entry["cleared_regime"] for entry in hits.values())
    pct = lambda count: round(100.0 * count / cycles, 4) if cycles else None  # noqa: E731
    return {
        "symbols_in_group": len(group),
        "symbols_seen_by_loop": seen,
        "not_seen_by_loop": sorted(set(wanted.values()) - set(hits)),
        "cycles_with_a_row": len(stamps),
        "symbol_cycles": cycles,
        "cleared_regime_gate": cleared,
        "clear_rate_pct": pct(cleared),
        "not_evaluable": classes["not_evaluable"],
        "not_evaluable_pct": pct(classes["not_evaluable"]),
        "blocked_by_regime": classes["blocked_by_regime"],
        "blocked_by_regime_pct": pct(classes["blocked_by_regime"]),
        "reason_counts": dict(reasons.most_common()),
        "class_counts": dict(classes.most_common()),
        "per_symbol": {s: {"cycles": e["cycles"], "cleared_regime_gate": e["cleared_regime"],
                           "not_evaluable": e["not_evaluable"], "reasons": dict(e["reasons"])}
                       for s, e in sorted(hits.items())},
    }


def measure(decision_rows: list[dict], orders: list[dict], trades: list[dict],
            groups: dict[str, list[str]]) -> dict:
    stamps = sorted({str(row["ts"]) for row in decision_rows})
    out: dict[str, object] = {
        "window": {"first_cycle": stamps[0] if stamps else None,
                   "last_cycle": stamps[-1] if stamps else None,
                   "distinct_cycles": len(stamps)},
        "groups": {name: _measure_group(decision_rows, symbols) for name, symbols in groups.items()},
    }
    rates = {name: out["groups"][name]["clear_rate_pct"] for name in groups}
    un, ar = rates["unarchived"], rates["archived"]
    unevaluable = {name: out["groups"][name]["not_evaluable_pct"] for name in groups}
    if un and ar:
        ratio = round(un / ar, 4)
        out["prediction"] = {
            "registered": f"clear-rate ratio unarchived/archived within {BAND_LOW}-{BAND_HIGH}",
            "ratio_unarchived_over_archived": ratio,
            "inside_band": BAND_LOW <= ratio <= BAND_HIGH,
        }
    else:
        out["prediction"] = {
            "registered": f"clear-rate ratio unarchived/archived within {BAND_LOW}-{BAND_HIGH}",
            "ratio_unarchived_over_archived": None,
            "inside_band": None,
            "note": "at least one group cleared the regime gate zero times, so the ratio is undefined",
        }
    if unevaluable["archived"]:
        out["not_evaluable_ratio_unarchived_over_archived"] = round(
            unevaluable["unarchived"] / unevaluable["archived"], 4)
    for name, symbols in groups.items():
        wanted = {canonical(s) for s in symbols}
        out["groups"][name]["orders"] = sum(1 for o in orders if canonical(o["symbol"]) in wanted)
        out["groups"][name]["closed_trades"] = sum(1 for t in trades if canonical(t["symbol"]) in wanted)
    return out


def decision_projection(connection: sqlite3.Connection) -> str:
    """Project ``approved`` only when the store has it (D-035).

    A store that predates the column must not be silently read as "nothing was approved"; the
    funnel labels those rows ``unverified_action:*`` instead, so the census keeps counting them as
    gate-clearing opportunities without claiming an order was placed.
    """
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(decisions)")}
    fields = ["ts", "symbol", "action"]
    if "approved" in columns:
        fields.append("approved")
    fields += ["rationale_json", "mode"]
    return f"SELECT {', '.join(fields)} FROM decisions ORDER BY ts"


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _universe_digest(path: Path) -> tuple[str | None, str]:
    """The same canonical digest the D-026 replays recorded, so the two artifacts cross-reference.

    Provenance is advisory — if the file is not a loadable quant universe the census still runs,
    and the reason is written into the artifact instead of being swallowed.
    """
    try:
        from dataclasses import replace

        from cat.config import load_config
        from cat.quant.settings import load_quant_settings
        from cat.quant.storage import universe_digest

        settings = load_quant_settings(load_config(root=ROOT))
        return universe_digest(replace(settings, universe_path=path)), "ok"
    except Exception as exc:  # noqa: BLE001 - the census must not die on a provenance lookup
        return None, f"digest unavailable: {type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="read-only snapshot of the server store")
    parser.add_argument("--universe", default=str(ROOT / "state/quant/universe_allowlist.json"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    universe = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    groups = groups_from_universe(universe)

    connection = _read_only(Path(args.db))
    decision_rows = [dict(row) for row in connection.execute(decision_projection(connection))]
    modes = {str(row.get("mode") or "") for row in decision_rows}
    if modes - {"PAPER"}:
        print(f"REFUSED: decisions carry a non-PAPER mode: {sorted(modes)}")
        return EXIT["FAIL"]
    orders = [dict(row) for row in connection.execute("SELECT symbol FROM orders")]
    trades = [dict(row) for row in connection.execute("SELECT symbol FROM trades")]

    digest, digest_note = _universe_digest(Path(args.universe))
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "db": str(Path(args.db)), "universe_file": Path(args.universe).name,
               "universe_digest": digest, "universe_digest_note": digest_note,
               "kind": "opportunity census (no expectancy measured here)", **measure(
                   decision_rows, orders, trades, groups)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"decisions rows: {len(decision_rows)} in {payload['window']['distinct_cycles']} cycles "
          f"({payload['window']['first_cycle']} .. {payload['window']['last_cycle']})")
    for name in ("unarchived", "archived"):
        g = payload["groups"][name]
        print(f"{name}: {g['symbols_in_group']} symbols, seen {g['symbols_seen_by_loop']} "
              f"(not seen: {len(g['not_seen_by_loop'])}), {g['symbol_cycles']} symbol-cycles, "
              f"cleared regime gate {g['cleared_regime_gate']} => {g['clear_rate_pct']}%, "
              f"not evaluable {g['not_evaluable']} => {g['not_evaluable_pct']}%, "
              f"blocked by regime {g['blocked_by_regime']} => {g['blocked_by_regime_pct']}%, "
              f"orders {g['orders']}, closed trades {g['closed_trades']}")
    print(f"prediction check: {payload['prediction']}")
    print(f"wrote {out}")
    return EXIT["MEASURED"]


if __name__ == "__main__":
    raise SystemExit(main())
