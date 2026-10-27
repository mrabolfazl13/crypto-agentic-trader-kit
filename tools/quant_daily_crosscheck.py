"""Cross-check the daily-sourced bars against the monthly archive already in the lake.

The daily path exists because the monthly zip for the current month is not published
yet (2026-10). That means its output cannot be validated against a same-month monthly
file today — so this tool checks it on a *closed* month instead: it pulls a few daily
archives into a scratch directory, composes them the way ``cat.quant.ingest_daily``
does, and compares them minute by minute with the monthly-sourced bars the research
chain already trusts.

When the 2026-10 monthly zip lands (≈ Nov 1), the same run with ``--month 2026-10``
proves the third window equals what the monthly path would have produced.

    python tools/quant_daily_crosscheck.py --symbol BTCUSDT --month 2026-09 --days 3

Nothing in the real lake is written: the scratch store lives under
``state/quant/results/daily_crosscheck/`` and is deleted on exit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from cat.config import load_config  # noqa: E402
from cat.quant import ingest_daily  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, read_symbol  # noqa: E402

FIELDS = ("open", "high", "low", "close", "volume")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--month", required=True, help="a month already stored from the monthly archive")
    parser.add_argument("--days", type=int, default=3, help="how many days of that month to fetch")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "state" / "quant" / "results" / "daily_crosscheck.json"))
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    if args.no_proxy:
        settings = replace(settings, proxy=None)
    monthly_version_before = current_data_version(settings)

    if args.month not in list(settings.months):
        print(f"{args.month} is not a monthly month in the lake; the crosscheck needs a stored month "
              f"to compare against (configured: {list(settings.months)})")
        return 3

    reference = read_symbol(settings, args.symbol, months=[args.month]).copy()
    reference["ts"] = reference["ts"].astype("int64")
    days = sorted({pd.Timestamp(int(ts), unit="s").strftime("%Y-%m-%d") for ts in reference["ts"]})[: args.days]
    if not days:
        print(f"no bars stored for {args.symbol} {args.month}")
        return 3

    scratch = ROOT / "state" / "quant" / "results" / "daily_crosscheck"
    if scratch.exists():
        for stale in scratch.rglob("*"):
            if stale.is_file():
                stale.unlink()
    scratch.mkdir(parents=True, exist_ok=True)
    daily_settings = replace(settings, data_dir=scratch)

    # The public entry point, on a scratch lake: store → compose → daily manifest.
    result = ingest_daily.ingest_daily(daily_settings, symbols=[args.symbol], dates=days)
    if result["status"] != "complete":
        print(f"daily ingest on the scratch lake did not complete: {result['errors'][:3]}")
        return 1
    daily_frame = read_symbol(daily_settings, args.symbol, months=[args.month])
    start = int(datetime.strptime(days[0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    stop = int(datetime.strptime(days[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
    ref_slice = reference[(reference["ts"] >= start) & (reference["ts"] < stop)].sort_values("ts")
    merged = daily_frame.merge(ref_slice, on="ts", how="outer", suffixes=("_daily", "_monthly"),
                               indicator=True)
    only_daily = int((merged["_merge"] == "left_only").sum())
    only_monthly = int((merged["_merge"] == "right_only").sum())
    both = merged[merged["_merge"] == "both"]
    worst = {}
    for field in FIELDS:
        diff = (both[f"{field}_daily"] - both[f"{field}_monthly"]).abs()
        rel = diff / both[f"{field}_monthly"].abs().clip(lower=1e-12)
        worst[field] = {"max_abs": float(diff.max()) if len(diff) else None,
                        "max_rel_pct": float(rel.max() * 100.0) if len(rel) else None,
                        "mismatches": int((diff > 0).sum())}

    row_equal = len(both) == len(daily_frame) == len(ref_slice) and only_daily == 0 and only_monthly == 0
    fields_equal = all(entry["mismatches"] == 0 for entry in worst.values())
    coverage = ingest_daily.daily_coverage(daily_settings)["months"][args.month]
    summary = {
        "symbol": args.symbol,
        "month": args.month,
        "days": days,
        "daily_rows": int(len(daily_frame)),
        "monthly_rows": int(len(ref_slice)),
        "matched_rows": int(len(both)),
        "only_in_daily": only_daily,
        "only_in_monthly": only_monthly,
        "field_differences": worst,
        "daily_digest": result["digest"],
        "daily_rows_composed": result["rows_composed"],
        "daily_month_coverage": {key: coverage[key] for key in ("symbols", "rows", "day_count", "span")},
        "verdict": "identical" if row_equal and fields_equal else "differs",
        "monthly_data_version": monthly_version_before,
        "monthly_data_version_after": current_data_version(settings),
        "scratch_dir": str(scratch),
        "note": ("daily archives composed by cat.quant.ingest_daily compared with the monthly-sourced "
                 "bars of the same symbol-month; the real lake and manifest.json were not written"),
    }
    print(f"{args.symbol} {args.month}: daily {len(daily_frame)} rows vs monthly {len(ref_slice)} rows "
          f"over {days[0]}..{days[-1]} ⇒ {summary['verdict']}")
    for field, entry in worst.items():
        print(f"  {field:6s} mismatches={entry['mismatches']} max_abs={entry['max_abs']} "
              f"max_rel={entry['max_rel_pct']}%")
    print(f"monthly data_version before/after: {summary['monthly_data_version']['manifest']} / "
          f"{summary['monthly_data_version_after']['manifest']}")
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"written: {args.out}")
    shutil.rmtree(scratch, ignore_errors=True)
    return 0 if summary["verdict"] == "identical" else 1


if __name__ == "__main__":
    raise SystemExit(main())
