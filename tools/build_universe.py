#!/usr/bin/env python3
"""Build the tradable universe from a real market-cap ranking.

The universe is a dated snapshot, not a live feed:

* ranking: CoinGecko ``/coins/markets`` (market_cap_desc)
* tradability: Gate spot ``/spot/currency_pairs`` where quote == USDT and
  ``trade_status == tradable``
* stablecoins, tokenised commodities and wrapped/staked duplicates are removed
  with an explicit reason in ``universe.json``

The script writes ``universe.json`` (machine-readable provenance) and replaces
only the ``trading.symbols`` block of ``config.yaml``, leaving every other line
and comment untouched. The change is appended to the hash-chained audit log.

Usage:
    python tools/build_universe.py                 # fetch, write manifest + config
    python tools/build_universe.py --no-write      # dry run, print the summary
    python tools/build_universe.py --proxy http://127.0.0.1:1080
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:  # Windows consoles default to cp1252; Persian + "·" must not crash the tool
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:  # pragma: no cover - older interpreters
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.audit import AuditLog  # noqa: E402

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
GATE_PAIRS_URL = "https://api.gateio.ws/api/v4/spot/currency_pairs"
USER_AGENT = "crypto-agentic-trader/1.0 (paper research universe builder)"

# A ticker containing USD is a fiat-pegged token in every market we have seen.
STABLE_TICKERS = {
    "DAI", "GHO", "FRAX", "LUSD", "USTC", "EURC", "EURT", "U", "USX",
    "RLUSD", "PYUSD", "USDD", "USDP", "TUSD", "FDUSD", "GUSD", "USDE",
    "USDY", "USDG", "USD1", "USDS", "AUSD", "OUSD", "USDAI", "REUSD",
    "BFUSD", "USDF", "USD0", "YLDS", "EURCV", "STABLE",
}
GOLD_TICKERS = {"XAUT", "PAXG", "KAU", "KAG"}
WRAPPED_TICKERS = {
    "WBTC", "WETH", "STETH", "WBETH", "CBETH", "RETH", "WEETH", "WSTETH",
    "BTCB", "BSC-USD", "WBNB", "SOL-WRAPPED", "MSOL", "JITOSOL",
}
STABLE_NAME = re.compile(r"(stablecoin|\bstable\b|us dollar|dollar|tether|t-bill|treasury|money market)", re.I)
GOLD_NAME = re.compile(r"(\bgold\b|\bsilver\b|tokeni[sz]ed (gold|silver))", re.I)
WRAPPED_NAME = re.compile(r"(wrapped|staked|liquid staking)", re.I)


class FetchError(RuntimeError):
    pass


def fetch_json(url: str, *, params: dict[str, Any] | None = None, proxy: str | None = None,
               attempts: int = 3, timeout: float = 40.0) -> Any:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url, params=params, proxies=proxies, timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise FetchError(f"HTTP {response.status_code} from {url}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, FetchError) as exc:
            last = exc
            if attempt < attempts:
                wait = 2.0 * attempt
                print(f"  retry {attempt}/{attempts - 1} after {type(exc).__name__}: {exc} (sleep {wait:.0f}s)", file=sys.stderr)
                time.sleep(wait)
    raise FetchError(f"failed after {attempts} attempts: {url} ({last})")


def classification(ticker: str, name: str) -> str | None:
    """Return the exclusion reason, or None when the coin is a trading candidate."""
    upper = ticker.upper()
    # Gold first: "Tether Gold" would otherwise be misread as a stablecoin.
    if upper in GOLD_TICKERS or GOLD_NAME.search(name):
        return "tokenised commodity (gold/silver)"
    if upper in WRAPPED_TICKERS or WRAPPED_NAME.search(name):
        return "wrapped/staked duplicate"
    if upper in STABLE_TICKERS or "USD" in upper or STABLE_NAME.search(name):
        return "stablecoin"
    return None


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def tokens(text: str) -> set[str]:
    return {part for part in re.split(r"[^a-z0-9]+", (text or "").lower()) if part}


def names_match(coin_name: str, ticker: str, gate_name: str) -> bool:
    """Guard against a ticker collision: the asset names must share something."""
    a, b = normalise(coin_name), normalise(gate_name)
    if a and b and (a in b or b in a):
        return True
    ignored = {"the", "token", "network", "protocol", "finance", "coin", "chain", "labs", "dao", "ecosystem"}
    left = tokens(coin_name) - ignored
    right = tokens(gate_name) - ignored
    return bool(left & right) or normalise(ticker) in (a + b)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_symbols_block(text: str, symbols: list[str], *, fetched_at: str, count: int) -> str:
    pattern = re.compile(r"(?m)^  symbols:[^\n]*\n(?:    - [^\n]*\n)+")
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one 'trading.symbols' block, found {len(matches)}")
    header = (
        "  symbols:                    # {count} جفت USDT برتر Gate به ترتیب ارزش بازار — بازتولید: python tools/build_universe.py\n"
        "    # منبع رتبه‌بندی: CoinGecko /coins/markets (market_cap_desc) · snapshot: {fetched_at}\n"
        "    # فقط جفت‌های tradable با quote=USDT؛ استیبل‌کوین/طلا/نسخه رپ‌شده حذف شده‌اند (جزئیات: universe.json).\n"
    ).format(count=count, fetched_at=fetched_at)
    body = "".join(f"    - {symbol}\n" for symbol in symbols)
    return pattern.sub(lambda _match: header + body, text, count=1)


def build(count: int, top: int, proxy: str | None) -> dict[str, Any]:
    print(f"fetching Gate spot pairs: {GATE_PAIRS_URL}")
    pairs = fetch_json(GATE_PAIRS_URL, proxy=proxy)
    if not isinstance(pairs, list):
        raise FetchError("Gate currency_pairs did not return a list")
    usdt = {
        str(row.get("base", "")).upper(): row
        for row in pairs
        if str(row.get("quote", "")).upper() == "USDT"
        and str(row.get("trade_status", "")) == "tradable"
        and str(row.get("type", "normal")) == "normal"
    }

    print(f"fetching CoinGecko top {top} by market cap")
    markets = fetch_json(
        COINGECKO_URL,
        params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": top, "page": 1},
        proxy=proxy,
    )
    if not isinstance(markets, list) or not markets:
        raise FetchError("CoinGecko did not return a market list")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for coin in markets:
        ticker = str(coin.get("symbol") or "").upper().strip()
        name = str(coin.get("name") or ticker)
        rank = coin.get("market_cap_rank")
        if not ticker:
            continue
        reason = classification(ticker, name)
        if reason:
            excluded.append({"rank": rank, "ticker": ticker, "name": name, "reason": reason})
            continue
        pair = usdt.get(ticker)
        if pair is None:
            skipped.append({"rank": rank, "ticker": ticker, "name": name, "reason": "no tradable Gate USDT pair"})
            continue
        gate_name = str(pair.get("base_name") or pair.get("base") or "")
        if not names_match(name, ticker, gate_name):
            skipped.append({
                "rank": rank, "ticker": ticker, "name": name,
                "reason": f"asset name mismatch on Gate: {gate_name!r}",
            })
            continue
        if ticker in seen:
            skipped.append({"rank": rank, "ticker": ticker, "name": name, "reason": "duplicate ticker"})
            continue
        seen.add(ticker)
        entries.append({
            "rank": rank,
            "ticker": ticker,
            "name": name,
            "symbol": f"{ticker}/USDT",
            "gate_pair": pair.get("id"),
            "gate_base_name": gate_name,
            "market_cap_usd": coin.get("market_cap"),
            "volume_24h_usd": coin.get("total_volume"),
            "price_usd_at_snapshot": coin.get("current_price"),
        })
        if len(entries) >= count:
            break

    if len(entries) < count:
        raise SystemExit(f"only {len(entries)} eligible pairs found; refusing to write a short universe")

    symbols = [entry["symbol"] for entry in entries]
    digest = sha256_text(",".join(symbols))
    return {
        "schema_version": 1,
        "generated_at": fetched_at,
        "requested_count": count,
        "count": len(symbols),
        "digest": digest,
        "source": {
            "name": "CoinGecko",
            "endpoint": COINGECKO_URL,
            "ranking": "market_cap_desc",
            "gate_endpoint": GATE_PAIRS_URL,
            "gate_tradable_usdt_pairs": len(usdt),
            "fetched_at": fetched_at,
            "note": "رتبه‌بندی و قیمت‌ها در لحظه‌ی snapshot هستند، نه قیمت زنده.",
        },
        "filters": {
            "excluded_categories": ["stablecoin", "tokenised commodity (gold/silver)", "wrapped/staked duplicate"],
            "tradability": "Gate spot, quote=USDT, trade_status=tradable, type=normal",
        },
        "symbols": symbols,
        "entries": entries,
        "excluded": excluded,
        "skipped": skipped,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        temp = Path(fh.name)
    os.replace(temp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the top-N tradable universe snapshot")
    parser.add_argument("--count", type=int, default=100, help="how many tradable pairs to keep (default 100)")
    parser.add_argument("--top", type=int, default=250, help="CoinGecko rows to scan (default 250)")
    parser.add_argument("--proxy", default=os.environ.get("CAT_PROXY") or None)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "universe.json")
    parser.add_argument("--no-write", action="store_true", help="dry run: fetch and print, write nothing")
    args = parser.parse_args(argv)

    if args.count < 1 or args.top < args.count:
        parser.error("--top must be >= --count >= 1")
    if args.proxy:
        print(f"using proxy {args.proxy}")

    manifest = build(args.count, args.top, args.proxy)
    print(
        f"universe: {manifest['count']} pairs · excluded {len(manifest['excluded'])} · "
        f"skipped {len(manifest['skipped'])} · digest {manifest['digest'][:12]}"
    )
    print("first 10: " + ", ".join(manifest["symbols"][:10]))
    print("last 5:   " + ", ".join(manifest["symbols"][-5:]))
    for item in manifest["excluded"]:
        if item["rank"] and item["rank"] <= 120:
            print(f"  excluded #{item['rank']} {item['ticker']}: {item['reason']}")
    for item in manifest["skipped"]:
        if item["rank"] and item["rank"] <= 160:
            print(f"  skipped  #{item['rank']} {item['ticker']}: {item['reason']}")

    if args.no_write:
        print("dry run: nothing written")
        return 0

    write_manifest(args.out, manifest)
    print(f"wrote {args.out}")

    config_text = args.config.read_text(encoding="utf-8")
    updated = replace_symbols_block(
        config_text, manifest["symbols"], fetched_at=manifest["generated_at"], count=manifest["count"]
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.config.parent, delete=False, suffix=".tmp", newline="\n") as fh:
        fh.write(updated)
        temp = Path(fh.name)
    os.replace(temp, args.config)
    print(f"updated {args.config} (trading.symbols only)")

    audit = AuditLog(ROOT / "state" / "audit" / "events.jsonl")
    record = audit.append(
        "universe_updated",
        {
            "source": manifest["source"]["name"],
            "endpoint": manifest["source"]["endpoint"],
            "fetched_at": manifest["generated_at"],
            "count": manifest["count"],
            "digest": manifest["digest"],
            "symbols": manifest["symbols"],
            "excluded": len(manifest["excluded"]),
            "skipped": len(manifest["skipped"]),
            "config": str(args.config),
            "manifest": str(args.out),
        },
    )
    print(f"audit seq={record['seq']} event={record['event']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
