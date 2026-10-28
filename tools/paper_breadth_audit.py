"""How much of the replayed cadence can the deployed engine actually order?

The research lake and the deployed allowlist are two independently chosen 100-symbol lists. The
frequency replays (D-020/D-022/D-023) read the lake; the PAPER cycle can only order what
`config.yaml -> trading.symbols` approves. Every cadence number therefore describes an account
whose basket is wider than the one that runs, and the gap was only visible as a rejection string
in the artifacts.

Read-only: it re-times recorded trade timelines, splits them by the deployed allowlist and reports
what the same schedule would produce on the symbols the engine is actually permitted to touch.
It changes no list — breadth is the owner's lever, not the agent's.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import universe_symbols  # noqa: E402
from paper_frequency_replay import canonical_symbol, serial_account_schedule  # noqa: E402

DEFAULT_CELLS = tuple(f"freqprod_{window}_100_{tf}"
                      for window in ("w1", "w3") for tf in ("4h", "1h", "15m"))


def canonical_allowlist(config: Any) -> set[str]:
    return {symbol.replace("/", "_").upper() for symbol in config.symbols}


def universe_overlap(allow: set[str], lake: set[str]) -> dict[str, Any]:
    """The two lists the mission keeps conflating: what may be ordered vs what can be evidenced."""
    return {
        "allowlist": len(allow),
        "lake": len(lake),
        "both": len(allow & lake),
        "approved_without_lake_data": sorted(allow - lake),
        "in_lake_not_approved": sorted(lake - allow),
        "coverage_pct": round(100.0 * len(allow & lake) / len(allow), 2) if allow else 0.0,
    }


def split_candidates(rows: list[dict[str, Any]], allow: set[str]) -> tuple[list[dict], list[dict]]:
    """Trade timelines split by whether the deployed risk layer would accept their symbol."""
    approved, blocked = [], []
    for row in rows:
        for trade in row.get("trade_timeline", []):
            symbol = canonical_symbol(trade.get("symbol") or row["symbol"])
            (approved if symbol in allow else blocked).append(trade)
    return approved, blocked


def _sum(trades: list[dict]) -> dict[str, Any]:
    gross = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gain = sum(t["pnl"] for t in wins)
    loss = -sum(t["pnl"] for t in losses)
    return {"trades": len(trades), "net_pnl_per_1000": round(gross, 4),
            "fees": round(sum(t.get("fees", 0.0) for t in trades), 4),
            "profit_factor": (round(gain / loss, 4) if loss > 0 else None),
            "win_rate_pct": round(100.0 * len(wins) / len(trades), 2) if trades else None}


def _display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def audit_artifact(artifact: Path, *, allow: set[str], limits: Any,
                   concurrencies: tuple[int, ...] = (1, 2)) -> dict[str, Any]:
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    summary, rows = doc["summary"], doc["per_symbol"]
    measured = [row for row in rows if "closed_trades" in row]
    days = summary["counted_days_per_symbol"]
    approved, blocked = split_candidates(measured, allow)

    def schedule(trades: list[dict], k: int) -> dict[str, Any]:
        if not trades:
            return {"taken": 0, "trades_per_day": 0.0, "profit_factor": None,
                    "net_pnl_per_1000": 0.0}
        return serial_account_schedule([trades], window_days=days,
                                       max_trades_per_day=limits.max_trades_per_day,
                                       cooldown_minutes_after_loss=limits.cooldown_minutes_after_loss,
                                       max_concurrent=k)

    symbols_with_trades = {canonical_symbol(row["symbol"]) for row in measured if row.get("trade_timeline")}
    replayed = {canonical_symbol(row["symbol"]) for row in measured}
    refused = {key.split(" ")[0]: count for key, count in summary["rejections_pooled"].items()
               if key.endswith("not in approved symbol list")}
    return {
        "artifact": _display(artifact),
        "timeframe": summary["timeframe"],
        "cadence": summary["cadence"],
        "counted_days": days,
        "decision_ticks": summary["decision_bars_total"],
        "symbols_replayed": summary["symbols_replayed"],
        "symbols_with_trades": len(symbols_with_trades),
        "symbols_replayed_not_approved": len(replayed - allow),
        "symbols_rejected_by_risk_layer": len(refused),
        "risk_layer_rejections_are_unapproved_replayed_symbols": (
            set(refused) <= (replayed - allow)),
        "allowlist_refusal_ticks": sum(refused.values()),
        "allowlist_refusal_tick_pct": (round(100.0 * sum(refused.values())
                                             / summary["decision_bars_total"], 3)
                                       if summary["decision_bars_total"] else None),
        "candidates": {"all": _sum(approved + blocked), "approved": _sum(approved),
                       "not_approved": _sum(blocked)},
        "schedules": {
            f"{scope}_{k}": {key: schedule(trades, k)[key] for key in
                             ("taken", "trades_per_day", "profit_factor", "net_pnl_per_1000")}
            for scope, trades in (("all", approved + blocked), ("approved", approved))
            for k in concurrencies},
        "note": ("all = every replayed candidate, approved = only symbols the deployed allowlist "
                 "permits; no list was changed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="state/quant/results")
    parser.add_argument("--cells", default=",".join(DEFAULT_CELLS),
                        help="comma separated artifact folders, each holding frequency-<tf>.json")
    parser.add_argument("--out", default="state/quant/results/breadth_audit")
    parser.add_argument("--lake-symbols", type=int, default=100,
                        help="how many lake symbols the cadence cells were replayed over")
    args = parser.parse_args()

    config = load_config(root=ROOT)
    allow = canonical_allowlist(config)
    lake = {canonical_symbol(symbol) for symbol in
            universe_symbols(load_quant_settings(config), limit=args.lake_symbols)}
    overlap = universe_overlap(allow, lake)
    results = Path(args.results)
    if not results.is_absolute():
        results = ROOT / results

    rows: list[dict[str, Any]] = []
    for cell_name in [name.strip() for name in args.cells.split(",") if name.strip()]:
        folder = results / cell_name
        tf = cell_name.rsplit("_", 1)[-1]
        artifact = folder / f"frequency-{tf}.json"
        if not artifact.exists():
            print(f"missing artifact: {artifact.as_posix()}")
            continue
        try:
            rows.append(audit_artifact(artifact, allow=allow, limits=config.limits))
        except Exception as exc:  # one unreadable cell must not end the audit
            print(f"{cell_name}: unreadable ({type(exc).__name__}: {exc})")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe_overlap": overlap,
        "cells": rows,
        "note": ("the cadence cells were replayed over the lake and every one of their recorded "
                 "candidates is already allowlist-approved, so the cadence numbers are not "
                 "overstated; what the gap costs is visible in allowlist_refusal_ticks, the gate "
                 "passing moments the deployed engine was not allowed to order"),
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    (out / "breadth_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"deployed allowlist {overlap['allowlist']} symbols, research lake {overlap['lake']} symbols, "
          f"overlap {overlap['both']} ({overlap['coverage_pct']}% of the allowlist has lake data)")
    print(f"approved but no lake history (unverifiable by backtest): {len(overlap['approved_without_lake_data'])}")
    print(f"in the lake but not orderable: {len(overlap['in_lake_not_approved'])}\n")
    header = (f"{'cell':>22} {'tf':>4} {'days':>6} {'cand all/appr/blk':>18} "
              f"{'K1 all':>7} {'K1 appr':>8} "
              f"{'K2 all':>7} {'K2 appr':>8} {'refused sym':>12} {'refused ticks':>14}")
    print(header)
    for row in rows:
        sch, cand = row["schedules"], row["candidates"]
        counts = f"{cand['all']['trades']}/{cand['approved']['trades']}/{cand['not_approved']['trades']}"
        print(f"{Path(row['artifact']).parent.name:>22} {row['timeframe']:>4} {row['counted_days']:>6} "
              f"{counts:>18} {sch['all_1']['trades_per_day']:>7} {sch['approved_1']['trades_per_day']:>8} "
              f"{sch['all_2']['trades_per_day']:>7} {sch['approved_2']['trades_per_day']:>8} "
              f"{row['symbols_rejected_by_risk_layer']:>12} {row['allowlist_refusal_ticks']:>14}")
    print("\nper-cell candidate quality (profit factor, net per 1000):")
    for row in rows:
        for scope in ("all", "approved", "not_approved"):
            c = row["candidates"][scope]
            print(f"  {Path(row['artifact']).parent.name:>22} {scope:>12}: "
                  f"trades {c['trades']:>4} PF {c['profit_factor'] if c['profit_factor'] is not None else '-':>7} "
                  f"net {c['net_pnl_per_1000']:>9} fees {c['fees']:>7}")
    print(f"\nwritten: {(out / 'breadth_audit.json').as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
