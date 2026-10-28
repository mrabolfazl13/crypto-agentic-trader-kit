"""Build a research universe from the *deployed* allowlist instead of a market-cap ranking.

D-025 measured that only 66 of the 100 symbols the PAPER engine may order have any backtest evidence
in the research lake, and refused to report the naive ×1.52 cadence scaling because it was never
measured. This tool makes that measurement possible: it writes a second universe file whose selection
rule is ``config.yaml -> trading.symbols`` — the owner's live basket — and keeps the same
availability rule as `build_quant_universe.py` (a symbol enters only if the monthly 1m archive exists
for every requested month), listing the exclusions instead of dropping them silently.

`state/quant/universe.json` is never touched: its digest is cited by the D-016..D-024 records, so
this output goes to its own path and each run records both.

Usage:
    python tools/build_allowlist_universe.py --out state/quant/universe_allowlist.json
    python tools/build_allowlist_universe.py --offline   # cached probes only
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from build_quant_universe import PROBE_CACHE, _load_cache, _probe  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import load_universe  # noqa: E402

SCHEMA_VERSION = "quant-universe-v1"
DEFAULT_OUT = ROOT / "state" / "quant" / "universe_allowlist.json"


def deployed_allowlist(config: Any) -> list[str]:
    """The symbols the running engine may order, in Binance Vision spelling (`X_USDT` → `XUSDT`)."""
    return sorted({str(symbol).replace("/", "").replace("_", "").upper()
                   for symbol in (config.symbols or [])})


def probe_months(symbols: list[str], months: list[str], *, proxy: str | None,
                 cache: dict[str, dict[str, int]], offline: bool) -> tuple[dict[str, dict[str, int]],
                                                                           dict[str, str]]:
    """Return {symbol: {month: bytes}} for complete symbols, and {symbol: reason} for the rest."""
    todo = [(symbol, month) for symbol in symbols for month in months
            if month not in (cache.get(symbol) or {})]
    if todo and not offline:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_probe, symbol, month, proxy): (symbol, month)
                       for symbol, month in todo}
            for future in concurrent.futures.as_completed(futures):
                size = future.result()
                if size is not None:
                    cache.setdefault(futures[future][0], {})[futures[future][1]] = size

    sizes: dict[str, dict[str, int]] = {}
    failures: dict[str, str] = {}
    uncached = {symbol for symbol, _ in todo}
    for symbol in symbols:
        cached = cache.get(symbol) or {}
        complete = {month: cached[month] for month in months if month in cached}
        absent = sorted(set(months) - set(complete))
        if not absent:
            sizes[symbol] = complete
        elif offline and symbol in uncached:
            # Offline never proved this month, so it is not evidence that the archive is missing.
            failures[symbol] = "offline and no cached probe for " + ", ".join(absent)
        else:
            failures[symbol] = "no monthly archive for " + ", ".join(absent)
    return sizes, failures


def build_universe(*, symbols: list[str], months: list[str], sizes: dict[str, dict[str, int]],
                   failures: dict[str, str], research_universe: list[str],
                   config_path: str) -> dict[str, Any]:
    entered = [symbol for symbol in symbols if symbol in sizes]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": {
            "name": "deployed PAPER allowlist",
            "url": config_path,
            "klines": "spot monthly 1m (Binance Vision)",
            "license": "Binance public market data archive; see "
                       "https://data.binance.vision/terms-of-use.html",
            "ranking": "none: this basket is the owner's live symbol list, not a market-cap ranking",
            "ranking_note": ("selection is whatever config.yaml permits today, so it inherits the "
                             "owner's listing choices rather than a reproducible index"),
        },
        "selection": {
            "requested": len(symbols),
            "count": len(entered),
            "months": list(months),
            "coverage_rule": ("a symbol enters only when the monthly 1m archive exists for every "
                              "requested month; the same rule as the research universe"),
            "exclusions": "stablecoins and duplicates are whatever the allowlist itself contains; "
                          "archive-missing symbols are listed in probe_errors",
        },
        "symbols": entered,
        "entries": [{"rank": None, "ticker": symbol[:-4] if symbol.endswith("USDT") else symbol,
                     "binance_symbol": symbol, "name": None, "market_cap_usd": None,
                     "months": sizes[symbol]} for symbol in entered],
        "limitations": [
            "Only symbols listed on Binance for every requested month are included; assets delisted "
            "before the window are absent and named in probe_errors.",
            "The basket is the deployed allowlist, chosen by the owner, so it is not a reproducible "
            "index and cannot be compared to the ranked universe without saying so.",
            "Monthly archives are exchange data, not an independent price source; no cross-provider "
            "reconciliation yet.",
            f"{len(research_universe)} symbols are carried by the ranked research universe "
            f"(state/quant/universe.json); only their overlap is already evidenced.",
        ],
        "probe_errors": [{"symbol": symbol, "reason": reason}
                         for symbol, reason in sorted(failures.items())],
    }


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--months", default="",
                        help="comma separated YYYY-MM; default = the lake months of the research universe")
    parser.add_argument("--offline", action="store_true", help="cached probes only")
    parser.add_argument("--proxy", default=os.environ.get("CAT_PROXY") or None)
    args = parser.parse_args(argv)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    research = ROOT / "state" / "quant" / "universe.json"
    if out.resolve() == research.resolve():
        print("refusing to overwrite the ranked research universe; D-016..D-024 cite its digest")
        return 3

    config = load_config(root=ROOT)
    settings = load_quant_settings(config)
    symbols = deployed_allowlist(config)
    if not symbols:
        print("config.yaml -> trading.symbols is empty; nothing to build")
        return 3
    payload_existing = load_universe(settings)
    months = ([m.strip() for m in args.months.split(",") if m.strip()]
              or sorted({month for entry in payload_existing.get("entries", [])
                         for month in (entry.get("months") or {})}))
    if args.months:
        months = sorted(months)

    cache = _load_cache()
    already = set(payload_existing["_symbols"])
    print(f"deployed allowlist: {len(symbols)} symbols; research universe: {len(already)} "
          f"(overlap {len(set(symbols) & already)})")
    print(f"months probed: {months}")
    sizes, failures = probe_months(symbols, months, proxy=args.proxy, cache=cache,
                                   offline=args.offline)
    payload = build_universe(symbols=symbols, months=months, sizes=sizes, failures=failures,
                             research_universe=sorted(already),
                             config_path="config.yaml -> trading.symbols")
    missing_evidence = sorted(set(symbols) - already)
    print(f"entering: {payload['selection']['count']} of {len(symbols)}; "
          f"{len(missing_evidence)} symbols had no lake evidence before this run")
    for item in payload["probe_errors"]:
        print(f"  excluded {item['symbol']}: {item['reason']}")
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    PROBE_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    print(f"written: {out.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
