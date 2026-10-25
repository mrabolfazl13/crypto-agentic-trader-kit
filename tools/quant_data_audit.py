"""Independent audit of the quant 1m parquet lake.

Re-derives every number in ``manifest.json`` from the stored files instead of
trusting it: schema, timestamps, duplicates, OHLC coherence, coverage, month
boundaries and cross-month continuity. Writes JSON+markdown and exits non-zero
when any check fails, so it can gate a release.

Usage:
    python tools/quant_data_audit.py [--data-dir state/quant] [--out state/quant/results/data_audit.json]
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")


def month_bounds(month: str) -> tuple[int, int, int]:
    year, number = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, number)[1]
    start = datetime(year, number, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if number == 12 else \
        datetime(year, number + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp()), days * 1440


@dataclass
class MonthAudit:
    symbol: str
    month: str
    rows: int = 0
    expected_rows: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    bytes: int = 0
    gaps: int = 0
    missing_minutes: int = 0
    duplicates: int = 0
    off_grid: int = 0
    non_finite: int = 0
    non_positive: int = 0
    ohlc_incoherent: int = 0
    negative_volume: int = 0
    unsorted: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "month": self.month, "rows": self.rows,
            "expected_rows": self.expected_rows, "first_ts": self.first_ts,
            "last_ts": self.last_ts, "first_iso": iso(self.first_ts) if self.first_ts else None,
            "last_iso": iso(self.last_ts) if self.last_ts else None, "bytes": self.bytes,
            "gaps": self.gaps, "missing_minutes": self.missing_minutes,
            "duplicates": self.duplicates, "off_grid": self.off_grid,
            "non_finite": self.non_finite, "non_positive": self.non_positive,
            "ohlc_incoherent": self.ohlc_incoherent, "negative_volume": self.negative_volume,
            "unsorted": self.unsorted, "problems": self.problems,
        }


def audit_month(path: Path, symbol: str, month: str) -> MonthAudit:
    out = MonthAudit(symbol=symbol, month=month)
    out.bytes = path.stat().st_size
    start_ts, end_ts, expected = month_bounds(month)
    out.expected_rows = expected

    table = pq.read_table(path, columns=COLUMNS)
    names = list(table.column_names)
    if names != COLUMNS:
        out.problems.append(f"schema {names} != {COLUMNS}")
        return out
    frame = table.to_pandas()
    out.rows = int(len(frame))
    if out.rows == 0:
        out.problems.append("empty file")
        return out

    ts = frame["ts"].to_numpy(dtype="int64")
    open_ = frame["open"].to_numpy(dtype="float64")
    high = frame["high"].to_numpy(dtype="float64")
    low = frame["low"].to_numpy(dtype="float64")
    close = frame["close"].to_numpy(dtype="float64")
    volume = frame["volume"].to_numpy(dtype="float64")
    out.first_ts, out.last_ts = int(ts[0]), int(ts[-1])

    out.off_grid = int(np.count_nonzero(ts % 60 != 0))
    out.duplicates = int(len(ts) - len(np.unique(ts)))
    out.unsorted = int(np.count_nonzero(np.diff(ts) <= 0))
    diffs = np.diff(ts)
    out.gaps = int(np.count_nonzero(diffs > 60))
    out.missing_minutes = int(max(0, (out.last_ts - out.first_ts) // 60 + 1 - len(np.unique(ts))))

    prices = np.column_stack([open_, high, low, close])
    out.non_finite = int(np.count_nonzero(~np.isfinite(prices)) + np.count_nonzero(~np.isfinite(volume)))
    with np.errstate(invalid="ignore"):
        out.non_positive = int(np.count_nonzero(np.any(prices <= 0, axis=1)))
        out.negative_volume = int(np.count_nonzero(volume < 0))
        out.ohlc_incoherent = int(np.count_nonzero(
            (high < np.maximum(open_, close)) | (low > np.minimum(open_, close)) | (high < low)
        ))

    if out.first_ts != start_ts:
        out.problems.append(f"first_ts {iso(out.first_ts)} != month start")
    if out.last_ts != end_ts - 60:
        out.problems.append(f"last_ts {iso(out.last_ts)} != month end - 1m")
    if out.rows != expected:
        out.problems.append(f"rows {out.rows} != expected {expected}")
    for name, value in (("duplicates", out.duplicates), ("off_grid", out.off_grid),
                        ("unsorted", out.unsorted), ("non_finite", out.non_finite),
                        ("non_positive", out.non_positive), ("negative_volume", out.negative_volume),
                        ("ohlc_incoherent", out.ohlc_incoherent), ("gaps", out.gaps),
                        ("missing_minutes", out.missing_minutes)):
        if value:
            out.problems.append(f"{name}={value}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="state/quant")
    parser.add_argument("--out", default="state/quant/results/data_audit.json")
    parser.add_argument("--sample", type=int, default=0, help="audit only the first N symbols")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    months = list(manifest.get("months") or [])
    symbols = sorted(manifest.get("symbols") or {})
    if args.sample:
        symbols = symbols[: args.sample]
    if not months or not symbols:
        print("manifest has no months or symbols", file=sys.stderr)
        return 2

    audits: list[MonthAudit] = []
    by_symbol: dict[str, dict] = {}
    manifest_mismatches: list[dict] = []
    for index, symbol in enumerate(symbols, start=1):
        record = manifest["symbols"][symbol]
        symbol_months = record.get("months") or {}
        symbol_audits = []
        for month in months:
            path = data_dir / "raw" / "1m" / symbol / f"{symbol}-1m-{month}.parquet"
            if not path.exists():
                symbol_audits.append(MonthAudit(symbol, month, problems=["missing file"]))
                continue
            audit = audit_month(path, symbol, month)
            audits.append(audit)
            symbol_audits.append(audit)
            expected = symbol_months.get(month) or {}
            for key in ("rows", "first_ts", "last_ts", "bytes"):
                if expected.get(key) is not None and expected.get(key) != getattr(audit, key):
                    manifest_mismatches.append({"symbol": symbol, "month": month, "field": key,
                                                "manifest": expected.get(key), "audited": getattr(audit, key)})
        for left, right in zip(symbol_audits, symbol_audits[1:]):
            if left.last_ts is None or right.first_ts is None:
                continue
            if left.last_ts + 60 != right.first_ts:
                left.problems.append(f"month seam to {right.month} is not contiguous")
                manifest_mismatches.append({"symbol": symbol, "month": left.month, "field": "month_seam",
                                            "manifest": "contiguous", "audited": f"{iso(left.last_ts)}->{iso(right.first_ts)}"})
        by_symbol[symbol] = {
            "months": len([a for a in symbol_audits if a.rows]),
            "rows": sum(a.rows for a in symbol_audits),
            "first_iso": iso(symbol_audits[0].first_ts) if symbol_audits and symbol_audits[0].first_ts else None,
            "last_iso": iso(symbol_audits[-1].last_ts) if symbol_audits and symbol_audits[-1].last_ts else None,
            "problems": [f"{a.month}: {p}" for a in symbol_audits for p in a.problems],
        }
        if index % 20 == 0:
            print(f"  audited {index}/{len(symbols)} symbols", flush=True)

    totals = {
        "symbols": len(symbols),
        "months": len(months),
        "symbol_months": len(audits),
        "rows": sum(a.rows for a in audits),
        "bytes": sum(a.bytes for a in audits),
        "gaps": sum(a.gaps for a in audits),
        "missing_minutes": sum(a.missing_minutes for a in audits),
        "duplicates": sum(a.duplicates for a in audits),
        "invalid_rows": sum(a.non_finite + a.non_positive + a.ohlc_incoherent + a.negative_volume
                            + a.off_grid for a in audits),
        "files_with_problems": sum(1 for a in audits if a.problems),
        "manifest_mismatches": len(manifest_mismatches),
    }
    report = {
        "audited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_dir": str(data_dir.resolve()),
        "months": months,
        "source": (manifest.get("source") or {}),
        "manifest_digest": manifest.get("digest"),
        "manifest_totals": manifest.get("totals"),
        "totals": totals,
        "manifest_mismatches": manifest_mismatches,
        "by_symbol": by_symbol,
        "months_detail": [a.as_dict() for a in audits if a.problems],
        "verdict": "pass" if totals["files_with_problems"] == 0 and not manifest_mismatches else "fail",
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps({k: totals[k] for k in ("symbols", "months", "symbol_months", "rows", "bytes",
                                             "gaps", "missing_minutes", "duplicates", "invalid_rows",
                                             "files_with_problems", "manifest_mismatches")},
                     ensure_ascii=False, indent=1))
    print("verdict:", report["verdict"], "->", out_path)
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
