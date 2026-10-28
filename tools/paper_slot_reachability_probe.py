"""D-030 probe: is the second open-position slot reachable, and what does that do to cadence?

Read-only. Two questions:

  1. The deployed config says max_open_positions = 2, but the sizing path caps TOTAL
     deployed value at max_account_allocation_pct. Is the second slot fundable at all?
  2. If only one slot is reachable, what is the maximum closed-trade cadence, and does
     it clear gate B's required rate?
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

bnb = conn.execute(
    "SELECT ts, symbol, rationale_json FROM decisions "
    "WHERE symbol='BNB_USDT' AND action NOT IN ('NO_TRADE','HOLD') ORDER BY ts DESC LIMIT 1"
).fetchone()
print(f"\nBNB decision: {bnb['ts']} :: {bnb['rationale_json']}")

# --- cadence ceiling with only one reachable slot ---------------------------
opened = datetime.fromisoformat(positions[0]["opened_at"])
now = datetime.fromisoformat(snap["ts"])
held_hours = (now - opened).total_seconds() / 3600.0
held_days = held_hours / 24.0
print(f"\nASTER held {held_hours:.2f} h ({held_days:.3f} d) and still open")

# Gate B needs a closed-trade rate. With N slots and a mean hold of H days,
# the ceiling is N / H closed trades per day.
REQUIRED = 1.1768  # from the gate-B cadence block, snapshot-derived
ceiling_1slot = 1.0 / held_days if held_days > 0 else None
print(f"required closed trades/day (gate B) = {REQUIRED}")
print(f"ceiling with 1 slot at this hold     = {ceiling_1slot:.4f}/day")
print(f"clears requirement?                  = {ceiling_1slot >= REQUIRED}")

# What hold would 1 slot need to clear it?
max_hold_for_requirement = 1.0 / REQUIRED
print(f"1 slot clears {REQUIRED}/day only if mean hold <= {max_hold_for_requirement:.4f} d "
      f"({max_hold_for_requirement * 24:.2f} h)")

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
    "bnb_rejection": bnb["rationale_json"],
    "cadence_ceiling": {
        "required_per_day": REQUIRED,
        "aster_held_hours": round(held_hours, 3),
        "aster_held_days": round(held_days, 4),
        "ceiling_one_slot_per_day": round(ceiling_1slot, 4),
        "one_slot_clears_requirement": ceiling_1slot >= REQUIRED,
        "max_hold_days_to_clear_with_one_slot": round(max_hold_for_requirement, 4),
    },
}
(OUT / "slot_reachability.json").write_text(
    json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
)
print(f"\nwrote {OUT / 'slot_reachability.json'}")
