"""Historical dataset ingestion (CSV + exchange) and one honest quality block.

Stdlib only: no pandas, no new dependencies. Every number in ``quality_report``
is measured from the bars themselves, and anything missing, stale or synthetic
becomes a warning instead of being smoothed over — a dataset with no volume
would otherwise silently bias a backtest.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .data import DataUnavailable, Kline, TIMEFRAMES, normalize_pair, seconds_per_bar, validate_klines

UTC = timezone.utc
MAX_GAP_STARTS = 10
DAY_SECONDS = 86400
MS_EPOCH_CUTOFF = 1e11  # a second-based epoch this large would be year 3170+

# Bad rows are counted, never fatal; these shapes decide what is a number/date.
_NON_NUMERIC = re.compile(r"[^0-9eE+\-.]")
_EPOCH_SHAPE = re.compile(r"^[0-9][0-9 ,]*(\.[0-9]+)?([eE][+-]?[0-9]+)?$")
_DATE_FORMATS = ("%Y/%m/%d", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y%m%d", "%Y%m%d%H%M")

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "open_time": ("timestamp", "time", "date", "datetime", "opentime", "opents", "candlestime"),
    "open": ("open", "o", "openprice", "startprice"),
    "high": ("high", "h", "highprice", "peak"),
    "low": ("low", "l", "lowprice", "trough"),
    "close": ("close", "c", "closeprice", "last", "finalprice"),
    "base_volume": ("volume", "v", "basevolume", "baseassetvolume", "vol", "quantity", "qty"),
    "quote_volume": ("quotevolume", "qv", "quoteassetvolume", "turnover", "value", "usdvolume"),
}
# A headerless export is read as a timestamp-first provider dump.
POSITIONAL = ("open_time", "open", "high", "low", "close", "base_volume", "quote_volume")


class DatasetError(Exception):
    """Raised when a dataset cannot be built honestly from the supplied input."""


# ------------------------------------------------------------------- reading


def _norm_header(cell: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(cell).lower())


def _map_header(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        key = _norm_header(cell)
        for field, aliases in COLUMN_ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = index
                break
    return mapping


def _numeric_cell(cell: Any) -> bool:
    return bool(_EPOCH_SHAPE.match(str(cell).replace(",", "").replace(" ", "")))


def _positional_header(row: list[str]) -> dict[str, int]:
    return {POSITIONAL[i]: i for i in range(min(len(row), len(POSITIONAL)))}


def _to_float(raw: Any, *, field: str, allow_zero: bool = False) -> float:
    """Positive finite number, tolerating thousands separators and currency signs."""
    text = _NON_NUMERIC.sub("", str(raw).strip())
    if text in {"", ".", "-", "+", "--"}:
        raise ValueError(f"{field}: empty numeric value")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{field}: not a number ({raw!r})") from exc
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{field}: not a positive finite number ({raw!r})")
    return value


def _to_epoch(raw: Any) -> tuple[int, bool]:
    """Normalise epoch seconds / milliseconds / ISO-8601 to UTC epoch seconds.

    The boolean reports a naive timestamp: UTC was assumed, and that assumption
    is surfaced as a warning rather than presented as if the file said so.
    """
    text = str(raw).strip().strip('"').strip("'")
    candidate = text.replace(",", "").replace(" ", "").replace("_", "")
    if _EPOCH_SHAPE.match(candidate):
        raw_value = float(candidate)
        value = raw_value / 1000.0 if raw_value >= MS_EPOCH_CUTOFF else raw_value
        if not (1e8 <= value < 4e9):
            raise ValueError(f"timestamp: implausible epoch {raw!r}")
        return int(value), False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(f"timestamp: unparseable {raw!r}")
    if parsed.tzinfo is None:
        return int(parsed.replace(tzinfo=UTC).timestamp()), True
    return int(parsed.astimezone(UTC).timestamp()), False


def _detect_spacing(times: list[int]) -> int | None:
    unique = sorted(set(times))
    diffs = [b - a for a, b in zip(unique, unique[1:]) if b > a]
    return int(median(diffs)) if diffs else None


def _spacing_to_timeframe(spacing: int | None) -> str | None:
    if not spacing or spacing <= 0:
        return None
    for label, (seconds, _, _) in TIMEFRAMES.items():
        if seconds == spacing:
            return label
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if spacing % size == 0:
            return f"{spacing // size}{unit}"
    return f"{spacing}s"


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _step_for(timeframe: str) -> int:
    try:
        return seconds_per_bar(timeframe)
    except DataUnavailable as exc:
        raise DatasetError(str(exc)) from exc


def parse_csv(path: str | Path) -> tuple[list[Kline], dict[str, Any]]:
    """Tolerantly read an OHLCV CSV into Kline objects plus read metadata.

    Unparseable rows land in ``meta['dropped_rows']``; a header that cannot be
    matched at all is a hard ``DatasetError``, because guessing which column is
    the close price would fabricate history.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise DatasetError(f"csv file not found: {file_path}")
    with file_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
        rows = [row for row in csv.reader(fh, delimiter=delimiter) if any(str(c).strip() for c in row)]
    if not rows:
        raise DatasetError(f"csv is empty: {file_path}")

    header, columns, warnings = rows[0], _map_header(rows[0]), []
    if not columns and len(header) >= 5 and all(_numeric_cell(c) for c in header[:5]):
        columns, data_rows = _positional_header(header), rows
        warnings.append("headerless_positional_columns")
    else:
        data_rows = rows[1:]
    missing = {"open_time", "close"} - set(columns)
    if missing:
        raise DatasetError(
            f"csv header for {file_path.name} is not recognisable; missing {sorted(missing)}. Expected columns "
            f"like timestamp, open, high, low, close, volume[, quote_volume]. Got {header!r}"
        )

    for field, token in (("high", "synthetic_hl"), ("low", "synthetic_hl"), ("open", "synthetic_open"),
                         ("base_volume", "missing_volume"), ("quote_volume", "missing_quote_volume")):
        if field not in columns and token not in warnings:
            warnings.append(token)
    ts_index, close_index = columns["open_time"], columns["close"]
    widest = max(columns.values())
    now_ts = int(datetime.now(UTC).timestamp())

    bars: list[Kline] = []
    dropped, naive_seen = 0, False
    drop_reasons: dict[str, int] = {}
    for row in data_rows:
        try:
            if len(row) <= widest:
                raise ValueError("short row")
            open_time, assumed_utc = _to_epoch(row[ts_index])
            close = _to_float(row[close_index], field="close")
            open_ = _to_float(row[columns["open"]], field="open") if "open" in columns else close
            high = _to_float(row[columns["high"]], field="high") if "high" in columns else max(open_, close)
            low = _to_float(row[columns["low"]], field="low") if "low" in columns else min(open_, close)
            volume = _to_float(row[columns["base_volume"]], field="volume", allow_zero=True) \
                if "base_volume" in columns else 0.0
            quote = _to_float(row[columns["quote_volume"]], field="quote_volume", allow_zero=True) \
                if "quote_volume" in columns else volume * close
            naive_seen = naive_seen or assumed_utc
        except (ValueError, TypeError, KeyError) as exc:
            dropped += 1
            reason = str(exc)[:60]
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
            continue
        bars.append(Kline(open_time=open_time, open=open_, high=high, low=low, close=close,
                          base_volume=volume, quote_volume=quote, closed=True))

    bars.sort(key=lambda b: b.open_time)
    spacing = _detect_spacing([b.open_time for b in bars])
    if spacing:  # a bar whose own window has not finished yet is not history
        bars = [replace(b, closed=b.open_time + spacing <= now_ts) for b in bars]
    timeframe = _spacing_to_timeframe(spacing)
    if naive_seen:
        warnings.append("naive_timestamps_assumed_utc")
    if not timeframe:
        warnings.append("timeframe_undetected")

    meta: dict[str, Any] = {
        "path": str(file_path.resolve()), "file": file_path.name, "delimiter": delimiter, "header": header,
        "columns": {field: header[i] for field, i in columns.items()}, "column_index": dict(columns),
        "timeframe": timeframe, "spacing_seconds": spacing, "rows_read": len(data_rows),
        "parsed_rows": len(bars), "dropped_rows": dropped, "drop_reasons": drop_reasons,
        "missing_volume": "base_volume" not in columns, "warnings": warnings,
    }
    return bars, meta


