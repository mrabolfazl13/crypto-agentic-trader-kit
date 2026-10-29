"""D-030 probe: is the second open-position slot reachable, and what does that do to cadence?

Read-only. Two questions:

  1. The deployed config says max_open_positions = 2, but the sizing path caps TOTAL
     deployed value at max_account_allocation_pct. Is the second slot fundable at all?
  2. If only one slot is reachable, what is the maximum closed-trade cadence, and does
     it clear gate B's required rate?

The ceiling is a function of the *holding time of a completed trade*; the age of whatever position is
open when the snapshot was taken is a censored lower bound and is recorded separately (D-035/F-039 —
using it as a hold time made the ceiling jump above the requirement the moment a fresh position
opened).  With no closed trade yet, the ceiling is reported as not measurable.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402

OUT = Path("state/quant/results/d030_slots")
OUT.mkdir(parents=True, exist_ok=True)

cfg = load_config()
lim = cfg.limits
allocated = cfg.allocated_capital
cap_pct = lim.max_account_allocation_pct
pos_pct = lim.max_position_pct_of_allocated_capital
risk_pct = lim.max_risk_per_trade_pct
max_open = cfg.max_open_positions

allocated_cap = allocated * cap_pct / 100.0
position_cap = allocated * pos_pct / 100.0
risk_budget = allocated * risk_pct / 100.0

print(f"allocated_capital        = {allocated}")
print(f"max_account_allocation%  = {cap_pct}  -> total deployed cap = {allocated_cap}")
print(f"max_position_pct         = {pos_pct}  -> per-position cap   = {position_cap}")
print(f"max_risk_per_trade%      = {risk_pct}  -> risk budget        = {risk_budget}")
print(f"max_open_positions       = {max_open}")

slots_by_cap = allocated_cap / position_cap
print(f"\ntotal_cap / per_position_cap = {slots_by_cap:g}")
print(f"config claims max_open_positions = {max_open}")
reachable = min(max_open, int(slots_by_cap))
print(f"=> reachable slots = {reachable}")

DB = "state/reports/server_platform_snapshot.sqlite3"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
snap = conn.execute(
    "SELECT ts, equity, cash, exposure_pct, positions_json FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
).fetchone()
positions = json.loads(snap["positions_json"] or "[]")
deployed = sum(p["qty"] * p["avg_entry"] for p in positions)
remaining = allocated_cap - deployed
print(f"\nlast snapshot {snap['ts']}: equity={snap['equity']} exposure_pct={snap['exposure_pct']}")
print(f"open positions = {len(positions)}; deployed at entry = {deployed:.6f}")
print(f"remaining exposure headroom = {remaining:.6f}")

# D-035: a rejected signal is not one event.  The loop rejected BNB four times for two different
# reasons, and only the sizing one is evidence about the allocation cap — so list them all and name
# the sizing one explicitly instead of letting "the latest" stand in for it.
rejections = [dict(row) for row in conn.execute(
    "SELECT ts, symbol, action, rationale_json FROM decisions "
    "WHERE symbol='BNB_USDT' AND action NOT IN ('NO_TRADE','HOLD') AND approved = 0 ORDER BY ts")]
size_rejection = next((row for row in reversed(rejections) if "rounds to zero" in
                      str(row["rationale_json"])), None)
print(f"\nBNB rejections: {len(rejections)}")
for row in rejections:
    print(f"  {row['ts']} :: {row['rationale_json']}")

# --- cadence ceiling with only one reachable slot ---------------------------
# The ceiling needs a *holding time*, not the age of whatever happens to be open.  An open
# position's age is a censored observation (D-031/F-029): it is a lower bound, and turning it into
# "trades/day" inflates the ceiling the moment a fresh position is opened.  Prefer a closed trade;
# if none exists, say the ceiling is not measurable instead of guessing.
def cadence_ceiling(*, closed, positions, snapshot_ts: str, required: float) -> dict:
    """``1 slot / hold`` closed trades per day, measured only from a completed holding time."""
    hold_hours = None
    source: dict = {"kind": "no_closed_trade"}
    if closed is not None:
        hold_hours = (float(closed["exit_ts"]) - float(closed["entry_ts"])) / 3600.0
        source = {"kind": "closed_trade", "symbol": closed["symbol"], "exit_reason": closed["reason"],
                  "entry_ts": closed["entry_ts"], "exit_ts": closed["exit_ts"], "censored": False}
    if positions:
        age = (datetime.fromisoformat(snapshot_ts)
               - datetime.fromisoformat(positions[0]["opened_at"])).total_seconds() / 3600.0
        source["open_position_symbol"] = positions[0]["symbol"]
        source["open_position_age_hours"] = round(age, 3)
    ceiling = round(24.0 / hold_hours, 4) if hold_hours else None
    return {
        "required_per_day": required,
        "hold_hours": round(hold_hours, 3) if hold_hours else None,
        "hold_source": source,
        "ceiling_one_slot_per_day": ceiling,
        "one_slot_clears_requirement": None if ceiling is None else ceiling >= required,
        "max_hold_days_to_clear_with_one_slot": round(1.0 / required, 4),
        "max_hold_hours_to_clear_with_one_slot": round(24.0 / required, 3),
        "note": ("the open position's age is recorded as a censored lower bound and is never the "
                 "ceiling; with no closed trade the ceiling is not measurable"),
    }


closed = conn.execute(
    "SELECT symbol, entry_ts, exit_ts, reason FROM trades WHERE exit_ts IS NOT NULL "
    "ORDER BY exit_ts DESC LIMIT 1"
).fetchone()
gate_b = json.loads(Path("state/reports/gate_b_evidence_server.json").read_text(encoding="utf-8"))
REQUIRED = float(gate_b["cadence"]["required_trades_per_day"])
ceiling = cadence_ceiling(closed=closed, positions=positions, snapshot_ts=snap["ts"],
                          required=REQUIRED)
hold_hours, hold_source = ceiling["hold_hours"], ceiling["hold_source"]
ceiling_1slot, clears = ceiling["ceiling_one_slot_per_day"], ceiling["one_slot_clears_requirement"]
print(f"\nrequired closed trades/day (gate B) = {REQUIRED} (read from the gate-B artifact)")
print(f"holding time measured from          = {hold_source['kind']}: "
      f"{round(hold_hours, 4) if hold_hours else 'n/a'} h")
if hold_source.get("open_position_age_hours") is not None:
    print(f"(the open {hold_source['open_position_symbol']} has aged "
          f"{hold_source['open_position_age_hours']} h — censored, never used as a hold time)")
print(f"ceiling with 1 slot at that hold     = {ceiling_1slot if ceiling_1slot else 'not measurable'} /day")
print(f"clears requirement?                  = {clears}")

print(f"1 slot clears {REQUIRED}/day only if mean hold <= "
      f"{ceiling['max_hold_days_to_clear_with_one_slot']} d "
      f"({ceiling['max_hold_hours_to_clear_with_one_slot']} h)")

conn.close()

summary = {
    "config": {
        "allocated_capital": allocated,
        "max_account_allocation_pct": cap_pct,
        "max_position_pct_of_allocated_capital": pos_pct,
        "max_risk_per_trade_pct": risk_pct,
        "max_open_positions": max_open,
    },
    "caps": {
        "total_deployed_cap": allocated_cap,
        "per_position_cap": position_cap,
        "risk_budget": risk_budget,
    },
    "slots": {
        "fundable_by_total_cap": slots_by_cap,
        "configured_max_open": max_open,
        "reachable": reachable,
        "second_slot_reachable": reachable >= 2,
        "inconsistency": (
            "total deployed cap is smaller than a single per-position cap, so the "
            "total cap always binds first and max_open_positions is unreachable"
        ),
    },
    "live": {
        "snapshot_ts": snap["ts"],
        "open_positions": len(positions),
        "deployed_at_entry": round(deployed, 6),
        "remaining_headroom": round(remaining, 6),
        "exposure_pct": snap["exposure_pct"],
    },
    "bnb_rejections": {"count": len(rejections),
                       "rationales": {str(row["ts"]): str(row["rationale_json"]) for row in rejections},
                       "sizing_rejection": ({"ts": size_rejection["ts"],
                                             "rationale": str(size_rejection["rationale_json"])}
                                            if size_rejection else None),
                       "note": ("only the sizing rejection is evidence about the allocation cap; a "
                                "cooldown rejection is evidence about the loss that closed the slot")},
    "cadence_ceiling": ceiling,
}
(OUT / "slot_reachability.json").write_text(
    json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
)
print(f"\nwrote {OUT / 'slot_reachability.json'}")
