"""D-029 probe: is the reward_risk gate a selection filter or a volatility floor?

Read-only. Two independent pieces of evidence:

  (a) the algebraic identity, checked exactly against the deployed config;
  (b) the gate's selectivity, measured on the real per-symbol ATR values that the
      D-028 probe already fetched from the venue (100 symbols, 4h, 300 bars).

The deployed `decisions` table does not record atr_pct/spread_pct, so the gate
cannot be replayed row-by-row from the ledger. That limitation is reported, not
worked around.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402

D028 = Path("state/quant/results/d028_precision/unevaluability.json")
OUT = Path("state/quant/results/d029_reward_risk")
OUT.mkdir(parents=True, exist_ok=True)

cfg = load_config()
p = cfg.strategy_params
STOP, TARGET = p.atr_stop_mult, p.atr_target_mult
MIN_RR = cfg.limits.min_reward_risk_ratio
CAP = cfg.limits.max_slippage_pct
MAKER, TAKER = cfg.maker_fee_pct, cfg.taker_fee_pct

# rr = (TARGET*A - C) / (STOP*A + C) >= MIN_RR  <=>  A >= K*C
K = (1.0 + MIN_RR) / (TARGET - MIN_RR * STOP)

# Cost bounds implied by the deployed code, in percent units.
#   slippage = min(CAP, spread/2 + 0.02)  -> floor 0.02, ceiling CAP
#   C = (MAKER + TAKER + 2*slippage) / 100
C_MIN = (MAKER + TAKER + 2 * 0.02) / 100.0
C_MAX = (MAKER + TAKER + 2 * CAP) / 100.0
A_MIN = K * C_MIN * 100.0  # in atr_pct units
A_MAX = K * C_MAX * 100.0

print(f"stop={STOP} target={TARGET} min_rr={MIN_RR} -> A >= {K:g} * C")
print(f"round-trip cost C in [{C_MIN*100:.4f}%, {C_MAX*100:.4f}%]")
print(f"=> reward_risk admits atr_pct >= [{A_MIN:.4f}%, {A_MAX:.4f}%]")

# --- (b) selectivity on the real ATR values from D-028 -----------------------
d028 = json.loads(D028.read_text(encoding="utf-8"))
census = d028["basket_census"]["per_symbol"]
measured = [s for s in census if s.get("atr_pct") is not None]
print(f"\nD-028 basket: {len(census)} symbols, {len(measured)} with measured atr_pct")

def share_at_least(threshold):
    ok = [s for s in measured if s["atr_pct"] >= threshold]
    return len(ok), len(ok) / len(measured) * 100.0, ok

n_min, pct_min, ok_min = share_at_least(A_MIN)
n_max, pct_max, ok_max = share_at_least(A_MAX)
print(f"atr_pct >= {A_MIN:.4f}% (most permissive cost): {n_min}/{len(measured)} = {pct_min:.2f}%")
print(f"atr_pct >= {A_MAX:.4f}% (most expensive cost):  {n_max}/{len(measured)} = {pct_max:.2f}%")

# H2: the gate is one-sided -- it only ever admits high-volatility names.
# Test: is the admitted set a strict upper tail (no low-vol name slips through)?
admitted = sorted(ok_min, key=lambda s: s["atr_pct"])
rejected = [s for s in measured if s["atr_pct"] < A_MIN]
print(f"\nadmitted (permissive bound): {len(admitted)}; rejected: {len(rejected)}")
if admitted and rejected:
    print(f"  min admitted atr_pct = {admitted[0]['atr_pct']:.4f}%")
    print(f"  max rejected atr_pct = {max(s['atr_pct'] for s in rejected):.4f}%")
    overlap = [s for s in rejected if s["atr_pct"] >= A_MIN]
    print(f"  rejected rows above the permissive threshold (must be 0): {len(overlap)}")

# The four D-028 symbols, specifically (H3).
four = ["PEPE_USDT", "SHIB_USDT", "BTT_USDT", "HTX_USDT"]
print("\n-- the four D-028 symbols under the reward_risk gate --")
four_rows = []
for sym in four:
    s = next((x for x in measured if x["symbol"] == sym), None)
    if s is None:
        continue
    a = s["atr_pct"]
    passes_min = a >= A_MIN
    passes_max = a >= A_MAX
    four_rows.append(
        {
            "symbol": sym,
            "atr_pct": a,
            "passes_permissive": passes_min,
            "passes_expensive": passes_max,
        }
    )
    print(f"  {sym:12s} atr_pct={a:7.4f}%  permissive={passes_min}  expensive={passes_max}")

summary = {
    "config": {
        "atr_stop_mult": STOP,
        "atr_target_mult": TARGET,
        "min_reward_risk_ratio": MIN_RR,
        "max_slippage_pct": CAP,
        "maker_fee_pct": MAKER,
        "taker_fee_pct": TAKER,
    },
    "algebra": {
        "identity": "rr >= min_rr  <=>  atr_pct >= K * round_trip_cost",
        "K": K,
        "cost_pct_bounds": [round(C_MIN * 100, 6), round(C_MAX * 100, 6)],
        "atr_pct_threshold_bounds": [round(A_MIN, 6), round(A_MAX, 6)],
    },
    "selectivity": {
        "symbols_measured": len(measured),
        "admitted_at_permissive": n_min,
        "admitted_at_permissive_pct": round(pct_min, 4),
        "admitted_at_expensive": n_max,
        "admitted_at_expensive_pct": round(pct_max, 4),
    },
    "four_d028_symbols": four_rows,
    "ledger_limitation": (
        "decisions table stores no atr_pct/spread_pct and never records 'reward_risk' "
        "as a reason in 2100 rows, so H1 cannot be replayed row-by-row from the ledger"
    ),
}
(OUT / "reward_risk_gate.json").write_text(
    json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
)
print(f"\nwrote {OUT / 'reward_risk_gate.json'}")
