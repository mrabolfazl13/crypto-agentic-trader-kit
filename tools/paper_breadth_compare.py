"""Compare two sets of frequency replays that differ only in basket breadth.

D-022 measured cadence at the 100-symbol research universe; D-026 re-ran the same
protocol at the deployed allowlist universe. This tool reads the two summary JSONs
and answers one question in the unit the owner asked for: how much of the trade
count is bought by breadth, and what did profit factor and net do at the same time.

Nothing here replays data, changes a threshold, or judges Gate B — it only reads
artifacts already on disk, so the comparison cannot drift from the runs it reports.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

# Metrics whose ratio between two runs is the breadth effect.
RATE_FIELDS = ("pooled_trades_per_day", "serial_trades_per_day")
QUALITY_FIELDS = ("profit_factor", "net_pnl_per_1000", "win_rate_pct", "days_to_30_trades")
# Anything that must match before two rows may be compared at all.
LOCK_FIELDS = ("timeframe", "cadence", "window_bars", "counted_from", "counted_to",
               "counted_days_per_symbol", "production_cycle_hours", "classifier_key")


def _classifier_key(classifier: dict[str, Any]) -> str:
    """Identity of the decision code, so a replay with different params is not comparable."""
    return json.dumps(classifier, sort_keys=True, default=str)


def summarize_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten one replay artifact down to the fields a breadth comparison may use."""
    summary = payload["summary"]
    serial = summary.get("single_account_schedule") or {}
    version = summary.get("data_version") or {}
    return {
        "generated_at": str(summary.get("generated_at")),
        "universe_file": summary.get("universe_file"),
        "universe_digest": summary.get("universe_digest"),
        "lake_manifest": version.get("manifest"),
        "lake_symbols": version.get("symbols"),
        "lake_rows": version.get("rows"),
        "timeframe": summary["timeframe"],
        "cadence": summary["cadence"],
        "window_bars": summary["window_bars"],
        "counted_from": summary["counted_from"],
        "counted_to": summary["counted_to"],
        "counted_days_per_symbol": summary["counted_days_per_symbol"],
        "production_cycle_hours": summary.get("production_cycle_hours"),
        "classifier_key": _classifier_key(summary.get("classifier") or {}),
        "symbols_replayed": summary.get("symbols_replayed"),
        "symbols_skipped": summary.get("symbols_skipped"),
        "pooled_trades": summary.get("closed_trades_total"),
        "pooled_trades_per_day": summary.get("pooled_trades_per_day_upper_bound"),
        "serial_trades": serial.get("taken"),
        "serial_trades_per_day": serial.get("trades_per_day"),
        "profit_factor": serial.get("profit_factor"),
        "net_pnl_per_1000": serial.get("net_pnl_per_1000"),
        "win_rate_pct": serial.get("win_rate_pct"),
        "days_to_30_trades": serial.get("days_to_30_trades"),
        "serial_skips": serial.get("skipped"),
    }


def compare_rows(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Ratio of the cadence fields and the delta of the quality fields, if the two rows are locked."""
    mismatch = {field: [base.get(field), other.get(field)] for field in LOCK_FIELDS
                if base.get(field) != other.get(field)}
    out: dict[str, Any] = {"comparable": not mismatch, "protocol_mismatch": mismatch}
    if mismatch:
        return out
    for field in RATE_FIELDS:
        a, b = base.get(field), other.get(field)
        out[f"{field}_ratio"] = round(b / a, 4) if a and b is not None else None
    for field in QUALITY_FIELDS:
        a, b = base.get(field), other.get(field)
        out[f"{field}_delta"] = round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    out["breadth_ratio"] = (round(other["symbols_replayed"] / base["symbols_replayed"], 4)
                            if base.get("symbols_replayed") and other.get("symbols_replayed") else None)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", action="append", default=[], metavar="LABEL=PATH",
                        help="a replay summary JSON to include (repeatable)")
    parser.add_argument("--pair", action="append", default=[], nargs=2, metavar=("BASE", "OTHER"),
                        help="compare two labelled cells; BASE is the narrower basket")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    rows: dict[str, dict[str, Any]] = {}
    for cell in args.cell:
        label, _, raw = cell.partition("=")
        path = Path(raw)
        path = path if path.is_absolute() else ROOT / path
        rows[label] = summarize_run(json.loads(path.read_text(encoding="utf-8")))

    pairs = []
    for base, other in args.pair:
        if base not in rows or other not in rows:
            print(f"pair {base}->{other}: missing label", file=sys.stderr)
            continue
        merged = {"base": base, "other": other}
        merged.update(compare_rows(rows[base], rows[other]))
        pairs.append(merged)

    report = {"rows": rows, "pairs": pairs,
              "note": "cadence ratios are breadth effects only when protocol_mismatch is empty; "
                      "a higher trade count with a lower profit factor is not an improvement"}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for pair in pairs:
        print(f"{pair['base']} -> {pair['other']}: comparable={pair['comparable']} "
              f"serial/day {rows[pair['base']].get('serial_trades_per_day')} -> "
              f"{rows[pair['other']].get('serial_trades_per_day')} "
              f"(x{pair.get('serial_trades_per_day_ratio')}) PF "
              f"{rows[pair['base']].get('profit_factor')} -> {rows[pair['other']].get('profit_factor')}")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
