"""Parquet data lake, manifest and the hard storage budget.

Rules enforced here, not in prompts:
* every write is checked against the per-category and total caps before it happens;
* a budget breach raises and stops the job, it never deletes existing data;
* downloads are parsed in memory and the compressed archive is discarded only
  after the parquet file has been written atomically;
* the manifest is the single source of truth for what was actually stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .settings import QuantSettings

GIB = 1024 ** 3
MIB = 1024 ** 2
MANIFEST_SCHEMA = "quant-manifest-v1"
BAR_COLUMNS = ("ts", "open", "high", "low", "close", "volume")


class StorageError(RuntimeError):
    pass


class StorageBudgetExceeded(StorageError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def free_disk_bytes(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return 0


def _category_sizes(settings: QuantSettings) -> dict[str, int]:
    data_dir = settings.data_dir
    raw = path_size(settings.raw_dir)
    features = path_size(settings.features_dir)
    models = path_size(settings.models_dir)
    total = path_size(data_dir)
    # results owns everything that is not raw data, derived features or a model.
    results = max(0, total - raw - features - models)
    return {"raw": raw, "features": features, "models": models, "results": results, "total": total}


def storage_report(settings: QuantSettings) -> dict[str, Any]:
    sizes = _category_sizes(settings)
    budget = settings.storage
    caps = {
        "raw": int(budget.raw_gb * GIB),
        "features": int(budget.features_gb * GIB),
        "models": int(budget.models_gb * GIB),
        "results": int(budget.results_gb * GIB),
        "total": int(budget.max_total_gb * GIB),
    }
    usable_total = int((budget.max_total_gb - budget.reserve_gb) * GIB)
    categories = {}
    for name in ("raw", "features", "models", "results"):
        categories[name] = {
            "bytes": sizes[name],
            "cap_bytes": caps[name],
            "used_pct": round(sizes[name] / caps[name] * 100.0, 2) if caps[name] else None,
            "within_budget": sizes[name] <= caps[name],
        }
    return {
        "data_dir": str(settings.data_dir),
        "categories": categories,
        "total_bytes": sizes["total"],
        "max_total_bytes": caps["total"],
        "usable_total_bytes": usable_total,
        "reserve_bytes": int(budget.reserve_gb * GIB),
        "used_pct": round(sizes["total"] / caps["total"] * 100.0, 2) if caps["total"] else None,
        "within_budget": sizes["total"] <= usable_total,
        "disk_free_bytes": free_disk_bytes(settings.data_dir),
        "disk_free_gib": round(free_disk_bytes(settings.data_dir) / GIB, 2),
        "policy": "a breach stops the job; existing data is never deleted to make room",
    }


class StorageBudget:
    """Serialises reservations so parallel downloads cannot jointly overshoot."""

    def __init__(self, settings: QuantSettings):
        self.settings = settings
        self._lock = threading.Lock()
        self._reserved = 0

    def check(self, additional_bytes: int, category: str = "raw", *, free_margin_bytes: int = 256 * MIB) -> dict[str, Any]:
        if additional_bytes < 0:
            raise StorageError("additional bytes must be >= 0")
        if category not in {"raw", "features", "models", "results"}:
            raise StorageError(f"unknown storage category {category!r}")
        with self._lock:
            sizes = _category_sizes(self.settings)
            caps = {
                "raw": int(self.settings.storage.raw_gb * GIB),
                "features": int(self.settings.storage.features_gb * GIB),
                "models": int(self.settings.storage.models_gb * GIB),
                "results": int(self.settings.storage.results_gb * GIB),
            }
            usable_total = int((self.settings.storage.max_total_gb - self.settings.storage.reserve_gb) * GIB)
            projected_total = sizes["total"] + self._reserved + additional_bytes
            if projected_total > usable_total:
                report = storage_report(self.settings)
                report["additional_bytes"] = additional_bytes
                report["reserved_bytes"] = self._reserved
                raise StorageBudgetExceeded(
                    f"storage budget would be exceeded: {projected_total / MIB:.1f} MiB > "
                    f"{usable_total / MIB:.1f} MiB usable (reserve {self.settings.storage.reserve_gb} GiB)",
                    report,
                )
            if sizes[category] + self._reserved + additional_bytes > caps[category]:
                report = storage_report(self.settings)
                report["additional_bytes"] = additional_bytes
                report["category"] = category
                raise StorageBudgetExceeded(
                    f"{category} budget would be exceeded: "
                    f"{(sizes[category] + self._reserved + additional_bytes) / MIB:.1f} MiB > "
                    f"{caps[category] / MIB:.1f} MiB",
                    report,
                )
            free = free_disk_bytes(self.settings.data_dir)
            if free and additional_bytes + free_margin_bytes > free:
                report = storage_report(self.settings)
                report["additional_bytes"] = additional_bytes
                raise StorageBudgetExceeded(
                    f"not enough free disk space: need {additional_bytes / MIB:.1f} MiB + "
                    f"{free_margin_bytes / MIB:.0f} MiB margin, free {free / MIB:.1f} MiB",
                    report,
                )
            self._reserved += additional_bytes
            return {"additional_bytes": additional_bytes, "category": category, "reserved": self._reserved}

    def release(self, additional_bytes: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - int(additional_bytes))

    def reserve_for_write(self, additional_bytes: int, category: str = "raw") -> Any:
        """Context manager style reservation that is always released."""
        return _Reservation(self, additional_bytes, category)


class _Reservation:
    def __init__(self, budget: StorageBudget, amount: int, category: str):
        self.budget = budget
        self.amount = int(amount)
        self.category = category

    def __enter__(self) -> "_Reservation":
        self.budget.check(self.amount, self.category)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.budget.release(self.amount)


# --------------------------------------------------------------------- universe


def load_universe(settings: QuantSettings) -> dict[str, Any]:
    path = settings.universe_path
    if not path.exists():
        raise StorageError(
            f"quant universe not found: {path}. Run `python tools/build_quant_universe.py` first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"quant universe is unreadable: {exc}") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise StorageError("quant universe has no entries")
    symbols = [str(entry.get("binance_symbol") or "").upper() for entry in entries]
    if any(not symbol.isalnum() for symbol in symbols):
        raise StorageError("quant universe contains an invalid binance_symbol")
    if len(set(symbols)) != len(symbols):
        raise StorageError("quant universe contains duplicate symbols")
    payload["_symbols"] = symbols
    payload["_by_symbol"] = {str(entry["binance_symbol"]).upper(): entry for entry in entries}
    return payload


def universe_symbols(settings: QuantSettings, *, limit: int | None = None) -> list[str]:
    payload = load_universe(settings)
    symbols = list(payload["_symbols"])
    if limit is not None:
        symbols = symbols[: max(0, int(limit))]
    return symbols


def universe_digest(settings: QuantSettings) -> str:
    payload = load_universe(settings)
    canonical = {
        "schema_version": payload.get("schema_version"),
        "months": payload.get("selection", {}).get("months") if isinstance(payload.get("selection"), dict) else None,
        "symbols": payload["_symbols"],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# --------------------------------------------------------------------- manifest


def empty_manifest(settings: QuantSettings) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": utc_now(),
        "source": {
            "name": "Binance Vision",
            "url": "https://data.binance.vision/",
            "dataset": "spot monthly klines, 1m",
            "license": "Binance public market data archive terms (https://data.binance.vision/terms-of-use.html)",
        },
        "months": list(settings.months),
        "symbols": {},
        "errors": [],
        "totals": {"symbols": 0, "months": 0, "rows": 0, "bytes": 0, "missing_minutes": 0, "gaps": 0},
    }


def load_manifest(settings: QuantSettings, path: Path | None = None) -> dict[str, Any]:
    """Read a manifest. ``path`` lets the daily-sourced lake keep its own record."""
    path = path or settings.manifest_path
    if not path.exists():
        return empty_manifest(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"manifest is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), dict):
        raise StorageError("manifest has an unexpected shape")
    payload.setdefault("errors", [])
    payload.setdefault("months", list(settings.months))
    payload.setdefault("source", empty_manifest(settings)["source"])
    return payload


def save_manifest(settings: QuantSettings, manifest: dict[str, Any], path: Path | None = None) -> None:
    symbols = manifest.setdefault("symbols", {})
    totals = {"symbols": 0, "months": 0, "rows": 0, "bytes": 0, "missing_minutes": 0, "gaps": 0}
    for record in symbols.values():
        months = record.get("months") if isinstance(record.get("months"), dict) else {}
        if not months:
            continue
        totals["symbols"] += 1
        totals["months"] += len(months)
        totals["rows"] += int(record.get("rows") or 0)
        totals["bytes"] += int(record.get("bytes") or 0)
        totals["missing_minutes"] += int(record.get("missing_minutes") or 0)
        totals["gaps"] += int(record.get("gaps") or 0)
    manifest["totals"] = totals
    manifest["generated_at"] = utc_now()
    manifest["digest"] = manifest_digest(manifest)
    write_json_atomic(path or settings.manifest_path, manifest)


def manifest_digest(manifest: dict[str, Any]) -> str:
    symbols = manifest.get("symbols") if isinstance(manifest.get("symbols"), dict) else {}
    canonical: dict[str, Any] = {}
    for symbol in sorted(symbols):
        record = symbols[symbol]
        months = record.get("months") if isinstance(record.get("months"), dict) else {}
        canonical[symbol] = {
            month: {
                "rows": month_data.get("rows"),
                "first_ts": month_data.get("first_ts"),
                "last_ts": month_data.get("last_ts"),
                "source_sha256": month_data.get("source_sha256"),
            }
            for month, month_data in sorted(months.items())
        }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def current_data_version(settings: QuantSettings) -> dict[str, Any]:
    manifest = load_manifest(settings)
    return {
        "universe": universe_digest(settings),
        "manifest": manifest.get("digest") or manifest_digest(manifest),
        "months": list(settings.months),
        "source": "binance_vision_1m",
        "symbols": manifest.get("totals", {}).get("symbols", 0),
        "rows": manifest.get("totals", {}).get("rows", 0),
    }


def daily_months(settings: QuantSettings) -> list[str]:
    """Months assembled from daily archives, as recorded in the separate daily manifest."""
    daily = load_manifest(settings, settings.manifest_daily_path)
    return sorted({month for record in daily.get("symbols", {}).values()
                   for month in (record.get("months") or {})})


def daily_data_version(settings: QuantSettings) -> dict[str, Any]:
    """Provenance for a run that also read a daily-sourced month.

    ``current_data_version`` deliberately stays the number every earlier record was
    written against: a daily month is an extra file with its own manifest, so adding
    one cannot silently re-label the windows already in DECISIONS.md.
    """
    version = current_data_version(settings)
    daily = load_manifest(settings, settings.manifest_daily_path)
    extra = daily_months(settings)
    return {
        **version,
        "months": sorted(set(version["months"]) | set(extra)),
        "daily_manifest": daily.get("digest") or manifest_digest(daily),
        "daily_months": extra,
        "daily_rows": int(daily.get("totals", {}).get("rows") or 0),
        "daily_symbols": int(daily.get("totals", {}).get("symbols") or 0),
        "sources": {"monthly": "binance_vision_1m_monthly", "daily": "binance_vision_1m_daily"},
    }


def symbol_months(manifest: dict[str, Any], symbol: str) -> dict[str, Any]:
    record = manifest.get("symbols", {}).get(symbol.upper())
    if not isinstance(record, dict):
        return {}
    months = record.get("months")
    return months if isinstance(months, dict) else {}


# ------------------------------------------------------------------- files


def month_path(settings: QuantSettings, symbol: str, month: str) -> Path:
    return settings.raw_dir / symbol.upper() / f"{symbol.upper()}-1m-{month}.parquet"


def day_path(settings: QuantSettings, symbol: str, date: str) -> Path:
    """Daily-sourced bars live beside the monthly files, never inside them.

    Keeping them in their own subdirectory is what lets the monthly lake and its
    manifest digest stay exactly as every prior record described them.
    """
    return settings.raw_dir / symbol.upper() / "daily" / f"{symbol.upper()}-1m-{date}.parquet"


def read_parquet_frame(path: Path, columns: Iterable[str] | None = None) -> pd.DataFrame:
    """Read one stored 1m frame and refuse a silently corrupt or partial file."""
    if not path.exists():
        raise StorageError(f"missing parquet file: {path}")
    frame = pd.read_parquet(path, engine="pyarrow", columns=list(columns) if columns else None)
    expected = [column for column in (columns or BAR_COLUMNS) if column in frame.columns]
    missing = [column for column in (columns or BAR_COLUMNS) if column not in frame.columns]
    if missing:
        raise StorageError(f"{path.name} is missing columns: {missing}")
    frame = frame[expected]
    for column in expected:
        if frame[column].dtype == object:
            raise StorageError(f"{path.name} has a non-numeric column {column}")
    return frame


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def write_parquet_atomic(frame: pd.DataFrame, path: Path, budget: StorageBudget, *, category: str = "raw",
                         already_reserved: bool = False) -> int:
    """Write one parquet file only if the budget allows it; returns bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        frame.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
        size = tmp.stat().st_size
        if already_reserved:
            os.replace(tmp, path)
        else:
            with budget.reserve_for_write(size, category):
                os.replace(tmp, path)
        return size
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_month(settings: QuantSettings, symbol: str, month: str, columns: Iterable[str] | None = None) -> pd.DataFrame:
    return read_parquet_frame(month_path(settings, symbol, month), columns)


