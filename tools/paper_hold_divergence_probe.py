"""D-032 probe: the deployed loop has no time exit; the backtest has a 24h one.

Read-only. D-031 found the 4h hold distribution is censored at
`quant.backtest.max_hold_hours` (24). This asks whether the DEPLOYED path has that
same exit at all.

  backtest  sim.py:120  max_hold_bars = max(1, round(max_hold_hours * 60))
            sim.py:197  last = min(start + max_hold_bars - 1, ...)
            -> applied unconditionally

  deployed  strategy.py:253  if p.max_hold_bars > 0 and horizon >= p.max_hold_bars
            strategies.py:51  max_hold_bars: int = 0
            config.yaml       max_hold_bars absent -> 0
            -> never fires

If that holds, every backtest number was measured on a system with a time exit the
deployment does not have. Nothing here changes config, thresholds or strategy.py.
"""

import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

OUT = Path("state/quant/results/d032_hold_divergence")
OUT.mkdir(parents=True, exist_ok=True)

from cat.config import load_config  # noqa: E402
from cat.strategies import StrategyParams  # noqa: E402

cfg = load_config()
deployed_bars = cfg.strategy_params.max_hold_bars
backtest_hours = cfg.raw["quant"]["backtest"]["max_hold_hours"]
config_text = Path("config.yaml").read_text(encoding="utf-8")
default_bars = StrategyParams.__dataclass_fields__["max_hold_bars"].default

print("=== H1: the two paths ===")
print(f"deployed strategy_params.max_hold_bars = {deployed_bars}")
print(f"backtest quant.backtest.max_hold_hours  = {backtest_hours}")
print(f"StrategyParams default                  = {default_bars}")
print(f"'max_hold_bars' present in config.yaml  = {'max_hold_bars' in config_text}")
print(f"'max_hold_hours' present in config.yaml = {'max_hold_hours' in config_text}")
h1 = (
    deployed_bars == 0
    and backtest_hours == 24
    and default_bars == 0
    and "max_hold_bars" not in config_text
)
print(f"H1 confirmed: {h1}")

print("\n=== H2: does the backtest cap actually bind? ===")
rows = []
for p in sorted(glob.glob("state/quant/results/**/*trades*.json", recursive=True)):
    name = Path(p).name
    if "-4h.trades" not in name:
        continue
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    for v in (d.get("folds") or {}).values():
        if isinstance(v, list):
            rows.extend(t for t in v if t.get("entry_ts") is not None)

by_reason = {}
for r in rows:
    by_reason.setdefault(r.get("reason"), []).append(r)

print(f"4h trades: {len(rows)}")
print(f"{'reason':8s} {'n':>6s} {'share%':>8s} {'net':>11s} {'win%':>7s} {'median_hold_d':>14s}")
reason_rows = []
for reason, sub in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
    holds = sorted((t["exit_ts"] - t["entry_ts"]) / 86400.0 for t in sub)
    net = sum(t.get("net", 0.0) for t in sub)
    wins = sum(1 for t in sub if t.get("net", 0.0) > 0)
    med = statistics.median(holds)
    reason_rows.append(
        {
            "reason": reason,
            "n": len(sub),
            "share_pct": round(len(sub) / len(rows) * 100, 4),
            "net": round(net, 4),
            "win_rate_pct": round(wins / len(sub) * 100, 4),
            "median_hold_days": round(med, 4),
            "max_hold_days": round(holds[-1], 4),
        }
    )
    print(
        f"{reason:8s} {len(sub):6d} {len(sub)/len(rows)*100:8.2f} {net:11.2f} "
        f"{wins/len(sub)*100:7.2f} {med:14.4f}"
    )

time_share = len(by_reason.get("time", [])) / len(rows) * 100
h2 = time_share > 0
print(f"\ntime-exit share = {time_share:.2f}%  -> H2 confirmed: {h2}")

# Are the time exits actually AT the cap (i.e. the cap binds, not just exists)?
time_holds = sorted((t["exit_ts"] - t["entry_ts"]) / 86400.0 for t in by_reason.get("time", []))
at_cap = sum(1 for h in time_holds if h >= 0.99)
print(f"time exits at/after 0.99 d: {at_cap}/{len(time_holds)} ({at_cap/len(time_holds)*100:.2f}%)")

print("\n=== H3: direction of the error ===")
h3 = time_share > 40.0
print(f"time share {time_share:.2f}% > 40% -> H3 confirmed: {h3}")
print("deployed has no cap => deployed holds >= backtest holds")
print("=> the D-031 ceiling of 1.4891/day is an UPPER bound for a system that does not exist")

print("\n=== H4: live evidence ===")
entry = datetime(2026, 10, 27, 18, 33, 25, tzinfo=timezone.utc)
snap = datetime(2026, 10, 28, 18, 37, 53, tzinfo=timezone.utc)
age_h = (snap - entry).total_seconds() / 3600.0
h4 = age_h > backtest_hours
print(f"ASTER age at snapshot = {age_h:.4f} h ({age_h/24:.4f} d)")
print(f"backtest cap = {backtest_hours} h -> exceeded by {age_h - backtest_hours:.4f} h")
print(f"H4 confirmed: {h4}")

verdict = h1 and h2 and h3 and h4
summary = {
    "h1_divergence_real": h1,
    "h2_cap_binds": h2,
    "h3_direction_against_gate_b": h3,
    "h4_live_evidence": h4,
    "verdict": "validation-divergence" if verdict else "not-confirmed",
    "deployed": {
        "max_hold_bars": deployed_bars,
        "default": default_bars,
        "present_in_config_yaml": "max_hold_bars" in config_text,
        "time_exit_can_fire": deployed_bars > 0,
    },
    "backtest": {
        "max_hold_hours": backtest_hours,
        "max_hold_minutes": max(1, round(backtest_hours * 60)),
        "applied_unconditionally": True,
    },
    "by_reason": reason_rows,
    "time_exit_share_pct": round(time_share, 4),
    "time_exits_at_cap_pct": round(at_cap / len(time_holds) * 100, 4) if time_holds else 0.0,
    "live": {
        "symbol": "ASTER_USDT",
        "entry": entry.isoformat(),
        "snapshot": snap.isoformat(),
        "age_hours": round(age_h, 4),
        "exceeded_cap_hours": round(age_h - backtest_hours, 4),
        "backtest_would_have_closed": age_h >= backtest_hours,
    },
    "note": (
        "read-only; config.yaml, thresholds, symbols, K, timeframe and strategy.py "
        "untouched; adding a time exit to deployment is an owner decision"
    ),
}
(OUT / "hold_divergence.json").write_text(
    json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
)
print(f"\nverdict: {summary['verdict']}")
print(f"wrote {OUT / 'hold_divergence.json'}")
