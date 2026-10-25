"""Rebuild ``state/quant/universe.json`` from the CoinGecko snapshot + Binance Vision probes.

The ranking is a *current* market-cap snapshot (``state/cg_top250.json``), so the
resulting universe carries survivorship bias and the output records that. Only
symbols whose monthly 1m archives exist for every requested month are kept.

Usage:
    python tools/build_quant_universe.py            # probe uncached candidates over the network
    python tools/build_quant_universe.py --refresh  # re-fetch the CoinGecko snapshot first
    python tools/build_quant_universe.py --offline  # cached probes only, fail if not enough

Probe results (HTTP content-length in bytes) are cached in
``state/quant/universe_probe.json`` so a rerun does not hammer the archive.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "state" / "cg_top250.json"
PROBE_CACHE = ROOT / "state" / "quant" / "universe_probe.json"
OUTPUT = ROOT / "state" / "quant" / "universe.json"
MONTHS = ("2026-06", "2026-07", "2026-08", "2026-09")
REQUESTED = 100
URL = "https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{month}.zip"
USER_AGENT = "cat-quant-research/1.0 (local paper research)"

STABLECOINS = {
    "USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDS", "PYUSD", "BUSD", "FRAX",
    "LUSD", "GHO", "RLUSD", "USD1", "USDD", "EURC", "EURT", "USTC", "USDP", "GUSD",
}
COMMODITIES = {"XAUT", "PAXG", "KAU", "KAG"}
WRAPPED = {
    "WBTC", "WETH", "WSTETH", "STETH", "RETH", "CBETH", "WBETH", "WEETH", "BNSOL",
    "JITOSOL", "MSOL", "CBBTC", "LBTC", "TBTC", "WBNB", "WAVAX", "WSOL", "STSOL",
}
NAME_EXCLUDE = re.compile(
    r"(stablecoin|tokeni[sz]ed|wrapped|staked|bridged|etf\b|\bfund\b|\bindex\b)",
    re.IGNORECASE,
)


def _ascii_ticker(ticker: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{2,12}", ticker))


def candidates(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter the market-cap snapshot down to plausible Binance spot assets, in rank order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for coin in snapshot:
        ticker = str(coin.get("symbol") or "").upper()
        name = str(coin.get("name") or "")
        symbol = f"{ticker}USDT"
        if not _ascii_ticker(ticker) or symbol in seen:
            continue
        if ticker in STABLECOINS or ticker in COMMODITIES or ticker in WRAPPED:
            continue
        if NAME_EXCLUDE.search(name):
            continue
        if "stock" in name.lower() or "xstock" in symbol.lower():
            continue
        seen.add(symbol)
        out.append({
            "rank": int(coin.get("market_cap_rank") or 0),
            "ticker": ticker,
            "binance_symbol": symbol,
            "name": name,
            "market_cap_usd": int(coin.get("market_cap") or 0),
        })
    return out


def _load_cache() -> dict[str, dict[str, int]]:
    if PROBE_CACHE.exists():
        try:
            raw = json.loads(PROBE_CACHE.read_text(encoding="utf-8"))
            return {str(k): {str(m): int(v) for m, v in months.items()}
                    for k, months in raw.items() if isinstance(months, dict)}
        except (OSError, ValueError, TypeError):
            return {}
    return {}


def _seed_cache_from_output(cache: dict[str, dict[str, int]]) -> None:
    """The previous universe already carries measured archive sizes; reuse them."""
    if not OUTPUT.exists():
        return
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for entry in payload.get("entries", []):
        symbol = str(entry.get("binance_symbol") or "").upper()
        months = entry.get("months") if isinstance(entry.get("months"), dict) else {}
        if symbol and months and symbol not in cache:
            cache[symbol] = {str(k): int(v) for k, v in months.items() if isinstance(v, int)}


def _probe(symbol: str, month: str, proxy: str | None, retries: int = 3) -> int | None:
    """Return the archive size in bytes, or None when the file does not exist."""
    url = URL.format(symbol=symbol, month=month)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for attempt in range(retries):
        try:
            response = requests.head(url, headers={"User-Agent": USER_AGENT}, proxies=proxies,
                                     timeout=30, allow_redirects=True)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            size = response.headers.get("Content-Length")
            return int(size) if size and int(size) > 0 else 0
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return None


def _probe_symbol(entry: dict[str, Any], cache: dict[str, dict[str, int]], proxy: str | None,
                  offline: bool) -> dict[str, int] | None:
    symbol = entry["binance_symbol"]
    cached = cache.get(symbol) or {}
    months: dict[str, int] = {}
    for month in MONTHS:
        if month in cached:
            months[month] = cached[month]
            continue
        if offline:
            return None
        size = _probe(symbol, month, proxy)
        if size is None:
            return None
        months[month] = size
    return months


def build(*, offline: bool = False, proxy: str | None = None, refresh: bool = False) -> dict[str, Any]:
    if refresh or not SNAPSHOT.exists():
        raise SystemExit("refresh is not implemented here: refetch CoinGecko into state/cg_top250.json first")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if not isinstance(snapshot, list):
        raise SystemExit("state/cg_top250.json must be a list of coins")
    pool = candidates(snapshot)
    cache = _load_cache()
    _seed_cache_from_output(cache)

    selected: list[dict[str, Any]] = []
    probe_errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_probe_symbol, entry, cache, proxy, offline): entry for entry in pool}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                months = future.result()
            except requests.RequestException as exc:
                probe_errors.append(f"{entry['binance_symbol']}: {type(exc).__name__}")
                continue
            if months and len(months) == len(MONTHS):
                cache[entry["binance_symbol"]] = months
                if len(selected) < REQUESTED:
                    selected.append({**entry, "months": dict(months)})
            if len(selected) >= REQUESTED:
                for pending in futures:
                    pending.cancel()
                break

    selected.sort(key=lambda item: item["rank"])
    selected = selected[:REQUESTED]
    if len(selected) < REQUESTED:
        raise SystemExit(f"only {len(selected)} symbols have all four monthly archives; need {REQUESTED}")

    PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROBE_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema_version": "quant-universe-v1",
        "generated_at": generated_at,
        "source": {
            "name": "Binance Vision",
            "url": "https://data.binance.vision/",
            "klines": "spot monthly 1m",
            "license": "Binance public market data archive; see https://data.binance.vision/terms-of-use.html",
            "ranking": "CoinGecko markets snapshot (current market cap, fetched 2026-10-26)",
            "ranking_note": "current ranking, not historical: survivorship bias is possible and is not corrected",
        },
        "selection": {
            "requested": REQUESTED,
            "count": len(selected),
            "months": list(MONTHS),
            "coverage_rule": "monthly 1m files exist for all four months; row-level gaps measured at ingest",
            "exclusions": "stablecoins, tokenised commodities/stocks, wrapped/staked duplicates, funds/ETFs, non-ASCII symbols",
        },
        "symbols": [item["binance_symbol"] for item in selected],
        "entries": selected,
        "limitations": [
            "Only symbols listed on Binance for all four months are included; assets delisted before the window are absent.",
            "Ranking is the current market-cap snapshot, so results are not a survivorship-bias-free test.",
            "Monthly archives are exchange data, not an independent price source; no cross-provider reconciliation yet.",
        ],
        "probe_errors": probe_errors[:20],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="use cached probes only")
    parser.add_argument("--refresh", action="store_true", help="refetch the CoinGecko snapshot (unsupported here)")
    parser.add_argument("--proxy", default=os.environ.get("CAT_PROXY") or None)
    args = parser.parse_args()
    payload = build(offline=args.offline, proxy=args.proxy, refresh=args.refresh)
    total = sum(sum(entry["months"].values()) for entry in payload["entries"])
    print(f"wrote {OUTPUT} with {payload['selection']['count']} symbols, {total / (1024 ** 2):.1f} MiB compressed")


if __name__ == "__main__":
    main()
