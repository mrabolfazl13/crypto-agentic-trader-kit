#!/usr/bin/env python3
"""Build the TSE instrument universe from TSETMC's own marketwatch.

The universe is a dated snapshot with machine-readable provenance, not a live
feed:

* source: ``https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch`` with the
  same ``market=0&paperTypes[0]=1`` query the runtime provider uses, so the
  manifest and the board can never drift apart;
* ranking: last session trade value (``qtc``) descending — an explicit,
  reproducible rule, not a hand-picked list;
* identity: numeric ``insCode`` is canonical; the four-character board ticker
  (``lva``) and ISIN are display aliases resolved through this file;
* exclusions: every dropped row is counted with a reason, never silently lost.

The marketwatch payload has ``dEven = 0`` on every row (verified on the recorded
fixture and live on 2026-10-24), so the session date is taken from
``GetMarketOverview`` (``marketActivityDEven``) and stored in the manifest.

Usage:
    python tools/build_tse_universe.py                  # fetch live, write manifest
    python tools/build_tse_universe.py --no-write       # dry run
    python tools/build_tse_universe.py --fixture tests/fixtures/tse_marketwatch_m1.json
    python tools/build_tse_universe.py --limit 0        # keep every valid row
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:  # Windows consoles default to cp1252; Persian tickers must not crash the tool
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:  # pragma: no cover - older interpreters
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.markets.tse import (  # noqa: E402
    BASE,
    MARKETWATCH_PARAMS,
    MARKETWATCH_PATH,
    MARKET_OVERVIEW_PATH,
    USER_AGENT,
)

DEFAULT_OUT = ROOT / "universe_tse.json"
DEFAULT_LIMIT = 500

# ``paperTypes`` no longer narrows the live board (verified 2026-10-24: every
# query variant returned the same 3462 rows, including funds, debt and options).
# The ISIN is the exchange's own instrument taxonomy: ordinary shares live on
# IRO1/IRO3/IRO5/IRO7 and use serial 0001 (or 0011); rights use 0002-0004.
SHARE_PREFIXES = ("IRO1", "IRO3", "IRO5", "IRO7")
SHARE_SERIALS = {"0001", "0011"}


def is_ordinary_share(isin: str | None) -> bool:
    text = str(isin or "").strip().upper()
    return len(text) == 12 and text[:4] in SHARE_PREFIXES and text[-4:] in SHARE_SERIALS


class FetchError(RuntimeError):
    pass


def fetch_json(url: str, *, params: dict[str, Any] | None = None, proxy: str | None = None,
               attempts: int = 3, timeout: float = 40.0) -> Any:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=timeout)
            if response.status_code == 429 or response.status_code >= 500:
                raise FetchError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, FetchError) as exc:
            last = exc
            if attempt < attempts:
                time.sleep(1.5 * attempt)
    raise FetchError(f"request failed after {attempts} attempts: {url} ({last})")


def fetch_board(proxy: str | None) -> list[dict[str, Any]]:
    payload = fetch_json(f"{BASE}{MARKETWATCH_PATH}", params=MARKETWATCH_PARAMS, proxy=proxy)
    rows = payload.get("marketwatch") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise FetchError("TSETMC marketwatch returned no rows")
    return [row for row in rows if isinstance(row, dict)]


def fetch_board_date(proxy: str | None) -> int | None:
    payload = fetch_json(f"{BASE}{MARKET_OVERVIEW_PATH.format(flow=1)}", proxy=proxy)
    overview = payload.get("marketOverview") if isinstance(payload, dict) else None
    if not isinstance(overview, dict):
        return None
    try:
        day = int(overview.get("marketActivityDEven") or 0)
    except (TypeError, ValueError):
        return None
    return day or None


def _fixture_board_date(fixture: Path) -> int | None:
    candidate = fixture.parent / "tse_marketoverview_f1.json"
    if not candidate.exists():
        return None
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        return int((payload.get("marketOverview") or {}).get("marketActivityDEven") or 0) or None
    except (OSError, ValueError, TypeError):
        return None


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_instruments(rows: list[dict[str, Any]], *, limit: int = DEFAULT_LIMIT,
                      min_value: float = 0.0) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Filter, rank and trim the board. Exclusions are returned with reasons."""
    excluded: Counter[str] = Counter()
    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []

    for row in rows:
        code = str(row.get("insCode") or "").strip()
        ticker = str(row.get("lva") or "").strip()
        name = str(row.get("lvc") or "").strip()
        isin = str(row.get("insID") or "").strip() or None
        last = _number(row.get("pcl"))
        yesterday = _number(row.get("py"))
        band_low = _number(row.get("pMin"))
        band_high = _number(row.get("pMax"))
        value = _number(row.get("qtc"))

        if not code.isdigit():
            excluded["invalid_ins_code"] += 1
            continue
        if code in seen:
            excluded["duplicate_ins_code"] += 1
            continue
        seen.add(code)
        if not is_ordinary_share(isin):
            excluded["not_ordinary_share"] += 1
            continue
        if not ticker or not name:
            excluded["missing_name"] += 1
            continue
        if last <= 0:
            excluded["no_last_price"] += 1
            continue
        if yesterday <= 0 or band_low <= 0 or band_high <= 0:
            excluded["incomplete_price_band"] += 1
            continue
        if value < min_value:
            excluded["below_min_value"] += 1
            continue

        accepted.append({
            "insCode": code,
            "ticker": ticker,
            "name": name,
            "isin": isin,
            "group": str(row.get("csv") or "").strip() or None,
            "last_price": last,
            "price_yesterday": yesterday,
            "change_pct": round((last / yesterday - 1) * 100, 3),
            "band_low": band_low,
            "band_high": band_high,
            "trade_value_rial": value or None,
            "volume_shares": _number(row.get("qtj")) or None,
            "trade_count": _number(row.get("ztt")) or None,
            "base_volume": _number(row.get("bv")) or None,
        })

    accepted.sort(key=lambda item: (-(item["trade_value_rial"] or 0.0), item["ticker"]))
    for rank, item in enumerate(accepted, 1):
        item["rank"] = rank
    trimmed = accepted[:limit] if limit and limit > 0 else accepted
    if limit and len(accepted) > limit:
        excluded["beyond_limit"] += len(accepted) - limit
    return trimmed, excluded


