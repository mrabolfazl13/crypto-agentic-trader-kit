"""Assemble a not-yet-published month from Binance Vision *daily* archives.

The monthly zip for the current month only appears after the month closes, which is
the only thing blocking a third forward window (see NEXT_ACTION item 11). Daily
archives are published one day late, so 2026-10-01..10-26 is already available.

Two rules keep this from contaminating the evidence that exists today:

* daily bars are stored under ``raw/1m/<symbol>/daily/`` and recorded in
  ``manifest_daily.json``, never in ``manifest.json`` — so ``current_data_version()``
  still returns the digest every prior record was written against;
* a composed month file is written only where no month file exists, and a month the
  monthly manifest already claims is refused. A monthly archive is never overwritten
  or shadowed.

Validation is the same predicate as the monthly path (shared ``_validate_rows``), and
a gap is still reported rather than filled.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from .ingest import (
    IngestError,
    IngestStopped,
    _request_bytes,
    day_window,
    daily_source_url,
    month_window,
    parse_day_archive,
)
from .settings import QuantSettings
from .storage import (
    StorageBudget,
    StorageError,
    day_path,
    file_sha256,
    load_manifest,
    load_universe,
    manifest_digest,
    month_path,
    read_parquet_frame,
    save_manifest,
    symbol_months,
    utc_now,
    validate_coverage,
    write_parquet_atomic,
)

MANIFEST_DAILY_SCHEMA = "quant-manifest-daily-v1"


def dates_in_range(first: str, last: str) -> list[str]:
    """Inclusive UTC calendar-day range, ``YYYY-MM-DD``."""
    try:
        start = datetime.strptime(first, "%Y-%m-%d")
        end = datetime.strptime(last, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise IngestError(f"invalid date range {first!r}..{last!r}, expected YYYY-MM-DD") from exc
    if end < start:
        raise IngestError(f"date range ends before it starts: {first}..{last}")
    return [(start + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range((end - start).days + 1)]


def empty_daily_manifest(settings: QuantSettings) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_DAILY_SCHEMA,
        "generated_at": utc_now(),
        "source": {
            "name": "Binance Vision",
            "url": "https://data.binance.vision/",
            "dataset": "spot daily klines, 1m",
            "license": "Binance public market data archive terms (https://data.binance.vision/terms-of-use.html)",
        },
        "months": [],
        "symbols": {},
        "errors": [],
        "totals": {"symbols": 0, "months": 0, "rows": 0, "bytes": 0, "missing_minutes": 0, "gaps": 0},
    }


def load_daily_manifest(settings: QuantSettings) -> dict[str, Any]:
    """The daily record, or a fresh one; a foreign schema version is not merged."""
    if not settings.manifest_daily_path.exists():
        return empty_daily_manifest(settings)
    manifest = load_manifest(settings, settings.manifest_daily_path)
    if manifest.get("schema_version") != MANIFEST_DAILY_SCHEMA:
        manifest = empty_daily_manifest(settings)
    manifest.setdefault("errors", [])
    manifest.setdefault("symbols", {})
    return manifest


def _month_of(date: str) -> str:
    return date[:7]


def _reserve_bytes(raw_len: int) -> int:
    # Same 2x rule as the monthly path: parquet+zstd measured smaller than the zip for
    # every observed archive, so the reservation cannot overshoot the cap.
    return int(raw_len * 2.0) + 1 * 1024 * 1024


def _existing_day(settings: QuantSettings, symbol: str, date: str) -> dict[str, Any] | None:
    """Re-measure a stored day instead of trusting it: resume costs no download."""
    path = day_path(settings, symbol, date)
    if not path.exists():
        return None
    try:
        frame = read_parquet_frame(path)
        coverage = validate_coverage(frame, date)
    except Exception:
        return None
    if not coverage["rows"]:
        return None
    return {
        **coverage,
        "day": date,
        "month": _month_of(date),
        "expected_minutes": day_window(date).expected_minutes,
        "duplicates": 0,
        "invalid_rows": 0,
        "rows_read": coverage["rows"],
        "source_url": daily_source_url(symbol, date),
        "source_bytes": None,
        "source_sha256": None,
        "file_sha256": file_sha256(path),
        "stored_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "file": str(path.relative_to(settings.data_dir)),
        "bytes": path.stat().st_size,
        "status": "ok" if coverage["coverage_pct"] >= 99.0 else "partial",
        "resumed": True,
    }


def store_day(settings: QuantSettings, budget: StorageBudget, symbol: str, date: str,
              stop_event: Any | None = None) -> dict[str, Any]:
    """Download, validate and persist one symbol-day. Resumable at day granularity."""
    symbol = symbol.upper()
    existing = _existing_day(settings, symbol, date)
    if existing is not None:
        return existing
    raw = _request_bytes(daily_source_url(symbol, date), proxy=settings.proxy,
                         timeout=settings.ingest.timeout_seconds, retries=settings.ingest.retries,
                         stop_event=stop_event)
    frame, record = parse_day_archive(raw, symbol, date)
    path = day_path(settings, symbol, date)
    reserve = _reserve_bytes(len(raw))
    with budget.reserve_for_write(reserve, "raw"):
        written = write_parquet_atomic(frame, path, budget, category="raw", already_reserved=True)
        if written > reserve:
            raise StorageError(f"daily parquet {path.name} is {written} bytes, above its {reserve}-byte reservation")
    record.update({"file": str(path.relative_to(settings.data_dir)), "bytes": written,
                   "file_sha256": file_sha256(path), "resumed": False})
    return record


def monthly_manifest_has_month(settings: QuantSettings, symbol: str, month: str) -> bool:
    """True when the authoritative monthly lake already stores this month."""
    return month in symbol_months(load_manifest(settings), symbol)


def stored_days(settings: QuantSettings, symbol: str, month: str,
                manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """The day records the daily manifest holds for one symbol-month."""
    manifest = manifest if manifest is not None else load_daily_manifest(settings)
    record = (manifest.get("symbols") or {}).get(symbol.upper()) or {}
    days = record.get("days") if isinstance(record.get("days"), dict) else {}
    return {day: entry for day, entry in sorted(days.items()) if _month_of(day) == month}


def compose_month(settings: QuantSettings, budget: StorageBudget, symbol: str, month: str,
                  days: dict[str, Any] | None = None) -> dict[str, Any]:
    """Concatenate the stored days of one month into the usual month parquet.

    Writing the month file in the same layout is what lets ``read_symbol``,
    ``load_bars`` and every downstream resample use the shipped code path — no second
    reader, so no second set of semantics to keep honest. ``days`` lets a caller that
    just stored them pass them directly; the default is the persisted daily manifest.
    """
    symbol = symbol.upper()
    daily_manifest = load_daily_manifest(settings)
    if days is None:
        days = stored_days(settings, symbol, month, daily_manifest)
    paths = [day_path(settings, symbol, day) for day in days]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise StorageError(f"{symbol} {month}: {len(missing)} daily file(s) vanished: {missing[0]}")
    if not paths:
        raise StorageError(f"{symbol} {month}: no daily bars stored for this month")

    target = month_path(settings, symbol, month)
    already_ours = month in ((daily_manifest.get("symbols") or {}).get(symbol, {}).get("months") or {})
    if target.exists() and not already_ours:
        raise StorageError(
            f"{symbol} {month}: a month file exists that the daily manifest did not build; "
            f"the monthly archive is authoritative, refusing to overwrite {target.name}")
    if monthly_manifest_has_month(settings, symbol, month):
        raise StorageError(f"{symbol} {month}: present in manifest.json; the daily path never shadows it")

    frame = pd.concat([read_parquet_frame(path) for path in paths], ignore_index=True)
    frame = frame.sort_values("ts", kind="stable").drop_duplicates(subset="ts", keep="last").reset_index(drop=True)
    if frame.empty:
        raise StorageError(f"{symbol} {month}: composed frame is empty")

    coverage = validate_coverage(frame, month)
    ordered = sorted(days.items())
    # Sorted by day: thread completion order varies between runs, and a provenance
    # digest that moves when the data has not would make every record unreadable.
    provenance = hashlib.sha256("|".join(f"{day}:{entry.get('file_sha256')}"
                                        for day, entry in ordered).encode("utf-8")).hexdigest()
    reserve = _reserve_bytes(sum(int(entry.get("source_bytes") or 0) for entry in days.values()) or 1024 * 1024)
    if target.exists():
        written = target.stat().st_size
    else:
        with budget.reserve_for_write(reserve, "raw"):
            written = write_parquet_atomic(frame, target, budget, category="raw", already_reserved=True)
    return {
        **coverage,
        "month": month,
        "expected_minutes": month_window(month).expected_minutes,
        "days": len(days),
        "day_dates": sorted(days),
        "rows_read": int(len(frame)),
        "bytes": written,
        "file": str(target.relative_to(settings.data_dir)),
        "source": "binance_vision_1m_daily",
        "source_sha256": provenance,
        "day_file_sha256": {day: entry.get("file_sha256") for day, entry in ordered},
        "stored_at": utc_now(),
        "status": "ok" if coverage["coverage_pct"] >= 99.0 else "partial",
    }


def _symbol_record(days: dict[str, Any], months: dict[str, Any]) -> dict[str, Any]:
    rows = sum(int(entry.get("rows") or 0) for entry in months.values())
    first = min((entry.get("first_ts") for entry in months.values() if entry.get("first_ts")), default=None)
    last = max((entry.get("last_ts") for entry in months.values() if entry.get("last_ts")), default=None)
    return {
        "rows": rows,
        "bytes": sum(int(entry.get("bytes") or 0) for entry in months.values()),
        "missing_minutes": sum(int(entry.get("missing_minutes") or 0) for entry in months.values()),
        "gaps": sum(int(entry.get("gaps") or 0) for entry in months.values()),
        "first_ts": first,
        "last_ts": last,
        "days": days,
        "months": months,
    }


def ingest_daily(settings: QuantSettings, *, symbols: list[str] | None = None, dates: list[str] | None = None,
                 progress: Callable[[dict[str, Any]], None] | None = None,
                 stop_event: Any | None = None, limit: int | None = None) -> dict[str, Any]:
    """Store the requested symbol-days, then compose each month they cover.

    A month already claimed by ``manifest.json`` is refused before anything downloads.
    """
    if not settings.enabled:
        raise IngestError("quant engine is disabled in config.yaml")
    universe = load_universe(settings)
    selected = [str(symbol).upper() for symbol in (symbols or universe["_symbols"])]
    if limit is not None:
        selected = selected[: max(0, int(limit))]
    unknown = sorted(set(selected) - set(universe["_symbols"]))
    if unknown:
        raise IngestError(f"symbols outside the recorded universe: {', '.join(unknown[:10])}")
    dates = list(dict.fromkeys(dates or []))
    if not dates:
        raise IngestError("ingest_daily needs at least one date")
    for date in dates:
        day_window(date)

    months = sorted({_month_of(date) for date in dates})
    for month in months:
        blocked = [symbol for symbol in selected if monthly_manifest_has_month(settings, symbol, month)]
        if blocked:
            raise IngestError(
                f"{month} is already stored from the monthly archive for {len(blocked)} symbol(s) "
                f"(e.g. {blocked[0]}); the daily path never overwrites it")

    manifest = load_daily_manifest(settings)
    manifest["months"] = sorted(set(manifest.get("months", [])) | set(months))
    budget = StorageBudget(settings)
    tasks = [(symbol, date) for symbol in selected for date in dates]
    total = len(tasks)
    done = 0
    errors: list[dict[str, Any]] = []
    started = time.monotonic()
    stopped = False
    records: dict[str, dict[str, Any]] = {symbol: {} for symbol in selected}

    def emit(message: str, **extra: Any) -> None:
        if progress:
            progress({"phase": "ingest_daily", "done": done, "total": total, "message": message,
                      "errors": len(errors), "elapsed_seconds": round(time.monotonic() - started, 2), **extra})

    emit("starting daily ingest")
    concurrency = max(1, min(settings.ingest.concurrency, 16))
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="quant-daily") as pool:
        pending: dict[Future, tuple[str, str]] = {}
        iterator = iter(tasks)

        def submit_next() -> bool:
            try:
                symbol, date = next(iterator)
            except StopIteration:
                return False
            pending[pool.submit(store_day, settings, budget, symbol, date, stop_event)] = (symbol, date)
            return True

        for _ in range(min(concurrency, total)):
            if not submit_next():
                break
        while pending:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                for future in pending:
                    future.cancel()
                break
            for future in wait(set(pending), timeout=5, return_when=FIRST_COMPLETED)[0]:
                symbol, date = pending.pop(future)
                try:
                    records[symbol][date] = future.result()
                except IngestStopped:
                    stopped = True
                except (StorageError, IngestError) as exc:
                    errors.append({"symbol": symbol, "day": date, "error": f"{type(exc).__name__}: {exc}"})
                done += 1
                if done % 25 == 0 or done == total:
                    emit(f"{done}/{total} symbol-days", symbol=symbol, day=date)
                while len(pending) < concurrency and not stopped and submit_next():
                    pass

    composed: dict[str, dict[str, int]] = {}
    for symbol in selected:
        days = records[symbol]
        if not days:
            continue
        symbol_months_map: dict[str, Any] = {}
        for month in months:
            if not any(_month_of(date) == month for date in days):
                continue
            try:
                symbol_months_map[month] = compose_month(
                    settings, budget, symbol, month,
                    days={day: entry for day, entry in days.items() if _month_of(day) == month})
            except (StorageError, IngestError) as exc:
                errors.append({"symbol": symbol, "month": month, "error": f"{type(exc).__name__}: {exc}"})
        previous = (manifest.get("symbols") or {}).get(symbol, {})
        manifest.setdefault("symbols", {})[symbol] = _symbol_record(
            {**(previous.get("days") or {}), **days},
            {**(previous.get("months") or {}), **symbol_months_map})
        composed[symbol] = {month: int(entry["rows"]) for month, entry in symbol_months_map.items()}

    manifest["errors"] = errors
    save_manifest(settings, manifest, settings.manifest_daily_path)
    return {
        "status": "stopped" if stopped else ("error" if errors and not composed else
                                             "partial" if errors else "complete"),
        "phase": "ingest_daily",
        "symbols": len(selected),
        "dates": len(dates),
        "days_stored": sum(len(entry) for entry in records.values()),
        "months": months,
        "composed_rows": composed,
        "rows_composed": sum(rows for per_month in composed.values() for rows in per_month.values()),
        "errors": errors[:50],
        "error_count": len(errors),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "daily_manifest": settings.manifest_daily_path.name,
        "digest": manifest_digest(manifest),
        "generated_at": utc_now(),
    }


def daily_coverage(settings: QuantSettings) -> dict[str, Any]:
    """What the daily manifest currently holds, per month."""
    manifest = load_daily_manifest(settings)
    per_month: dict[str, dict[str, Any]] = {}
    for record in (manifest.get("symbols") or {}).values():
        for month, entry in (record.get("months") or {}).items():
            bucket = per_month.setdefault(month, {"symbols": 0, "rows": 0, "bytes": 0, "days": set()})
            bucket["symbols"] += 1
            bucket["rows"] += int(entry.get("rows") or 0)
            bucket["bytes"] += int(entry.get("bytes") or 0)
            bucket["days"] |= set(entry.get("day_dates") or [])
    months_out = {}
    for month, bucket in sorted(per_month.items()):
        dates = sorted(bucket.pop("days"))
        span = {"first": dates[0] if dates else None, "last": dates[-1] if dates else None}
        if dates:
            span["expected_days"] = (datetime.strptime(dates[-1], "%Y-%m-%d")
                                     - datetime.strptime(dates[0], "%Y-%m-%d")).days + 1
        months_out[month] = {**bucket, "day_count": len(dates), "span": span}
    return {"schema_version": manifest.get("schema_version"),
            "digest": manifest.get("digest") or manifest_digest(manifest),
            "months": months_out}
