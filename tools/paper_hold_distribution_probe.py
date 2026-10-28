"""D-031 probe: the real hold-time distribution for 4h strategies, and the one-slot ceiling.

Read-only. D-030's H2 rested on a single CENSORED observation (ASTER still open, so
1.0031 d is a lower bound on its eventual hold). This replaces it with the measured
hold-time distribution from the repo's own 4h backtests, which use the same ATR
stop/target mechanics as the deployed loop.

Hold time is taken from exit_ts - entry_ts (epoch seconds), which is unambiguous.
The ``bars`` field is NOT a bar count: it is minutes elapsed (inclusive), so it is
used only as a cross-check, never as the primary measure.

The measured distribution is itself right-censored: `quant.backtest.max_hold_hours`
is 24, so ~46% of trades close exactly at the cap. The pooled mean is therefore a
LOWER bound on true hold time and the one-slot ceiling is an UPPER bound. The
deployed strategy has `max_hold_bars = 0` (no time exit), so the censored trades
could run well past 24h. A censoring sensitivity is reported for that reason.
"""

import glob
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

OUT = Path("state/quant/results/d031_hold")
OUT.mkdir(parents=True, exist_ok=True)

REQUIRED_PER_DAY = 1.1768  # gate-B cadence requirement, from the snapshot

paths = sorted(glob.glob("state/quant/results/**/*trades*.json", recursive=True))
rows = []
all_holds = []
bars_check = []

for p in paths:
    name = Path(p).name
    tf = next((t for t in ("15m", "1h", "4h") if f"-{t}.trades" in name), None)
    if tf != "4h":
        continue
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    folds = d.get("folds") or {}
    trades = [t for v in folds.values() if isinstance(v, list) for t in v]

    holds = []
    for t in trades:
        e, x = t.get("entry_ts"), t.get("exit_ts")
        if e is None or x is None:
            continue
        secs = float(x) - float(e)
        if secs <= 0:
            continue
        holds.append(secs / 86400.0)
        if t.get("bars") is not None:
            bars_check.append((float(t["bars"]) * 60.0, secs))
    if not holds:
        continue
    holds.sort()
    all_holds.extend(holds)
    rows.append(
        {
            "file": p,
            "strategy": name.split("-")[2] if "-" in name else name,
            "timeframe": tf,
            "trades": len(holds),
            "median_days": round(statistics.median(holds), 4),
            "mean_days": round(statistics.fmean(holds), 4),
            "p25_days": round(holds[int(0.25 * (len(holds) - 1))], 4),
            "p75_days": round(holds[int(0.75 * (len(holds) - 1))], 4),
            "min_days": round(holds[0], 4),
            "max_days": round(holds[-1], 4),
        }
    )

print(f"4h trade dumps with hold data: {len(rows)}\n")
print(f"{'strategy':12s} {'trades':>6s} {'median_d':>9s} {'mean_d':>8s} {'p25':>7s} {'p75':>7s} {'max':>7s}")
for r in rows:
    print(
        f"{r['strategy']:12s} {r['trades']:6d} {r['median_days']:9.4f} "
        f"{r['mean_days']:8.4f} {r['p25_days']:7.4f} {r['p75_days']:7.4f} {r['max_days']:7.4f}"
    )

if bars_check:
    ratios = [b / s for b, s in bars_check if s > 0]
    print(f"\nbars*60 / elapsed_secs: median={statistics.median(ratios):.4f} (1.0 => bars is minutes)")

all_holds.sort()
pooled_median = statistics.median(all_holds)
pooled_mean = statistics.fmean(all_holds)
print(f"\npooled: n={len(all_holds)} median={pooled_median:.4f} d mean={pooled_mean:.4f} d")


def ceiling(hold_days):
    return 1.0 / hold_days if hold_days > 0 else None


