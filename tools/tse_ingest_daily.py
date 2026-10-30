#!/usr/bin/env python3
"""Ingest Iranian Bourse daily bars under ``state/tse/`` — read-only, PAPER-only (D-040).

Source contract measured live on 2026-10-30 and recorded in ``DECISIONS.md`` (D-039/D-040):

* universe: ``GetMarketWatch`` with the same params the runtime provider uses, ordinary
  shares only, ranked by last-session trade value (``qtc``) — reused from
  ``tools/build_tse_universe.py`` so the two can never drift apart;
* bars: ``GetClosingPriceDailyList/{insCode}/{top}`` answers **exactly 200 rows** for any
  ``top``, so one year of calendar history is not purchasable with a bigger number. The
  tool reports ``depth_capped_by_source`` and the calendar shortfall instead of hiding it;
* a missing endpoint on this CDN can answer ``HTTP 200`` with an HTML shell, so every
  response must parse as JSON and carry the expected key (F-047/F-048).

Nothing here touches the crypto lake, ``config.yaml`` or any gate. No order path exists
for this venue and none is added: ``supports_live = False`` stays the only truth.

Usage:
    python tools/tse_ingest_daily.py --max-symbols 20      # pilot
    python tools/tse_ingest_daily.py                       # top-K full pass
    python tools/tse_ingest_daily.py --no-write            # audit only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

try:  # Persian ticker names must not crash a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:  # pragma: no cover - older interpreters
    pass

from build_tse_universe import FetchError, build_instruments, fetch_board, fetch_board_date, fetch_json  # noqa: E402
from cat.markets.tse import BASE, DAILY_PATH, SOURCE_MAX_DAILY_ROWS  # noqa: E402

DEFAULT_OUT = ROOT / "state" / "tse"
DEFAULT_TOP_K = 1000
DEFAULT_TARGET_CALENDAR_DAYS = 365
# The board only ever trades Saturday through Wednesday (calendar.py); a Thursday or
# Friday session row would mean the date mapping itself is wrong.
ALLOWED_WEEKDAYS = {5, 6, 0, 1, 2}


def session_date(d_even: Any) -> date | None:
    """TSETMC stamps ``dEven`` as a Gregorian ``YYYYMMDD`` integer (verified live)."""
    try:
        text = str(int(d_even))
    except (TypeError, ValueError):
        return None
    if len(text) != 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def bar_from_row(row: dict[str, Any], *, ins_code: str, board_date: int | None) -> tuple[dict[str, Any] | None, str | None]:
    """Map one TSETMC daily row to a bar, or return the reason it is unusable.

    Missing prices are dropped, never filled (F-029): a bar whose high is unknown cannot
    feed an ATR, and inventing one would be a fabricated observation.
    """
    day = session_date(row.get("dEven"))
    if day is None:
        return None, "invalid_session_date"
    session_int = int(day.strftime("%Y%m%d"))
    if board_date and session_int > int(board_date):
        return None, "session_after_board_date"
    if day.weekday() not in ALLOWED_WEEKDAYS:
        return None, "non_session_weekday"
    open_ = float(row.get("priceFirst") or 0.0)
    high = float(row.get("priceMax") or 0.0)
    low = float(row.get("priceMin") or 0.0)
    close = float(row.get("pClosing") or 0.0)
    volume = float(row.get("qTotTran5J") or 0.0)
    traded_value = float(row.get("qTotCap") or 0.0)
    if close <= 0:
        return None, "no_close"
    # Measured on the live board (pilot 2026-10-30): every row whose open/high/low are 0 also has
    # volume 0 and traded value 0, and its ``pClosing`` is simply carried over. That is a session
    # with no trade, not a broken row — so it is labelled as such and dropped, never filled.
    if volume <= 0 and traded_value <= 0:
        return None, "no_trading_session"
    if open_ <= 0:
        return None, "no_open"
    if high <= 0 or low <= 0:
        return None, "no_range"
    if high < low or high < max(open_, close) or low > min(open_, close):
        return None, "ohlc_inconsistent"
    return {
        "insCode": ins_code,
        "date": day.isoformat(),
        "session": session_int,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume_shares": volume,
        "traded_value_rial": traded_value,
        "trade_count": float(row.get("zTotTran") or 0.0),
    }, None


def bars_from_rows(rows: list[dict[str, Any]], *, ins_code: str, board_date: int | None) -> tuple[list[dict[str, Any]], Counter[str]]:
    bars: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    seen: set[int] = set()
    for row in rows:
        bar, reason = bar_from_row(row, ins_code=ins_code, board_date=board_date)
        if bar is None:
            dropped[reason or "unknown"] += 1
            continue
        if bar["session"] in seen:
            dropped["duplicate_session"] += 1
            continue
        seen.add(bar["session"])
        bars.append(bar)
    bars.sort(key=lambda item: item["session"])
    return bars, dropped


def audit_symbol(bars: list[dict[str, Any]], *, rows_requested: int, rows_returned: int,
                 target_start: date) -> dict[str, Any]:
    if not bars:
        return {"sessions": 0, "usable": False, "calendar_days": 0}
    oldest = date.fromisoformat(bars[0]["date"])
    newest = date.fromisoformat(bars[-1]["date"])
    calendar_days = (newest - oldest).days + 1
    capped = rows_returned >= SOURCE_MAX_DAILY_ROWS and oldest > target_start
    return {
        "sessions": len(bars),
        "usable": True,
        "oldest_session": bars[0]["session"],
        "newest_session": bars[-1]["session"],
        "calendar_days": calendar_days,
        "rows_requested": rows_requested,
        "rows_returned": rows_returned,
        "depth_capped_by_source": capped,
        "shortfall_days": max(0, (oldest - target_start).days),
    }


def aggregate_audit(per_symbol: dict[str, dict[str, Any]], dropped: Counter[str], *, universe_count: int,
                    fetch_errors: Counter[str], target_start: date, board_date: int | None) -> dict[str, Any]:
    usable = [audit for audit in per_symbol.values() if audit.get("usable")]
    sessions = [audit["sessions"] for audit in usable]
    rows_returned = sum(audit.get("rows_returned", 0) for audit in usable)
    calendar_days = [audit["calendar_days"] for audit in usable]
    capped = sum(1 for audit in usable if audit.get("depth_capped_by_source"))
    oldest = min((audit["oldest_session"] for audit in usable), default=0)
    duplicate_rows = dropped.get("duplicate_session", 0)
    future_rows = dropped.get("session_after_board_date", 0)
    weekday_rows = dropped.get("non_session_weekday", 0)
    verdict_ok = bool(usable) and duplicate_rows == 0 and future_rows == 0 and weekday_rows == 0 and not fetch_errors
    return {
        "universe_count": universe_count,
        "symbols_fetched": len(per_symbol),
        "usable_symbols": len(usable),
        "empty_symbols": len(per_symbol) - len(usable),
        "bars_total": sum(sessions),
        "rows_returned_total": rows_returned,
        "traded_session_share": round(sum(sessions) / rows_returned, 4) if rows_returned else 0.0,
        "sessions_median": int(sorted(sessions)[len(sessions) // 2]) if sessions else 0,
        "sessions_min": min(sessions, default=0),
        "sessions_max": max(sessions, default=0),
        "calendar_days_median": int(sorted(calendar_days)[len(calendar_days) // 2]) if calendar_days else 0,
        "oldest_session_overall": oldest,
        "newest_session_overall": max((audit["newest_session"] for audit in usable), default=0),
        "target_start_session": int(target_start.strftime("%Y%m%d")),
        "board_date": board_date,
        "symbols_depth_capped_by_source": capped,
        "dropped_rows": dict(sorted(dropped.items())),
        "fetch_errors": dict(sorted(fetch_errors.items())),
        "integrity_violations": duplicate_rows + future_rows + weekday_rows,
        "verdict": "usable" if verdict_ok else ("empty" if not usable else "degraded"),
    }


def bars_digest(all_bars: dict[str, list[dict[str, Any]]]) -> str:
    blob = json.dumps(
        {code: [bar["session"] for bar in bars] for code, bars in sorted(all_bars.items())},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def write_bars(out_dir: Path, code: str, bars: list[dict[str, Any]]) -> Path:
    path = out_dir / "bars" / f"{code}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(bar, ensure_ascii=False, sort_keys=True) + "\n" for bar in bars), encoding="utf-8")
    return path


def read_bars(path: Path) -> list[dict[str, Any]]:
    bars = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            bars.append(json.loads(line))
    return bars


def fetch_daily_rows(proxy: str | None, code: str, request_rows: int) -> list[dict[str, Any]]:
    payload = fetch_json(f"{BASE}{DAILY_PATH.format(ins=code, top=int(request_rows))}", proxy=proxy)
    if not isinstance(payload, dict) or not isinstance(payload.get("closingPriceDaily"), list):
        raise FetchError(f"{code}: response is not a closingPriceDaily payload")
    return [row for row in payload["closingPriceDaily"] if isinstance(row, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest TSE daily bars into state/tse (read-only venue)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="rank by trade value, keep the top N")
    parser.add_argument("--request-rows", type=int, default=SOURCE_MAX_DAILY_ROWS,
                        help="ask the source for N rows (it caps at %d anyway)" % SOURCE_MAX_DAILY_ROWS)
    parser.add_argument("--target-calendar-days", type=int, default=DEFAULT_TARGET_CALENDAR_DAYS)
    parser.add_argument("--max-symbols", type=int, default=0, help="stop after N symbols (0 = all; pilot runs)")
    parser.add_argument("--delay", type=float, default=0.25, help="seconds between requests; be polite to the source")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--no-resume", action="store_true", help="re-download bars already on disk")
    parser.add_argument("--no-write", action="store_true", help="audit without writing bars/manifest")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    try:
        board_rows = fetch_board(args.proxy)
        board_date = fetch_board_date(args.proxy)
    except FetchError as exc:
        print(f"tse_ingest_daily: board fetch failed: {exc}", file=sys.stderr)
        return 1
    if not board_date:
        print("tse_ingest_daily: no marketActivityDEven — refusing to guess the session date", file=sys.stderr)
        return 1

    universe, excluded = build_instruments(board_rows, limit=args.top_k)
    if not universe:
        print("tse_ingest_daily: no ordinary-share instruments on the board", file=sys.stderr)
        return 1
    if args.max_symbols and args.max_symbols > 0:
        universe = universe[: args.max_symbols]

    # The window is anchored to the venue's own session date, not this machine's clock
    # (the local clock is known to drift; a wrong "today" would silently shift the year).
    anchor = session_date(board_date) or datetime.now(timezone.utc).date()
    target_start = anchor - timedelta(days=int(args.target_calendar_days))
    bars_dir = args.out_dir / "bars"
    per_symbol: dict[str, dict[str, Any]] = {}
    dropped: Counter[str] = Counter()
    fetch_errors: Counter[str] = Counter()
    all_bars: dict[str, list[dict[str, Any]]] = {}
    reused = 0

    for index, item in enumerate(universe, 1):
        code = item["insCode"]
        path = bars_dir / f"{code}.jsonl"
        if path.exists() and not args.no_resume:
            bars = read_bars(path)
            rows_returned = len(bars)
            reused += 1
        else:
            try:
                rows = fetch_daily_rows(args.proxy, code, args.request_rows)
            except FetchError as exc:
                fetch_errors[type(exc).__name__] += 1
                per_symbol[code] = {"sessions": 0, "usable": False}
                continue
            bars, why = bars_from_rows(rows, ins_code=code, board_date=board_date)
            dropped.update(why)
            rows_returned = len(rows)
            if not args.no_write and bars:
                write_bars(args.out_dir, code, bars)
        all_bars[code] = bars
        per_symbol[code] = audit_symbol(bars, rows_requested=args.request_rows, rows_returned=rows_returned,
                                       target_start=target_start)
        time.sleep(max(0.0, args.delay))
        if index % 50 == 0 or index == len(universe):
            print(f"  {index}/{len(universe)} symbols | bars so far: {sum(len(b) for b in all_bars.values())}")

    audit = aggregate_audit(per_symbol, dropped, universe_count=len(universe), fetch_errors=fetch_errors,
                            target_start=target_start, board_date=board_date)
    manifest = {
        "generated_at": started.isoformat(timespec="seconds"),
        "venue": "tsetmc:cdn.tsetmc.com/api",
        "safety": {"mode": "read-only ingest", "orders_placed": 0, "live": False, "credentials_used": False},
        "universe": {
            "ranking": "trade_value_rial (qtc) descending, then ticker",
            "filter": "ordinary shares only (ISIN IRO1/IRO3/IRO5/IRO7 + serial 0001/0011)",
            "board_rows": len(board_rows),
            "requested_top_k": args.top_k,
            "effective_k": len(universe),
            "excluded": dict(sorted(excluded.items())),
        },
        "source_contract": {
            "daily_endpoint": f"{BASE}{DAILY_PATH}",
            "rows_requested_per_symbol": args.request_rows,
            "source_row_cap": SOURCE_MAX_DAILY_ROWS,
            "paging_available": False,
            "note": "top>200 still returns 200 rows; no deeper-history endpoint answered JSON on 2026-10-30",
            "zero_ohlc_rows_mean": "no trade that session (volume and traded value are 0 too); dropped, never filled",
        },
        "target": {"calendar_days": int(args.target_calendar_days), "start_date": target_start.isoformat()},
        "run": {"reused_from_disk": reused, "max_symbols": args.max_symbols, "delay_seconds": args.delay},
        "audit": audit,
        "bars_digest": bars_digest(all_bars),
    }
    if not args.no_write:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "tse_daily_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({k: manifest[k] for k in ("universe", "source_contract", "target", "audit")},
                     ensure_ascii=False, indent=2))
    print(f"wrote: {args.out_dir / 'tse_daily_manifest.json' if not args.no_write else '(dry run)'}")
    return 0 if audit["verdict"] in ("usable", "degraded") and audit["usable_symbols"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
