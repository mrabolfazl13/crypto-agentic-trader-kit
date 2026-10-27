"""Fill a month the monthly archive has not published yet, from Binance Vision *daily* zips.

    python tools/quant_ingest_daily.py --report
    python tools/quant_ingest_daily.py --from 2026-10-01 --to 2026-10-26 --limit 1
    python tools/quant_ingest_daily.py --from 2026-10-01 --to 2026-10-26 --symbols BTCUSDT --no-proxy

Resumable at day granularity, resumable across runs, and every write passes through the
same storage budget as the monthly ingest. The monthly ``manifest.json`` and its digest
are never touched: a month already stored from the monthly archive is refused outright,
so the windows DECISIONS.md already reports stay byte-reproducible.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import load_config  # noqa: E402
from cat.quant.ingest_daily import dates_in_range, daily_coverage, ingest_daily  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import (  # noqa: E402
    current_data_version,
    daily_data_version,
    storage_report,
)


def _log(settings, message: str) -> None:
    print(message, flush=True)
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.log_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--to", dest="last", default=None, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--symbols", default=None, help="comma separated lake symbols (BTCUSDT)")
    parser.add_argument("--limit", type=int, default=None, help="first N symbols of the recorded universe")
    parser.add_argument("--report", action="store_true", help="daily coverage + budget, no network")
    parser.add_argument("--no-proxy", action="store_true",
                        help="fetch directly; the configured local proxy comes and goes and a dead one "
                             "fails every download (2026-10-27: 127.0.0.1:1080 refused, direct worked)")
    parser.add_argument("--out", default=None, help="write the summary JSON here")
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    if args.no_proxy:
        from dataclasses import replace
        settings = replace(settings, proxy=None)
    elif settings.proxy is None:
        import os
        from dataclasses import replace
        settings = replace(settings, proxy=os.environ.get("CAT_PROXY") or None)

    storage = storage_report(settings)
    gib = 1024 ** 3
    if args.report:
        print(f"monthly data_version (unchanged by this tool): {current_data_version(settings)['manifest']}")
        print(f"daily data_version: {json.dumps(daily_data_version(settings), ensure_ascii=False)}")
        coverage = daily_coverage(settings)
        print(f"daily manifest digest: {coverage['digest']}")
        for month, info in coverage["months"].items():
            print(f"  {month}: {info['symbols']} symbols, {info['rows']:,} rows, "
                  f"{info['day_count']} day(s) {info['span'].get('first')}..{info['span'].get('last')}")
        if not coverage["months"]:
            print("  (no daily-sourced month stored)")
        print(f"storage: {storage['total_bytes'] / gib:.3f} GB of {storage['max_total_bytes'] / gib:.3f} GB, "
              f"raw {storage['categories']['raw']['bytes'] / gib:.3f} GB "
              f"({storage['categories']['raw']['used_pct']}%), disk free {storage['disk_free_gib']} GB")
        return 0

    if not args.first or not args.last:
        parser.error("--from and --to are required unless --report is used")
    dates = dates_in_range(args.first, args.last)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    _log(settings, f"[daily-ingest] start {dates[0]}..{dates[-1]} ({len(dates)} days) "
                   f"symbols={symbols or f'universe limit={args.limit}'} "
                   f"proxy={'yes' if settings.proxy else 'no'}")

    def progress(event: dict) -> None:
        _log(settings, f"{event.get('done', 0)}/{event.get('total', 0)} {event.get('message', '')}"
                       + (f" [{event.get('symbol')} {event.get('day')}]" if event.get("symbol") else ""))

    result = ingest_daily(settings, symbols=symbols, dates=dates, progress=progress, limit=args.limit)
    _log(settings, f"daily ingest {result['status']}: {result['days_stored']} symbol-days, "
                   f"{result['rows_composed']:,} composed rows, months={result['months']}, "
                   f"errors={result['error_count']}, {result['elapsed_seconds']}s")
    for err in result["errors"][:10]:
        _log(settings, f"  failed {err.get('symbol')} {err.get('day') or err.get('month')}: {err['error']}")
    print(f"monthly data_version before/after: {current_data_version(settings)['manifest']} "
          f"(manifest.json is not written by this tool)")
    print(f"daily data_version: {json.dumps(daily_data_version(settings), ensure_ascii=False)}")
    print(f"storage now: {storage_report(settings)['total_bytes'] / gib:.3f} GB, "
          f"disk free {storage_report(settings)['disk_free_gib']} GB")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"written: {out}")
    return 0 if result["status"] in {"complete", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