print(f"\nrequired by gate B: {REQUIRED_PER_DAY}/day")
for label, h in (
    ("pooled median", pooled_median),
    ("pooled mean", pooled_mean),
    ("fastest strategy median", min(r["median_days"] for r in rows)),
    ("p25 of pooled", all_holds[int(0.25 * (len(all_holds) - 1))]),
):
    c = ceiling(h)
    print(f"  {label:26s} hold={h:7.4f} d -> ceiling={c:7.4f}/day  clears={c >= REQUIRED_PER_DAY}")

max_hold = 1.0 / REQUIRED_PER_DAY
print(f"\none slot clears {REQUIRED_PER_DAY}/day only if mean hold <= {max_hold:.4f} d ({max_hold*24:.2f} h)")
share_faster = sum(1 for h in all_holds if h <= max_hold) / len(all_holds) * 100.0
print(f"share of measured 4h trades that closed within that hold: {share_faster:.2f}%")

# --- censoring sensitivity -------------------------------------------------
CENSOR_AT = 0.99
unc = [h for h in all_holds if h < CENSOR_AT]
cen = [h for h in all_holds if h >= CENSOR_AT]
unc_mean = statistics.fmean(unc) if unc else 0.0
n_u, n_c, N = len(unc), len(cen), len(all_holds)
max_overall_mean = 1.0 / REQUIRED_PER_DAY
break_even_censored = ((max_overall_mean * N - n_u * unc_mean) / n_c) if n_c else float("nan")

sensitivity = []
for h_c in (0.9993, 1.0, 1.2, 1.3839, 1.5, 2.0, 3.0):
    m = (n_u * unc_mean + n_c * h_c) / N
    sensitivity.append(
        {
            "censored_mean_days": h_c,
            "overall_mean_days": round(m, 4),
            "ceiling_per_day": round(1.0 / m, 4),
            "clears": (1.0 / m) >= REQUIRED_PER_DAY,
        }
    )

print(f"\ncensoring: {n_c}/{N} trades ({n_c / N * 100:.2f}%) close at the 24h backtest cap")
print(f"  uncensored mean={unc_mean:.4f} d (n={n_u})")
print(f"  break-even censored mean = {break_even_censored:.4f} d ({break_even_censored * 24:.2f} h)")
print("  => verdict is INDETERMINATE: the ceiling is an upper bound, and whether one")
print("     slot clears gate B depends on how long the censored half actually runs.")

summary = {
    "required_per_day": REQUIRED_PER_DAY,
    "dumps": rows,
    "pooled": {
        "n": len(all_holds),
        "median_days": round(pooled_median, 4),
        "mean_days": round(pooled_mean, 4),
        "p25_days": round(all_holds[int(0.25 * (len(all_holds) - 1))], 4),
        "p75_days": round(all_holds[int(0.75 * (len(all_holds) - 1))], 4),
        "min_days": round(all_holds[0], 4),
        "max_days": round(all_holds[-1], 4),
    },
    "censoring": {
        "censored_at_days": CENSOR_AT,
        "n_uncensored": n_u,
        "n_censored": n_c,
        "uncensored_mean_days": round(unc_mean, 4),
        "break_even_censored_mean_days": round(break_even_censored, 4),
        "sensitivity": sensitivity,
        "verdict": "indeterminate: pooled mean is a lower bound, ceiling an upper bound",
    },
    "one_slot_ceiling": {
        "at_pooled_median_per_day": round(ceiling(pooled_median), 4),
        "at_pooled_mean_per_day": round(ceiling(pooled_mean), 4),
        "clears_requirement_at_median": ceiling(pooled_median) >= REQUIRED_PER_DAY,
        "clears_requirement_at_mean": ceiling(pooled_mean) >= REQUIRED_PER_DAY,
        "max_hold_days_to_clear": round(max_hold, 4),
        "share_of_trades_faster_than_that_pct": round(share_faster, 4),
    },
    "bars_field_note": (
        "bars is minutes elapsed (inclusive), not a bar count; verified against "
        "entry_ts/exit_ts and used only as a cross-check"
    ),
}
(OUT / "hold_distribution.json").write_text(
    json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
)
print(f"\nwrote {OUT / 'hold_distribution.json'}")
