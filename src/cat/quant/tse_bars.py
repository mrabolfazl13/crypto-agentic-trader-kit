"""Read the D-040 Bourse daily-bar lake into the frame shape the shared research chain consumes.

Why this is a separate layer instead of a second call into ``cat.quant.storage``:
``load_bars`` refuses anything that is not on a 60-second grid and ``generate_trades`` fills at the
open of the 1m bar that follows a signal, but TSETMC publishes no sub-daily bars at all
(``src/cat/markets/tse.py`` notes: «داده فقط روزانه (1d)»). Feeding daily rows through the 1m
contract would mean inventing minute bars the venue never published, so the fill rule lives here
instead and portfolio accounting stays in ``cat.quant.sim``.

The fill rule is the whole anti-look-ahead story for this venue: a session row is labelled at its
close (12:30 Tehran), and any decision taken on it can only be executed at the *next* session's
open. ``exec_ts``/``exec_open`` carry that pair, and the last row of a symbol has neither — a
decision with no venue to trade on is dropped, never filled at the price that produced it.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date as calendar_date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..markets import calendar

BARS_DIRNAME = Path("state/tse/bars")
MANIFEST_PATH = Path("state/tse/tse_daily_manifest.json")

#: Session clock, session weekdays and the holiday-agnostic gap rule all come from
#: ``cat.markets.calendar`` — the venue's own authority. Restating «شنبه تا چهارشنبه ۰۹:۰۰–۱۲:۳۰»
#: as numbers here would let the two drift and would silently move every timestamp on this venue.
ROW_FIELDS = ("date", "session", "open", "high", "low", "close", "volume_shares", "insCode")
#: A close-to-close move beyond this is treated as a corporate action (unadjusted capital
#: increase) rather than a price, and is counted in the census published next to every number.
BIG_MOVE_PCT = 30.0


class TseBarError(ValueError):
    """A daily bar file that cannot be trusted; the caller records it, never trades it."""


def _bounds(day: str) -> tuple[datetime, datetime]:
    bounds = calendar.session_bounds(calendar_date.fromisoformat(day))
    if bounds is None:
        raise TseBarError(f"{day}: the board has no session on this weekday (Sat-Wed only)")
    return bounds


def session_close_ts(day: str) -> int:
    """Epoch seconds of the session close — the instant the bar becomes actionable."""
    return int(_bounds(day)[1].timestamp())


def session_open_ts(day: str) -> int:
    """Epoch seconds of the session open — the earliest fill a prior signal can get."""
    return int(_bounds(day)[0].timestamp())


def read_bar_file(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise TseBarError(f"{path.name}: line {line_number} is not JSON ({exc})") from exc
    return rows


def _check_row(row: dict[str, Any], *, ins_code: str) -> None:
    missing = [field for field in ROW_FIELDS if field not in row]
    if missing:
        raise TseBarError(f"{ins_code}: row missing {missing}")
    day = str(row["date"])
    if not day or str(row["session"]) != day.replace("-", ""):
        raise TseBarError(f"{ins_code}: session {row['session']!r} does not match date {day!r}")
    prices = [row["open"], row["high"], row["low"], row["close"]]
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in prices):
        raise TseBarError(f"{ins_code}: {day} has a non-finite price")
    if any(float(value) <= 0.0 for value in prices):
        raise TseBarError(f"{ins_code}: {day} has a non-positive price")
    high, low = float(row["high"]), float(row["low"])
    if high < max(float(row["open"]), float(row["close"])) - 1e-9:
        raise TseBarError(f"{ins_code}: {day} high below open/close")
    if low > min(float(row["open"]), float(row["close"])) + 1e-9:
        raise TseBarError(f"{ins_code}: {day} low above open/close")
    if high < low:
        raise TseBarError(f"{ins_code}: {day} high below low")
    volume = row.get("volume_shares")
    if volume is not None and float(volume) < 0.0:
        raise TseBarError(f"{ins_code}: {day} has negative volume")


def validate_bars(rows: list[dict[str, Any]], *, ins_code: str) -> tuple[list[dict[str, Any]], int]:
    """Sort chronologically and refuse the file on the first untrustworthy row.

    Returns the rows and how many were out of order in the file — a reordering is safe for a daily
    series, but it must be visible rather than silently absorbed.
    """
    if not rows:
        raise TseBarError(f"{ins_code}: file holds no bars")
    for row in rows:
        _check_row(row, ins_code=ins_code)
    days = [str(row["date"]) for row in rows]
    duplicates = sorted({day for day in days if days.count(day) > 1})
    if duplicates:
        raise TseBarError(f"{ins_code}: duplicate sessions {duplicates[:5]}")
    reordered = sum(1 for previous, following in zip(days, days[1:]) if previous > following)
    return sorted(rows, key=lambda row: str(row["date"])), reordered


def bars_digest(bars_by_code: dict[str, list[dict[str, Any]]]) -> str:
    """The D-040 manifest's own digest: sha256 over each symbol's session list, codes sorted.

    Kept as the single implementation — ``tools/tse_ingest_daily.py`` imports it, so a reader can
    never compute a different hash than the writer recorded and call a mismatch a match.
    """
    blob = json.dumps(
        {code: [bar["session"] for bar in bars] for code, bars in sorted(bars_by_code.items())},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def absent_weekday_sessions(rows: list[dict[str, Any]]) -> int:
    """Board-open weekdays between the first and last row that carry no bar.

    The source publishes no holiday calendar, so this count mixes official closures (Nowruz,
    Ashura) with genuine holes; it is published as a ceiling on "missing data", not as a defect.
    """
    if len(rows) < 2:
        return 0
    first = calendar_date.fromisoformat(str(rows[0]["date"]))
    last = calendar_date.fromisoformat(str(rows[-1]["date"]))
    present = {str(row["date"]) for row in rows}
    day_step = calendar.timedelta(days=1)
    cursor, absent = first, 0
    while cursor <= last:
        if calendar.is_session_day(cursor) and cursor.isoformat() not in present:
            absent += 1
        cursor += day_step
    return absent


def multi_session_gaps(rows: list[dict[str, Any]]) -> int:
    """Rows whose next bar is more than one board session later.

    D-040 dropped no-trade sessions instead of filling them (``no_trading_session = 36214``), so a
    symbol's own daily series can skip board days: its "one bar" is not the same clock as another
    symbol's, and an indicator computed on it spans a different number of market days. Counting the
    skips is what lets the result say how irregular that clock actually was.
    """
    skips = 0
    for previous, following in zip(rows, rows[1:]):
        if str(following["date"]) != calendar.next_session_day(
                calendar_date.fromisoformat(str(previous["date"]))).isoformat():
            skips += 1
    return skips


def big_move_sessions(rows: list[dict[str, Any]], *, threshold_pct: float = BIG_MOVE_PCT) -> int:
    """Sessions whose close-to-close move exceeds the board's own daily price band.

    TSETMC's daily series is unadjusted, so an Iranian capital increase from retained earnings
    prints as a 30-50% crash. Nothing here can tell a crash from a corporate action, which is why
    the count travels with the result instead of being quietly filtered out of it.
    """
    hits = 0
    for previous, following in zip(rows, rows[1:]):
        base = float(previous["close"])
        if base > 0 and abs(float(following["close"]) / base - 1.0) * 100.0 > threshold_pct:
            hits += 1
    return hits


def signal_frame(rows: list[dict[str, Any]], *, ins_code: str) -> pd.DataFrame:
    """One row per closed session, shaped the way ``cat.quant.features`` expects a signal bar.

    ``bar_end`` is the session close (the decision instant) and ``exec_*`` columns carry the next
    session's open, which is the only price a decision on that bar can honestly trade.
    """
    frame = pd.DataFrame({
        "bar_end": [session_close_ts(str(row["date"])) for row in rows],
        "open": [float(row["open"]) for row in rows],
        "high": [float(row["high"]) for row in rows],
        "low": [float(row["low"]) for row in rows],
        "close": [float(row["close"]) for row in rows],
        "volume": [float(row.get("volume_shares") or 0.0) for row in rows],
        "bars": [1] * len(rows),
        "complete": [True] * len(rows),
        "trade_count": [float(row.get("trade_count") or 0.0) for row in rows],
        "traded_value_rial": [float(row.get("traded_value_rial") or 0.0) for row in rows],
    })
    frame["session_date"] = [str(row["date"]) for row in rows]
    frame["exec_ts"] = [None] * len(frame)
    frame["exec_open"] = math.nan
    for index in range(len(frame) - 1):
        frame.loc[index, "exec_ts"] = session_open_ts(str(rows[index + 1]["date"]))
        frame.loc[index, "exec_open"] = float(rows[index + 1]["open"])
    frame["ts"] = frame["bar_end"]
    frame.attrs["ins_code"] = ins_code
    if not frame["bar_end"].is_unique:
        raise TseBarError(f"{ins_code}: duplicate session close timestamps")
    return frame.reset_index(drop=True)


def load_lake(root: Path, *, max_symbols: int | None = None) -> dict[str, Any]:
    """Read every symbol file, cross-check the manifest digest, and keep the failures visible.

    A lake that no longer matches the manifest is not "the same data with one more symbol": the
    numbers recorded against the old digest would stop being reproducible, so the whole run stops.
    The digest always covers **every** file on disk, because ``max_symbols`` narrows what a run
    trades on, not what the lake is — hashing a subset would make each pilot look like a new lake.
    """
    bars_dir = root / BARS_DIRNAME
    manifest_path = root / MANIFEST_PATH
    if not bars_dir.is_dir():
        raise TseBarError(f"no Bourse bar lake at {bars_dir}; run tools/tse_ingest_daily.py first")
    if not manifest_path.is_file():
        raise TseBarError(f"bar lake without a manifest at {manifest_path}; refusing an unlabelled lake")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = sorted(bars_dir.glob("*.jsonl"))
    bars: dict[str, list[dict[str, Any]]] = {}
    census: dict[str, dict[str, int]] = {}
    errors: dict[str, str] = {}
    reordered_total = 0
    for path in paths:
        ins_code = path.stem
        try:
            rows, reordered = validate_bars(read_bar_file(path), ins_code=ins_code)
        except TseBarError as exc:
            errors[ins_code] = str(exc)
            continue
        except OSError as exc:
            errors[ins_code] = f"unreadable: {exc}"
            continue
        reordered_total += reordered
        bars[ins_code] = rows
        census[ins_code] = {
            "sessions": len(rows),
            "absent_weekday_sessions": absent_weekday_sessions(rows),
            "multi_session_gaps": multi_session_gaps(rows),
            "big_move_sessions": big_move_sessions(rows),
            "first_date": str(rows[0]["date"]),
            "last_date": str(rows[-1]["date"]),
        }
    digest = bars_digest(bars)
    recorded = manifest.get("bars_digest")
    if recorded and digest != recorded:
        raise TseBarError(f"bar lake digest {digest[:16]} does not match the manifest {str(recorded)[:16]}")
    span_start = min((value["first_date"] for value in census.values()), default=None)
    span_end = max((value["last_date"] for value in census.values()), default=None)
    depths = sorted(value["sessions"] for value in census.values())
    ordered = dict(sorted(bars.items()))
    selected = dict(list(ordered.items())[:int(max_symbols)]) if max_symbols is not None else ordered
    return {
        "bars": selected,
        "census": {code: census[code] for code in selected},
        "errors": errors,
        "digest": digest,
        "digest_matches_manifest": bool(recorded) and digest == recorded,
        "symbols_usable": len(selected),
        "symbols_in_lake": len(bars),
        "symbols_failed": len(errors),
        "bars_total": sum(len(rows) for rows in bars.values()),
        "bars_selected": sum(len(rows) for rows in selected.values()),
        "sessions_per_symbol": {
            "min": depths[0] if depths else None,
            "median": depths[len(depths) // 2] if depths else None,
            "max": depths[-1] if depths else None,
        },
        "reordered_rows": reordered_total,
        "span": {"first_date": span_start, "last_date": span_end},
        "venue": manifest.get("venue"),
        "source_contract": manifest.get("source_contract"),
        "safety": manifest.get("safety"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