def read_day(settings: QuantSettings, symbol: str, date: str, columns: Iterable[str] | None = None) -> pd.DataFrame:
    return read_parquet_frame(day_path(settings, symbol, date), columns)


def read_symbol(settings: QuantSettings, symbol: str, months: Iterable[str] | None = None,
                columns: Iterable[str] | None = None) -> pd.DataFrame:
    months = list(months or settings.months)
    frames = [read_month(settings, symbol, month, columns=columns) for month in months
              if month_path(settings, symbol, month).exists()]
    if not frames:
        raise StorageError(f"no parquet months stored for {symbol}")
    frame = pd.concat(frames, ignore_index=True).sort_values("ts", kind="stable")
    frame = frame.drop_duplicates(subset="ts", keep="last").reset_index(drop=True)
    return frame


def daily_closes(settings: QuantSettings, symbol: str, months: Iterable[str] | None = None) -> pd.DataFrame:
    frame = read_symbol(settings, symbol, months=months, columns=["ts", "close"])
    if frame.empty:
        return pd.DataFrame(columns=["day", "close"])
    days = (frame["ts"] // 86400) * 86400
    grouped = frame.groupby(days, sort=True)["close"].last()
    return pd.DataFrame({"day": grouped.index.astype("int64"), "close": grouped.to_numpy(dtype="float64")})


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_coverage(frame: pd.DataFrame, month: str) -> dict[str, Any]:
    """Measure rows, duplicates and missing minutes for one month of 1m bars.

    Gaps are measured, never filled. A bar whose timestamp is not on a 60-second
    boundary is rejected before this function is called.
    """
    if frame.empty:
        return {"rows": 0, "missing_minutes": 0, "gaps": 0, "gap_starts": [], "coverage_pct": 0.0,
                "first_ts": None, "last_ts": None, "first_iso": None, "last_iso": None}
    timestamps = frame["ts"].to_numpy(dtype="int64")
    unique = pd.Series(timestamps).drop_duplicates().sort_values().to_numpy(dtype="int64")
    step = 60
    diffs = unique[1:] - unique[:-1]
    gap_positions = (diffs > step).sum()
    missing = int(max(0, ((unique[-1] - unique[0]) // step + 1) - len(unique)))
    expected = (unique[-1] - unique[0]) // step + 1
    gap_starts = [int(unique[i] + step) for i in range(len(diffs)) if diffs[i] > step][:20]
    first_ts, last_ts = int(unique[0]), int(unique[-1])
    return {
        "rows": int(len(unique)),
        "missing_minutes": missing,
        "gaps": int(gap_positions),
        "gap_starts": gap_starts,
        "coverage_pct": round(len(unique) / expected * 100.0, 4) if expected else 0.0,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "first_iso": datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat(timespec="seconds"),
        "last_iso": datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat(timespec="seconds"),
        "month": month,
    }


def summarize_symbol(record: dict[str, Any]) -> dict[str, Any]:
    months = record.get("months") if isinstance(record.get("months"), dict) else {}
    rows = sum(int(month.get("rows") or 0) for month in months.values())
    missing = sum(int(month.get("missing_minutes") or 0) for month in months.values())
    gaps = sum(int(month.get("gaps") or 0) for month in months.values())
    expected = sum(int(month.get("expected_minutes") or month.get("rows") or 0) for month in months.values())
    first = min((month.get("first_ts") for month in months.values() if month.get("first_ts")), default=None)
    last = max((month.get("last_ts") for month in months.values() if month.get("last_ts")), default=None)
    return {
        **record,
        "rows": rows,
        "bytes": sum(int(month.get("bytes") or 0) for month in months.values()),
        "missing_minutes": missing,
        "gaps": gaps,
        "coverage_pct": round(rows / expected * 100.0, 4) if expected else None,
        "first_ts": first,
        "last_ts": last,
        "first_iso": datetime.fromtimestamp(first, tz=timezone.utc).isoformat(timespec="seconds") if first else None,
        "last_iso": datetime.fromtimestamp(last, tz=timezone.utc).isoformat(timespec="seconds") if last else None,
    }


def coverage_report(settings: QuantSettings, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest(settings)
    rows = [summarize_symbol(record) for record in manifest.get("symbols", {}).values()]
    complete = [row for row in rows if row.get("months") and len(row["months"]) == len(settings.months)]
    return {
        "symbols_stored": len(rows),
        "symbols_complete": len(complete),
        "months_expected": list(settings.months),
        "rows": sum(row["rows"] for row in rows),
        "missing_minutes": sum(row["missing_minutes"] for row in rows),
        "gaps": sum(row["gaps"] for row in rows),
        "bytes": sum(row["bytes"] for row in rows),
        "coverage_pct": round(sum(row["rows"] for row in rows) /
                              max(1, sum(int(m.get("expected_minutes") or m.get("rows") or 0)
                                         for row in rows
                                         for m in (row.get("months") or {}).values())) * 100.0, 4),
        "symbols": sorted(rows, key=lambda row: row.get("symbol") or ""),
        "errors": manifest.get("errors", []),
    }


def cleanup_tmp(settings: QuantSettings) -> int:
    """Delete only our own orphaned ``*.tmp-*`` files; returns bytes reclaimed."""
    reclaimed = 0
    for root, _dirs, files in os.walk(settings.data_dir):
        for name in files:
            if ".tmp-" in name:
                path = Path(root) / name
                try:
                    size = path.stat().st_size
                    path.unlink()
                    reclaimed += size
                except OSError:
                    continue
    return reclaimed