# ------------------------------------------------------------------- quality


def quality_report(
    bars: list[Kline],
    *,
    timeframe: str,
    source: str,
    origin: str,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """The single quality block the UI shows before a dataset is trusted.

    ``validate_klines`` already rules on freshness, duplicates and OHLC sanity
    for live snapshots, so its verdicts are reused here rather than re-derived.
    """
    now_ts = int(now_ts if now_ts is not None else datetime.now(UTC).timestamp())
    base = {"source": source, "origin": origin, "timeframe": timeframe, "warnings": [], "incomplete": True}
    if not bars:
        return {**base, "bars": 0, "closed_bars": 0, "dropped_unclosed": 0, "duplicates": 0,
                "gaps": {"count": 0, "missing_bars": 0, "starts": []}, "impossible_ohlc": 0,
                "non_positive_prices": 0, "coverage_pct": 0.0, "stale_seconds": None, "is_stale": True,
                "median_spacing": None, "first_ts": None, "last_ts": None, "first_iso": None,
                "last_iso": None, "expected_bars": 0, "warnings": ["no_data"]}

    times = [b.open_time for b in bars]
    spacing = _detect_spacing(times)
    supported = timeframe in TIMEFRAMES
    step = TIMEFRAMES[timeframe][0] if supported else (spacing or 0)
    closed = [b for b in bars if b.closed]
    unique_closed = sorted({b.open_time for b in closed})

    gap_starts: list[int] = []
    missing_bars = 0
    for previous, current in zip(unique_closed, unique_closed[1:]):
        if step > 0 and current - previous > step:
            missing_bars += max(int(round((current - previous) / step)) - 1, 1)
            gap_starts.append(previous + step)

    impossible = sum(1 for b in bars
                     if b.high < b.low or b.high < max(b.open, b.close) or b.low > min(b.open, b.close))
    non_positive = sum(1 for b in bars if min(b.open, b.high, b.low, b.close) <= 0)
    stale_seconds = now_ts - (unique_closed[-1] + step) if (closed and step > 0) else None

    warnings: list[str] = [] if supported else ["unsupported_timeframe"]
    is_stale = stale_seconds is not None and stale_seconds > 2.5 * max(step, 1)
    if closed and supported:
        # validate_klines reads the newest bar positionally, so hand it sorted bars;
        # unsorted input would otherwise look stale.
        _, gate_warnings = validate_klines(sorted(closed, key=lambda b: b.open_time), timeframe,
                                           float(now_ts), step * 2.0)
        warnings.extend(gate_warnings)
        is_stale = "stale_klines" in gate_warnings
    elif not closed:
        warnings.append("no_closed_bars")
    if closed and all(b.base_volume == 0 for b in closed):
        warnings.append("missing_volume")
    if impossible:
        warnings.append("impossible_ohlc")
    if non_positive:
        warnings.append("non_positive_prices")

    expected = coverage = 0
    if step > 0 and len(unique_closed) >= 2:
        expected = (unique_closed[-1] - unique_closed[0]) // step + 1
        coverage = round(min(len(unique_closed) / expected * 100.0, 100.0), 2)
    elif len(unique_closed) == 1:
        coverage = 100.0
    return {
        **base,
        "bars": len(bars), "closed_bars": len(closed), "dropped_unclosed": len(bars) - len(closed),
        "duplicates": len(times) - len(set(times)), "impossible_ohlc": impossible,
        "non_positive_prices": non_positive, "coverage_pct": coverage, "expected_bars": expected,
        "gaps": {"count": len(gap_starts), "missing_bars": missing_bars, "starts": gap_starts[:MAX_GAP_STARTS]},
        "stale_seconds": round(stale_seconds, 3) if stale_seconds is not None else None, "is_stale": is_stale,
        "median_spacing": spacing, "first_ts": min(times), "last_ts": max(times),
        "first_iso": _iso(min(times)), "last_iso": _iso(max(times)),
        # "incomplete" means holes in the window, not age: a two-year-old CSV is
        # complete history, a truncated exchange pull is not.
        "incomplete": bool(not closed) or (0 < coverage < 100.0),
        "warnings": list(dict.fromkeys(warnings)),
    }


# -------------------------------------------------------------------- ingest


def _dataset_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"ds-{prefix}-{digest[:10]}"


def _persist(store: Any, *, dataset_id: str, symbol: str, timeframe: str, origin: str, source: str,
             bars: list[Kline], quality: dict[str, Any], path: str | None, note: str) -> dict[str, Any]:
    if quality["closed_bars"] == 0:
        raise DatasetError(f"refusing to store {dataset_id}: no closed bars survive validation "
                           f"({quality['warnings']})")
    store.save_dataset(dataset_id=dataset_id, symbol=symbol, timeframe=timeframe, origin=origin, source=source,
                       bars=bars, quality=quality, path=path, note=note)
    store.log("info", "datasets", f"dataset {dataset_id} stored",
              {"dataset_id": dataset_id, "symbol": symbol, "timeframe": timeframe, "origin": origin,
               "source": source, "closed_bars": quality["closed_bars"], "warnings": quality["warnings"]})
    return {"dataset_id": dataset_id, "bars": quality["closed_bars"], "timeframe": timeframe, "quality": quality}


def ingest_csv(store: Any, path: str | Path, *, symbol: str, timeframe: str | None = None,
               note: str = "") -> dict[str, Any]:
    """Import a CSV as a dataset; re-importing the same file is idempotent."""
    file_path = Path(path)
    bars, meta = parse_csv(file_path)
    tf = timeframe or meta["timeframe"]
    if not tf:
        raise DatasetError(f"cannot infer the timeframe of {file_path.name}; pass timeframe explicitly")
    try:
        pair = normalize_pair(symbol)
    except DataUnavailable as exc:
        raise DatasetError(f"invalid symbol for csv import: {exc}") from exc
    quality = quality_report(bars, timeframe=tf, source=file_path.name, origin="csv")
    quality["dropped_rows"] = meta["dropped_rows"]
    quality["parse_warnings"] = meta["warnings"]
    if meta["dropped_rows"]:
        quality["warnings"] = list(dict.fromkeys([*quality["warnings"], "dropped_rows"]))
    return _persist(store, dataset_id=_dataset_id("csv", str(file_path.resolve()), pair, tf), symbol=pair,
                    timeframe=tf, origin="csv", source=file_path.name, bars=bars, quality=quality,
                    path=str(file_path.resolve()), note=note)


def ingest_exchange(store: Any, gate: Any, symbol: str, timeframe: str, days: int) -> dict[str, Any]:
    """Pull a history window from a provider and store it together with its quality.

    Network / ``DataUnavailable`` failures propagate untouched so nothing partial
    is ever written; a short answer is stored but flagged ``incomplete`` so a
    backtest cannot mistake it for the window that was asked for.
    """
    if days <= 0:
        raise DatasetError(f"days must be a positive number of days, got {days}")
    tf_seconds, pair = _step_for(timeframe), normalize_pair(symbol)
    end_ts = int(datetime.now(UTC).timestamp())
    start_ts = end_ts - days * DAY_SECONDS
    bars = gate.klines_range(pair, timeframe, start_ts, end_ts)  # errors propagate on purpose
    quality = quality_report(bars, timeframe=timeframe, source=str(getattr(gate, "name", "exchange")),
                             origin="exchange")
    expected = max((end_ts - start_ts) // tf_seconds, 1)
    ratio = quality["closed_bars"] / expected
    quality.update({"requested_bars": expected, "requested_days": days,
                    "bar_ratio_pct": round(ratio * 100.0, 2)})
    if ratio < 0.6:
        quality["incomplete"] = True
        quality["warnings"] = list(dict.fromkeys([*quality["warnings"], "incomplete_history"]))
    return _persist(store, dataset_id=_dataset_id("ex", pair, timeframe, days, start_ts, end_ts), symbol=pair,
                    timeframe=timeframe, origin="exchange", source=str(getattr(gate, "name", "exchange")),
                    bars=bars, quality=quality, path=None, note=f"{days}d window")


def load_klines(store: Any, dataset_id: str) -> list[Kline]:
    """Rebuild Kline objects from stored bars; a missing id is an error, not zero bars."""
    rows = store.load_bars(dataset_id)
    if not rows:
        raise DatasetError(f"dataset {dataset_id!r} has no stored bars")
    return [Kline(open_time=int(row["open_time"]), open=float(row["open"]), high=float(row["high"]),
                  low=float(row["low"]), close=float(row["close"]), base_volume=float(row["base_volume"]),
                  quote_volume=float(row["quote_volume"]), closed=bool(row["closed"])) for row in rows]


# ----------------------------------------------------------------- transform


def _fold(bucket: int, group: list[Kline]) -> Kline:
    deduped: dict[int, Kline] = {bar.open_time: bar for bar in group}
    bars = sorted(deduped.values(), key=lambda b: b.open_time)
    return Kline(open_time=bucket, open=bars[0].open, close=bars[-1].close,
                 high=max(b.high for b in bars), low=min(b.low for b in bars),
                 base_volume=sum(b.base_volume for b in bars), quote_volume=sum(b.quote_volume for b in bars),
                 closed=all(b.closed for b in bars))


def resample(bars: list[Kline], timeframe: str) -> list[Kline]:
    """Aggregate bars into a coarser timeframe on calendar-aligned buckets."""
    step = _step_for(timeframe)
    if not bars:
        return []
    spacing = _detect_spacing([b.open_time for b in bars])
    if spacing and step < spacing:
        raise DatasetError(f"resample needs a coarser timeframe than the source ({spacing}s bars -> {step}s)")
    buckets: dict[int, list[Kline]] = {}
    for bar in sorted(bars, key=lambda b: b.open_time):
        buckets.setdefault((bar.open_time // step) * step, []).append(bar)
    return [_fold(bucket, buckets[bucket]) for bucket in sorted(buckets)]


def compare_sources(a: list[Kline], b: list[Kline], *, tolerance_pct: float = 0.5) -> dict[str, Any]:
    """Cross-check two providers on identical bar timestamps.

    ``tolerance_pct`` defaults to the same 0.5% disagreement bound the live
    snapshot gate uses, so offline data is judged as strictly as a quote.
    """
    index_a = {bar.open_time: bar for bar in a}
    index_b = {bar.open_time: bar for bar in b}
    shared = sorted(set(index_a) & set(index_b))
    diffs: list[float] = []
    mismatched, worst_ts, worst_pct = 0, None, 0.0
    for ts in shared:
        ca, cb = index_a[ts].close, index_b[ts].close
        if cb <= 0:
            continue
        pct = abs(ca - cb) / cb * 100.0
        diffs.append(pct)
        mismatched += pct > tolerance_pct
        if pct > worst_pct:
            worst_pct, worst_ts = pct, ts
    return {
        "common_bars": len(shared), "only_in_a": len(set(index_a) - set(index_b)),
        "only_in_b": len(set(index_b) - set(index_a)), "mismatched_bars": mismatched,
        "max_abs_diff_pct": round(max(diffs), 6) if diffs else 0.0,
        "mean_abs_diff_pct": round(sum(diffs) / len(diffs), 6) if diffs else 0.0,
        "worst_open_time": worst_ts, "tolerance_pct": tolerance_pct,
        "sources_agree": bool(shared) and mismatched == 0,
    }
