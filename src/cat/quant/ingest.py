"""Download Binance Vision 1m archives, validate them and store Parquet.

The archive is the public Binance market-data bucket (``data.binance.vision``),
one monthly zip per symbol. Downloading is resumable at file granularity: an
existing parquet file is re-measured and skipped, never trusted blindly.

No synthetic bar is ever created. Missing minutes are counted and reported; the
gap is preserved in the stored series so a backtest cannot accidentally treat
two distant bars as adjacent.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import os
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

from .settings import QuantSettings
from .storage import (
    BAR_COLUMNS,
    StorageBudget,
    StorageBudgetExceeded,
    StorageError,
    load_manifest,
    load_universe,
    month_path,
    read_month,
    save_manifest,
    storage_report,
    summarize_symbol,
    validate_coverage,
    write_parquet_atomic,
)

BASE_URL = "https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{month}.zip"
USER_AGENT = "cat-quant-research/1.0 (local paper research)"
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024


class IngestError(RuntimeError):
    pass


class IngestStopped(IngestError):
    pass


@dataclass(frozen=True)
class MonthWindow:
    month: str
    start_ts: int
    end_ts: int
    expected_minutes: int


def month_window(month: str) -> MonthWindow:
    try:
        year, month_number = (int(part) for part in month.split("-"))
        days = calendar.monthrange(year, month_number)[1]
    except (ValueError, TypeError) as exc:
        raise IngestError(f"invalid month {month!r}, expected YYYY-MM") from exc
    start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=timezone.utc)
    return MonthWindow(month=month, start_ts=int(start.timestamp()), end_ts=int(end.timestamp()),
                       expected_minutes=days * 1440)


def source_url(symbol: str, month: str) -> str:
    return BASE_URL.format(symbol=symbol.upper(), month=month)


def _timestamp_seconds(values: pd.Series) -> pd.Series:
    """Binance archives switched to microseconds; older files are milliseconds."""
    first = int(values.iloc[0])
    if first >= 1_000_000_000_000_000:  # microseconds
        seconds = values // 1_000_000
    elif first >= 1_000_000_000_000:  # milliseconds
        seconds = values // 1_000
    else:
        seconds = values
    return seconds.astype("int64")


def parse_month_archive(raw: bytes, symbol: str, month: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parse one Binance Vision zip entirely in memory.

    The CSV has no header and twelve columns: open_time, OHLC, volume,
    close_time, quote_volume, trade_count, taker volumes, ignore.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise IngestError(f"{symbol} {month}: expected one CSV in the archive, got {names}")
            csv_bytes = archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{symbol} {month}: archive is not a valid zip") from exc

    try:
        frame = pd.read_csv(
            io.BytesIO(csv_bytes), header=None, usecols=[0, 1, 2, 3, 4, 5],
            names=["raw_ts", "open", "high", "low", "close", "volume"],
            dtype={"raw_ts": "int64", "open": "float64", "high": "float64", "low": "float64",
                   "close": "float64", "volume": "float64"},
            engine="c",
        )
    except Exception as exc:
        raise IngestError(f"{symbol} {month}: CSV could not be parsed ({type(exc).__name__}: {exc})") from exc
    if frame.empty:
        raise IngestError(f"{symbol} {month}: archive CSV is empty")

    window = month_window(month)
    total_rows = int(len(frame))
    frame["ts"] = _timestamp_seconds(frame["raw_ts"])
    off_grid = (frame["ts"] % 60) != 0
    outside = (frame["ts"] < window.start_ts) | (frame["ts"] >= window.end_ts)
    duplicates = int(frame["ts"].duplicated().sum())
    finite = frame[["open", "high", "low", "close", "volume"]].apply(lambda col: col.notna()).all(axis=1)
    positive = (frame[["open", "high", "low", "close"]] > 0).all(axis=1) & (frame["volume"] >= 0)
    coherent = (
        (frame["high"] >= frame[["open", "close"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close"]].min(axis=1))
        & (frame["high"] >= frame["low"])
    )
    valid = finite & positive & coherent & ~off_grid & ~outside
    invalid_rows = int((~valid).sum())
    clean = (frame.loc[valid, ["ts", "open", "high", "low", "close", "volume"]]
             .sort_values("ts", kind="stable")
             .drop_duplicates(subset="ts", keep="last")
             .reset_index(drop=True))
    if clean.empty:
        raise IngestError(f"{symbol} {month}: no valid 1m rows survived validation")

    coverage = validate_coverage(clean, month)
    record = {
        **coverage,
        "expected_minutes": window.expected_minutes,
        "duplicates": duplicates,
        "invalid_rows": invalid_rows,
        "rows_read": total_rows,
        "source_url": source_url(symbol, month),
        "source_bytes": len(raw),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok" if coverage["coverage_pct"] >= 99.0 else "partial",
    }
    return clean, record


def _request_bytes(url: str, *, proxy: str | None, timeout: int, retries: int,
                   stop_event: Any | None = None) -> bytes:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if stop_event is not None and stop_event.is_set():
            raise IngestStopped("ingest stopped by operator")
        try:
            with requests.get(url, stream=True, timeout=(10, timeout), proxies=proxies,
                              headers={"User-Agent": USER_AGENT}) as response:
                if response.status_code == 404:
                    raise IngestError(f"archive not found (404): {url}")
                response.raise_for_status()
                length = int(response.headers.get("Content-Length") or 0)
                if length > MAX_ARCHIVE_BYTES:
                    raise IngestError(f"archive is unexpectedly large ({length} bytes): {url}")
                buffer = bytearray()
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if chunk:
                        buffer.extend(chunk)
                        if len(buffer) > MAX_ARCHIVE_BYTES:
                            raise IngestError(f"archive exceeded {MAX_ARCHIVE_BYTES} bytes while streaming: {url}")
                if not buffer:
                    raise IngestError(f"empty response: {url}")
                return bytes(buffer)
        except IngestError:
            raise
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 8))
    raise IngestError(f"download failed after {retries + 1} attempts: {last_error}")


def _existing_record(settings: QuantSettings, symbol: str, month: str) -> dict[str, Any] | None:
    path = month_path(settings, symbol, month)
    if not path.exists():
        return None
    try:
        frame = read_month(settings, symbol, month)
        coverage = validate_coverage(frame, month)
    except Exception:
        return None
    if not coverage["rows"]:
        return None
    return {
        **coverage,
        "expected_minutes": month_window(month).expected_minutes,
        "duplicates": 0,
        "invalid_rows": 0,
        "rows_read": coverage["rows"],
        "source_url": source_url(symbol, month),
        "source_bytes": None,
        "source_sha256": None,
        "stored_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "file": str(path.relative_to(settings.data_dir)),
        "bytes": path.stat().st_size,
        "status": "ok" if coverage["coverage_pct"] >= 99.0 else "partial",
        "resumed": True,
    }


def _store_one(settings: QuantSettings, budget: StorageBudget, symbol: str, month: str,
               stop_event: Any | None = None) -> dict[str, Any]:
    existing = _existing_record(settings, symbol, month)
    if existing is not None:
        return existing
    url = source_url(symbol, month)
    raw = _request_bytes(url, proxy=settings.proxy, timeout=settings.ingest.timeout_seconds,
                         retries=settings.ingest.retries, stop_event=stop_event)
    frame, record = parse_month_archive(raw, symbol, month)
    path = month_path(settings, symbol, month)
    # Parquet with zstd is smaller than the archive for every observed month; the
    # 2x reservation is deliberately conservative so the write cannot overshoot.
    reserve = int(len(raw) * 2.0) + 1 * 1024 * 1024
    with budget.reserve_for_write(reserve, "raw"):
        written = write_parquet_atomic(frame, path, budget, category="raw", already_reserved=True)
        if written > reserve:
            raise StorageError(
                f"parquet file {path.name} is {written} bytes, above its {reserve}-byte reservation"
            )
    record.update({
        "file": str(path.relative_to(settings.data_dir)),
        "bytes": written,
        "resumed": False,
    })
    return record


def ingest(settings: QuantSettings, *, symbols: list[str] | None = None, months: list[str] | None = None,
           progress: Callable[[dict[str, Any]], None] | None = None,
           stop_event: Any | None = None, limit: int | None = None) -> dict[str, Any]:
    """Download/validate the requested symbol-months and persist the manifest.

    Returns a summary with ``status`` in ``complete|partial|stopped|error``. A
    budget breach is a hard failure: the job stops and the report says why.
    """
    if not settings.enabled:
        raise IngestError("quant engine is disabled in config.yaml")
    universe = load_universe(settings)
    selected = list(symbols or universe["_symbols"])
    if limit is not None:
        selected = selected[: max(0, int(limit))]
    selected = [symbol.upper() for symbol in selected]
    unknown = sorted(set(selected) - set(universe["_symbols"]))
    if unknown:
        raise IngestError(f"symbols outside the recorded universe: {', '.join(unknown[:10])}")
    months = [str(month) for month in (months or settings.months)]
    for month in months:
        month_window(month)

    manifest = load_manifest(settings)
    manifest.setdefault("symbols", {})
    manifest["months"] = sorted(set(manifest.get("months", [])) | set(months))
    budget = StorageBudget(settings)
    tasks = [(symbol, month) for symbol in selected for month in months]
    total = len(tasks)
    done = 0
    errors: list[dict[str, Any]] = []
    started = time.monotonic()
    stopped = False
    budget_error: str | None = None

    def emit(message: str, **extra: Any) -> None:
        if progress:
            progress({"phase": "ingest", "done": done, "total": total, "message": message,
                      "errors": len(errors), "elapsed_seconds": round(time.monotonic() - started, 2), **extra})

    emit("starting ingest")
    concurrency = max(1, min(settings.ingest.concurrency, 16))
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="quant-ingest") as pool:
        pending: dict[Future, tuple[str, str]] = {}
        iterator = iter(tasks)

        def submit_next() -> bool:
            try:
                symbol, month = next(iterator)
            except StopIteration:
                return False
            pending[pool.submit(_store_one, settings, budget, symbol, month, stop_event)] = (symbol, month)
            return True

        for _ in range(concurrency):
            if not submit_next():
                break
        while pending:
            if stop_event is not None and stop_event.is_set():
                stopped = True
            finished, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
            if not finished:
                if stopped:
                    break
                continue
            for future in finished:
                symbol, month = pending.pop(future)
                done += 1
                try:
                    record = future.result()
                except StorageBudgetExceeded as exc:
                    budget_error = str(exc)
                    errors.append({"symbol": symbol, "month": month, "error": budget_error,
                                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    stopped = True
                    continue
                except IngestStopped:
                    stopped = True
                    continue
                except Exception as exc:
                    errors.append({"symbol": symbol, "month": month, "error": f"{type(exc).__name__}: {exc}",
                                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    emit(f"{symbol} {month}: {type(exc).__name__}", symbol=symbol, month=month, error=str(exc))
                    continue
                symbol_record = manifest["symbols"].setdefault(symbol, {"symbol": symbol, "months": {}})
                symbol_record.setdefault("months", {})[month] = record
                manifest["symbols"][symbol] = summarize_symbol(symbol_record)
                emit(f"{symbol} {month}: {record['rows']} bars", symbol=symbol, month=month,
                     bytes=record.get("bytes"), coverage_pct=record.get("coverage_pct"))
                if stopped or (stop_event is not None and stop_event.is_set()):
                    stopped = True
            if stopped:
                break
            while len(pending) < concurrency and submit_next():
                pass
        for future in pending:
            future.cancel()
    if stop_event is not None and stop_event.is_set():
        stopped = True

    stored_pairs = {(symbol, str(month)) for symbol, record in manifest["symbols"].items()
                    for month in (record.get("months") or {})}
    manifest["errors"] = [err for err in (manifest.get("errors", []) + errors)
                          if (err.get("symbol"), err.get("month")) not in stored_pairs][-200:]
    for symbol in selected:
        if symbol in manifest["symbols"]:
            manifest["symbols"][symbol] = summarize_symbol(manifest["symbols"][symbol])
    save_manifest(settings, manifest)
    report = storage_report(settings)
    emit("ingest finished", stopped=stopped, budget_error=budget_error)
    status = "error" if budget_error else ("stopped" if stopped else ("partial" if errors else "complete"))
    return {
        "status": status,
        "symbols_requested": len(selected),
        "months": months,
        "files_requested": total,
        "files_succeeded": total - len(errors) if not stopped else None,
        "errors": errors,
        "budget_error": budget_error,
        "manifest_digest": manifest.get("digest"),
        "coverage": {
            "symbols_stored": len([s for s in manifest["symbols"] if manifest["symbols"][s].get("months")]),
            "columns": list(BAR_COLUMNS),
            "totals": manifest.get("totals"),
        },
        "storage": report,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def preflight(settings: QuantSettings, *, symbols: list[str] | None = None,
              months: list[str] | None = None) -> dict[str, Any]:
    """HEAD-only availability/size check before a long ingest (bounded requests)."""
    universe = load_universe(settings)
    selected = [symbol.upper() for symbol in (symbols or universe["_symbols"])]
    months = [str(month) for month in (months or settings.months)]
    proxies = {"http": settings.proxy, "https": settings.proxy} if settings.proxy else None
    results: list[dict[str, Any]] = []
    for symbol in selected:
        for month in months:
            url = source_url(symbol, month)
            try:
                response = requests.head(url, timeout=settings.ingest.timeout_seconds, proxies=proxies,
                                         headers={"User-Agent": USER_AGENT}, allow_redirects=True)
                size = int(response.headers.get("Content-Length") or 0)
                results.append({"symbol": symbol, "month": month, "status": response.status_code,
                                "bytes": size, "url": url})
            except requests.RequestException as exc:
                results.append({"symbol": symbol, "month": month, "status": None, "bytes": 0,
                                "error": f"{type(exc).__name__}: {exc}", "url": url})
    available = [row for row in results if row["status"] == 200]
    bytes_total = sum(row["bytes"] for row in available)
    return {
        "checked": len(results),
        "available": len(available),
        "missing": [row for row in results if row["status"] != 200],
        "compressed_bytes": bytes_total,
        "compressed_gib": round(bytes_total / (1024 ** 3), 3),
        "estimated_parquet_gib": round(bytes_total * 0.78 / (1024 ** 3), 3),
        "storage": storage_report(settings),
    }