def manifest_digest(instruments: list[dict[str, Any]]) -> str:
    blob = json.dumps(instruments, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temp = Path(handle.name)
    temp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build universe_tse.json from TSETMC marketwatch")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="keep the top N by trade value (0 = all; default %(default)s)")
    parser.add_argument("--min-value", type=float, default=0.0, help="minimum last-session trade value in Rial")
    parser.add_argument("--fixture", type=Path, help="read a recorded marketwatch payload instead of the network")
    parser.add_argument("--board-date", type=int, help="override the session date (YYYYMMDD)")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    try:
        if args.fixture:
            fixture = args.fixture if args.fixture.is_absolute() else (ROOT / args.fixture)
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            rows = [row for row in payload.get("marketwatch") or [] if isinstance(row, dict)]
            if not rows:
                raise FetchError(f"{fixture} contains no marketwatch rows")
            board_date = args.board_date or _fixture_board_date(fixture)
            origin = f"fixture:{fixture.relative_to(ROOT) if fixture.is_relative_to(ROOT) else fixture}"
        else:
            rows = fetch_board(args.proxy)
            board_date = args.board_date or fetch_board_date(args.proxy)
            origin = "live"
    except (FetchError, OSError, ValueError) as exc:
        print(f"build_tse_universe: {exc}", file=sys.stderr)
        return 1

    instruments, excluded = build_instruments(rows, limit=args.limit, min_value=args.min_value)
    if not instruments:
        print("build_tse_universe: no valid instruments after filtering", file=sys.stderr)
        return 1

    digest = manifest_digest(instruments)
    payload = {
        "generated_at": started.isoformat(timespec="seconds"),
        "source": {
            "endpoint": f"{BASE}{MARKETWATCH_PATH}",
            "params": MARKETWATCH_PARAMS,
            "overview_endpoint": f"{BASE}{MARKET_OVERVIEW_PATH.format(flow=1)}",
            "origin": origin,
            "recorded_session_date": board_date,
            "ranking": "trade_value_rial descending, then ticker",
            "board_rows": len(rows),
            "user_agent": "browser-like; TSETMC returns JSON only to browser agents",
            "instrument_filter": "ordinary shares only: ISIN IRO1/IRO3/IRO5/IRO7 + serial 0001/0011",
        },
        "count": len(instruments),
        "digest": digest,
        "excluded": dict(sorted(excluded.items())),
        "instruments": instruments,
    }

    top = ", ".join(f"{row['ticker']} ({row['insCode']})" for row in instruments[:5])
    print(f"origin: {origin}")
    print(f"session date: {board_date or 'unknown'}")
    print(f"board rows: {len(rows)} | kept: {len(instruments)} | excluded: {dict(sorted(excluded.items()))}")
    print(f"top: {top}")
    print(f"digest: {digest[:16]}…")
    if args.no_write:
        print("dry run: manifest not written")
        return 0
    write_manifest(args.out, payload)
    print(f"wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
