"""Download and store the quant 1m history with progress, resumable at file granularity.

    python tools/quant_ingest.py                 # all 100 symbols x 4 months
    python tools/quant_ingest.py --limit 3       # smoke test
    python tools/quant_ingest.py --symbols BTCUSDT,ETHUSDT
    python tools/quant_ingest.py --months 2026-09
    python tools/quant_ingest.py --report        # storage + coverage only, no network

Every write passes through the 2.5 GB budget in ``cat.quant.storage``; nothing is
deleted to make room and no synthetic bar is created.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import load_config  # noqa: E402
from cat.quant.ingest import ingest  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import coverage_report, load_manifest, storage_report  # noqa: E402


def _log(settings, message: str) -> None:
    print(message, flush=True)
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.log_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--symbols", default=None, help="comma separated symbols")
    parser.add_argument("--months", default=None, help="comma separated YYYY-MM")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--universe", default=None,
                        help="alternative quant-universe-v1 file (e.g. the deployed-allowlist "
                             "universe); state/quant/universe.json is never written by this tool")
    parser.add_argument("--no-proxy", action="store_true",
                        help="fetch directly; the configured local proxy comes and goes and a dead "
                             "one fails every download (same escape as quant_ingest_daily.py)")
    args = parser.parse_args()

    config = load_config(root=ROOT)
    settings = load_quant_settings(config)
    if args.no_proxy:
        from dataclasses import replace
        settings = replace(settings, proxy=None)
    if args.universe:
        from dataclasses import replace
        path = Path(args.universe)
        settings = replace(settings, universe_path=(path if path.is_absolute() else ROOT / path))
    if settings.proxy is None:
        import os
        from dataclasses import replace
        settings = replace(settings, proxy=os.environ.get("CAT_PROXY") or None)

    if args.report:
        storage = storage_report(settings)
        gib = 1024 ** 3
        print(f"total used: {storage['total_bytes'] / gib:.3f} GB of "
              f"{storage['max_total_bytes'] / gib:.3f} GB "
              f"({storage['used_pct']}%, within_budget={storage['within_budget']})")
        for name, info in storage["categories"].items():
            print(f"  {name:9s} {info['bytes'] / gib:.3f} GB / {info['cap_bytes'] / gib:.3f} GB")
        print(f"disk free: {storage['disk_free_gib']} GB")
        manifest = load_manifest(settings)
        print(f"manifest: {manifest.get('totals', {}).get('months', 0)} symbol-months, "
              f"{manifest.get('totals', {}).get('rows', 0):,} rows, "
              f"{len(manifest.get('errors') or [])} unresolved errors")
        report = coverage_report(settings)
        print(f"coverage: {report['symbols_stored']} symbols, {report['symbols_complete']} complete, "
              f"{report['coverage_pct']}% of expected minutes")
        return 0

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    months = [m.strip() for m in args.months.split(",")] if args.months else None
    _log(settings, f"[{settings.log_path.name}] ingest start symbols={symbols or 'universe'} "
                   f"months={months or list(settings.months)} proxy={'yes' if settings.proxy else 'no'}")

    def progress(event: dict) -> None:
        extras = []
        if event.get("error"):
            extras.append(f"error={event['error']}")
        if event.get("bytes"):
            extras.append(f"{event['bytes'] / 1024:.0f} KiB")
        if event.get("coverage_pct") is not None:
            extras.append(f"coverage={event['coverage_pct']}%")
        suffix = f" [{' '.join(extras)}]" if extras else ""
        _log(settings, f"{event.get('done', 0)}/{event.get('total', 0)} {event.get('message', '')}{suffix}")

    result = ingest(settings, symbols=symbols, months=months, progress=progress, limit=args.limit)
    storage = result.get("storage", {})
    totals = (result.get("coverage") or {}).get("totals") or {}
    _log(settings, f"ingest {result['status']}: {result.get('files_succeeded')} files, "
                   f"{totals.get('rows', 0):,} rows, storage {storage.get('total_bytes', 0) / (1024 ** 3):.3f} GB")
    for err in list(result.get("errors") or [])[:10]:
        _log(settings, f"  failed {err.get('symbol')} {err.get('month')}: {err.get('error')}")
    return 0 if result["status"] in {"complete", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
