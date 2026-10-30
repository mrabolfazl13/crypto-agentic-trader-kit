"""D-030 probe: is the second open-position slot reachable, and what does that do to cadence?

Read-only. Two questions:

  1. The deployed config says max_open_positions = 2, but the sizing path caps TOTAL
     deployed value at max_account_allocation_pct.  Is the second slot fundable at all?
  2. If only one slot is reachable, what is the maximum closed-trade cadence, and does
     it clear gate B's required rate?

Two estimators were wrong before and are pinned here.  (a) D-035/F-039: the ceiling is a function of
the *holding time of a completed trade*; the age of whatever position is open is a censored lower
bound and is recorded separately.  (b) D-037: the ceiling must be estimated from **all** closed
trades, not the newest one — with `ORDER BY exit_ts DESC LIMIT 1` the verdict flipped from
"unreachable" to "reachable" the day a second trade closed, on the same config and the same window.
Because individual trades can straddle the requirement, the verdict is tri-state: it is only
`True`/`False` when the whole per-trade band agrees, otherwise `null` with the band published.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import load_config  # noqa: E402

OUT = ROOT / "state" / "quant" / "results" / "d030_slots"
DB = ROOT / "state" / "reports" / "server_platform_snapshot.sqlite3"
GATE_B = ROOT / "state" / "reports" / "gate_b_evidence_server.json"


def slot_arithmetic(*, allocated_cap: float, position_cap: float, deployed: float,
                    open_positions: int, max_open: int) -> dict[str, Any]:
    """How many positions the caps actually fund, stated as the sizing path computes it.

    ``src/cat/risk.py`` sizes with ``min(qty_by_risk, qty_by_position, qty_by_exposure, qty_by_cash)``,
    so a first position is funded at *whatever headroom is left* even when that is below the
    per-position cap.  Reporting only ``floor(total_cap / position_cap)`` printed 0 and read as
    "no slot is fundable", which contradicts every order the loop has actually placed (D-037/F-045).
    """
    headroom = max(0.0, allocated_cap - deployed)
    first_slot_budget = min(position_cap, headroom)
    return {
        "fundable_by_total_cap": allocated_cap / position_cap,
        "slots_fundable_at_full_position_cap": min(max_open, int(allocated_cap / position_cap)),
        "configured_max_open": max_open,
        "open_positions": open_positions,
        "first_slot_notional_budget": first_slot_budget,
        "headroom_after_one_sized_down_slot": headroom - first_slot_budget,
        "second_slot_reachable": (headroom - first_slot_budget) > 0.0,
        "binding_cap_for_a_fresh_position": "total account allocation" if first_slot_budget < position_cap
                                            else "per-position cap",
        "inconsistency": (
            "total deployed cap is smaller than a single per-position cap, so the total cap always "
            "binds first: one position is funded at the whole remaining headroom and the next one "
            "sizes to zero, so max_open_positions is unreachable"
        ),
    }


def cadence_ceiling(*, trades: Sequence[Mapping[str, Any]], positions: Sequence[Mapping[str, Any]],
                    snapshot_ts: str, required: float) -> dict[str, Any]:
    """``1 slot / mean hold`` closed trades per day, estimated from every completed trade.

    The verdict needs the whole band, not the mean alone: one trade can clear the requirement while
    another does not, and then the sample — not the arithmetic — is what refuses to answer.
    """
    rows: list[dict[str, Any]] = []
    for trade in trades:
        entry_ts, exit_ts = trade.get("entry_ts"), trade.get("exit_ts")
        if entry_ts is None or exit_ts is None:
            continue
        hold_hours = (float(exit_ts) - float(entry_ts)) / 3600.0
        if hold_hours <= 0:
            continue
        rows.append({"symbol": trade.get("symbol"), "exit_ts": int(exit_ts),
                     "exit_reason": trade.get("reason"), "hold_hours": round(hold_hours, 4),
                     "ceiling_one_slot_per_day": round(24.0 / hold_hours, 4)})

    trade_ceilings = [row["ceiling_one_slot_per_day"] for row in rows]
    source: dict[str, Any] = {"kind": "closed_trades" if rows else "no_closed_trade",
                              "closed_trades": len(rows), "censored": False}
    if positions:
        age = (datetime.fromisoformat(snapshot_ts)
               - datetime.fromisoformat(positions[0]["opened_at"])).total_seconds() / 3600.0
        source["open_position_symbol"] = positions[0]["symbol"]
        source["open_position_age_hours"] = round(age, 3)

    payload: dict[str, Any] = {
        "required_per_day": required,
        "hold_source": source,
        "per_trade": rows,
        "max_hold_days_to_clear_with_one_slot": round(1.0 / required, 4),
        "max_hold_hours_to_clear_with_one_slot": round(24.0 / required, 3),
        "estimator": "mean hold over every closed trade; verdict needs the whole per-trade band",
        "note": ("a censored open-position age is reported but never sets the ceiling (F-039); "
                 "a single newest trade never sets it either (D-037)"),
    }
    if not rows:
        payload.update({"hold_hours": None, "ceiling_one_slot_per_day": None,
                        "per_trade_band": None, "band_straddles_requirement": None,
                        "one_slot_clears_requirement": None})
        return payload

    hold_hours = sum(row["hold_hours"] for row in rows) / len(rows)
    band_min, band_max = min(trade_ceilings), max(trade_ceilings)
    straddles = band_min < required <= band_max
    payload.update({
        "hold_hours": round(hold_hours, 4),
        "ceiling_one_slot_per_day": round(24.0 / hold_hours, 4),
        "per_trade_band": {"min": band_min, "max": band_max},
        "band_straddles_requirement": straddles,
        "one_slot_clears_requirement": None if straddles else band_min >= required,
    })
    return payload


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config(root=ROOT)
    lim = cfg.limits
    allocated, cap_pct = cfg.allocated_capital, lim.max_account_allocation_pct
    pos_pct, risk_pct = lim.max_position_pct_of_allocated_capital, lim.max_risk_per_trade_pct
    max_open = cfg.max_open_positions

    allocated_cap = allocated * cap_pct / 100.0
    position_cap = allocated * pos_pct / 100.0
    risk_budget = allocated * risk_pct / 100.0

    print(f"allocated_capital        = {allocated}")
    print(f"max_account_allocation%  = {cap_pct}  -> total deployed cap = {allocated_cap}")
    print(f"max_position_pct         = {pos_pct}  -> per-position cap   = {position_cap}")
    print(f"max_risk_per_trade%      = {risk_pct}  -> risk budget        = {risk_budget}")
    print(f"max_open_positions       = {max_open}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    snap = conn.execute(
        "SELECT ts, equity, cash, exposure_pct, positions_json FROM portfolio_snapshots "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    positions = json.loads(snap["positions_json"] or "[]")
    deployed = sum(p["qty"] * p["avg_entry"] for p in positions)
    remaining = allocated_cap - deployed

    slots = slot_arithmetic(allocated_cap=allocated_cap, position_cap=position_cap,
                            deployed=deployed, open_positions=len(positions), max_open=max_open)
    print(f"\ntotal_cap / per_position_cap = {slots['fundable_by_total_cap']:g}")
    print(f"=> slots fundable at the FULL per-position cap = {slots['slots_fundable_at_full_position_cap']}"
          f" (of {max_open} configured)")
    print(f"=> a fresh position is sized by the {slots['binding_cap_for_a_fresh_position']}: "
          f"{slots['first_slot_notional_budget']:.6f} USDT; headroom left after it = "
          f"{slots['headroom_after_one_sized_down_slot']:.6f}")
    print(f"=> second slot reachable = {slots['second_slot_reachable']}")

    print(f"\nlast snapshot {snap['ts']}: equity={snap['equity']} exposure_pct={snap['exposure_pct']}")
    print(f"open positions = {len(positions)}; deployed at entry = {deployed:.6f}")
    print(f"remaining exposure headroom = {remaining:.6f}")

    # D-035: a rejected signal is not one event.  The loop rejected BNB four times for two different
    # reasons, and only the sizing one is evidence about the allocation cap — so list them all and
    # name the sizing one explicitly instead of letting "the latest" stand in for it.
    rejections = [dict(row) for row in conn.execute(
        "SELECT ts, symbol, action, rationale_json FROM decisions "
        "WHERE symbol='BNB_USDT' AND action NOT IN ('NO_TRADE','HOLD') AND approved = 0 ORDER BY ts")]
    size_rejection = next((row for row in reversed(rejections)
                           if "rounds to zero" in str(row["rationale_json"])), None)
    print(f"\nBNB rejections: {len(rejections)}")
    for row in rejections:
        print(f"  {row['ts']} :: {row['rationale_json']}")

    # D-037: EVERY closed trade, no ORDER BY, no LIMIT — the estimator must not depend on which
    # trade happened to close last.
    trades = [dict(row) for row in conn.execute(
        "SELECT symbol, entry_ts, exit_ts, reason FROM trades WHERE exit_ts IS NOT NULL")]
    gate_b = json.loads(GATE_B.read_text(encoding="utf-8"))
    required = float(gate_b["cadence"]["required_trades_per_day"])
    ceiling = cadence_ceiling(trades=trades, positions=positions, snapshot_ts=snap["ts"],
                              required=required)
    conn.close()

    print(f"\nrequired closed trades/day (gate B) = {required} (read from the gate-B artifact)")
    print(f"holding time measured from          = {ceiling['hold_source']['kind']} "
          f"({ceiling['hold_source']['closed_trades']} closed trade(s))")
    for row in ceiling["per_trade"]:
        print(f"  {row['symbol']:<12} exit={row['exit_ts']} hold={row['hold_hours']:>8} h "
              f"ceiling={row['ceiling_one_slot_per_day']:>7} /day  reason={row['exit_reason']}")
    band = ceiling["per_trade_band"]
    print(f"mean hold                          = {ceiling['hold_hours']} h")
    print(f"ceiling at the mean hold (1 slot)  = {ceiling['ceiling_one_slot_per_day']} /day")
    if band:
        print(f"per-trade band                     = [{band['min']}, {band['max']}] /day "
              f"straddles {required}: {ceiling['band_straddles_requirement']}")
    print(f"clears requirement?                  = {ceiling['one_slot_clears_requirement']}")
    print(f"1 slot clears {required}/day only if mean hold <= "
          f"{ceiling['max_hold_days_to_clear_with_one_slot']} d "
          f"({ceiling['max_hold_hours_to_clear_with_one_slot']} h)")

    summary = {
        "config": {"allocated_capital": allocated, "max_account_allocation_pct": cap_pct,
                   "max_position_pct_of_allocated_capital": pos_pct,
                   "max_risk_per_trade_pct": risk_pct, "max_open_positions": max_open},
        "caps": {"total_deployed_cap": allocated_cap, "per_position_cap": position_cap,
                 "risk_budget": risk_budget},
        "slots": slots,
        "live": {"snapshot_ts": snap["ts"], "open_positions": len(positions),
                 "deployed_at_entry": round(deployed, 6),
                 "remaining_headroom": round(remaining, 6), "exposure_pct": snap["exposure_pct"]},
        "bnb_rejections": {"count": len(rejections),
                           "rationales": {str(row["ts"]): str(row["rationale_json"])
                                          for row in rejections},
                           "sizing_rejection": ({"ts": size_rejection["ts"],
                                                 "rationale": str(size_rejection["rationale_json"])}
                                                if size_rejection else None),
                           "note": ("only the sizing rejection is evidence about the allocation cap; a "
                                    "cooldown rejection is evidence about the loss that closed the slot")},
        "cadence_ceiling": ceiling,
    }
    (OUT / "slot_reachability.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False),
                                                encoding="utf-8")
    print(f"\nwrote {OUT / 'slot_reachability.json'}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
