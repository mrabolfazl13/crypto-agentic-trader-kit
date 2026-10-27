"""Read-only: how thin is the per-symbol evidence behind every stored walk-forward verdict?

The acceptance gates count trades across the whole basket, so a fold can clear
`min_trades` while each individual symbol contributes a handful of trades. This
prints, for every stored nested walk-forward run, the trade count per fold
against the number of symbols that were searched — the number that decides
whether the next hypothesis should be about symbol selection or about running
fewer, deeper combinations.

Nothing here changes a verdict; it only measures what is already on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "state" / "quant" / "results" / "nested_walk_forward"


def main() -> None:
    rows = []
    for path in sorted(RESULTS.glob("nested-wf-*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        aggregate = run.get("aggregate") or {}
        folds = run.get("folds") or []
        if not folds:
            continue
        symbols = int(folds[0].get("symbols_evaluated") or 0)
        per_fold = [int(fold.get("trades") or 0) for fold in folds]
        trades = int(aggregate.get("trades") or sum(per_fold))
        stem = path.stem.removeprefix("nested-wf-").split("-")
        rows.append({
            "run": run.get("run_id"),
            "strategy": stem[0],
            "timeframe": stem[1],
            "search_symbols": int(run.get("protocol", {}).get("search_symbols") or 0),
            "symbols": symbols,
            "folds": len(folds),
            "fold_trades": per_fold,
            "trades": trades,
            "trades_per_symbol": round(trades / symbols, 2) if symbols else None,
            "positive_fold_pct": aggregate.get("positive_fold_pct"),
            "net_sum_pct": aggregate.get("net_return_pct_sum"),
            "profit_factor_mean": aggregate.get("profit_factor_mean"),
            "decision": (run.get("decision") or {}).get("decision"),
        })

    for row in rows:
        print(f"{row['strategy']:>15}/{row['timeframe']:<3} "
              f"search={row['search_symbols']:>2}/scored={row['symbols']:>3} "
              f"fold_trades={str(row['fold_trades']):<24} "
              f"trades/symbol={row['trades_per_symbol']:>5} "
              f"pf={row['profit_factor_mean']} pos_folds={row['positive_fold_pct']}% "
              f"net={row['net_sum_pct']} -> {row['decision']}")

    usable = [r for r in rows if r["trades_per_symbol"] is not None]
    if usable:
        worst = min(usable, key=lambda r: r["trades_per_symbol"])
        best = max(usable, key=lambda r: r["trades_per_symbol"])
        print(f"\nbreadth: {best['strategy']}/{best['timeframe']} reaches "
              f"{best['trades_per_symbol']} trades per symbol; {worst['strategy']}/"
              f"{worst['timeframe']} only {worst['trades_per_symbol']}. "
              f"min_trades=30 is a basket-wide count, so per-symbol evidence is far thinner.")


if __name__ == "__main__":
    main()
