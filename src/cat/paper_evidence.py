"""Read-only evidence for gate B (PAPER): continuity, restart recovery, ledger integrity.

Gate B asks two questions that a single "last cycle" timestamp cannot answer:

  * did the PAPER loop keep running at the configured cadence, and
  * after a process restart, did the next scheduled cycle actually happen,

without losing or duplicating orders/fills.  This module only reads recorded
state; it never builds an Engine, never fetches data and never sends an order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

DEFAULT_MIN_DAYS = 28.0
DEFAULT_ON_TIME_FRACTION = 0.9
GAP_TOLERANCE = 1.5  # a cycle is "on time" when it lands within 1.5 intervals


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def _hours(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 3600.0, 3)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def analyze(
    *,
    snapshots: Iterable[Mapping[str, Any]],
    orders: Iterable[Mapping[str, Any]] = (),
    fills: Iterable[Mapping[str, Any]] = (),
    trades: Iterable[Mapping[str, Any]] = (),
    restarts: Iterable[Any] = (),
    interval_hours: float,
    now: Any,
    mode: str = "PAPER",
    min_days: float = DEFAULT_MIN_DAYS,
    on_time_fraction: float = DEFAULT_ON_TIME_FRACTION,
) -> dict[str, Any]:
    """Return a JSON-ready verdict for the recorded PAPER window.

    ``snapshots`` are the portfolio snapshots (one per completed cycle),
    ``restarts`` are timestamps of automation "started" events.  A restart is
    recovered when a snapshot lands within two intervals; the *latest* restart
    is judged against its own deadline, while older misses are reported as
    historical downtime rather than poisoning the gate forever.
    """
    interval_hours = float(interval_hours)
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    if not 0 <= on_time_fraction <= 1:
        raise ValueError("on_time_fraction must be between 0 and 1")
    now_dt = _as_dt(now)
    if now_dt is None:
        raise ValueError("now must be a datetime or ISO timestamp")

    problems: list[str] = []

    snapshot_rows = [dict(row) for row in snapshots]
    timed: list[tuple[datetime, Mapping[str, Any]]] = []
    bad_timestamps = 0
    for row in snapshot_rows:
        parsed = _as_dt(row.get("ts"))
        if parsed is None:
            bad_timestamps += 1
            continue
        timed.append((parsed, row))
    timed.sort(key=lambda item: item[0])
    times = [item[0] for item in timed]

    # ---- ledger integrity -------------------------------------------------
    order_rows = [dict(row) for row in orders]
    fill_rows = [dict(row) for row in fills]
    trade_rows = [dict(row) for row in trades]

    def foreign(rows: list[Mapping[str, Any]]) -> int:
        return sum(1 for row in rows if row.get("mode") not in (None, mode))

    foreign_rows = foreign(snapshot_rows) + foreign(order_rows) + foreign(fill_rows) + foreign(trade_rows)
    if foreign_rows:
        problems.append(f"{foreign_rows} row(s) outside mode={mode}")
    if bad_timestamps:
        problems.append(f"{bad_timestamps} snapshot(s) with an unreadable timestamp")

    duplicate_order_ids = len(order_rows) - len({str(row.get("id")) for row in order_rows})
    if duplicate_order_ids:
        problems.append(f"{duplicate_order_ids} duplicate order id(s)")

    order_clients = {
        str(row.get("client_order_id"))
        for row in order_rows
        if row.get("client_order_id") not in (None, "")
    }
    orphan_fills = [
        row for row in fill_rows
        if row.get("client_order_id") not in (None, "")
        and str(row.get("client_order_id")) not in order_clients
    ]
    if orphan_fills:
        problems.append(f"{len(orphan_fills)} fill(s) without a matching order")

    bad_equity = [row for _, row in timed if (_num(row.get("equity")) is None or (_num(row.get("equity")) or 0) < 0)]
    if bad_equity:
        problems.append(f"{len(bad_equity)} snapshot(s) with missing or negative equity")

    closed_net = [_num(row.get("net")) for row in trade_rows]
    closed_fees = [_num(row.get("fees")) for row in trade_rows]
    net_values = [value for value in closed_net if value is not None]
    fee_values = [value for value in closed_fees if value is not None]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value <= 0]
    gross_loss = abs(sum(losses))
    ledger = {
        "orders": len(order_rows),
        "fills": len(fill_rows),
        "trades": len(trade_rows),
        "net_pnl": round(sum(net_values), 4) if net_values else None,
        "fees": round(sum(fee_values), 4) if fee_values else None,
        "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss > 0 else None,
        "orphan_fills": len(orphan_fills),
    }

    # ---- continuity -------------------------------------------------------
    span_days = round(_hours(times[0], times[-1]) / 24.0, 3) if len(times) >= 2 else 0.0
    gaps: list[dict[str, Any]] = []
    for start, end in zip(times, times[1:]):
        hours = _hours(start, end)
        gaps.append({
            "from": _iso(start),
            "to": _iso(end),
            "hours": hours,
            "downtime": hours > interval_hours * GAP_TOLERANCE,
        })
    downtime = [gap for gap in gaps if gap["downtime"]]
    on_time = (len(gaps) - len(downtime)) / len(gaps) if gaps else None
    expected_cycles = int((span_days * 24.0) // interval_hours) + 1 if times else 0

    # ---- restart recovery -------------------------------------------------
    window_start = times[0] if times else None
    restart_times = sorted(
        parsed for parsed in (_as_dt(value) for value in restarts)
        if parsed is not None and (window_start is None or parsed >= window_start)
    )
    restart_events: list[dict[str, Any]] = []
    for restart_at in restart_times:
        following = next((t for t in times if t > restart_at), None)
        deadline = restart_at + timedelta(hours=2 * interval_hours)
        if following is not None and following <= deadline:
            state = "recovered"
        elif now_dt < deadline:
            state = "pending"
        else:
            state = "missed"
        restart_events.append({
            "restart_at": _iso(restart_at),
            "deadline": _iso(deadline),
            "next_cycle_at": _iso(following),
            "state": state,
            "hours_to_cycle": _hours(restart_at, following) if following else None,
        })
    recovered = sum(1 for event in restart_events if event["state"] == "recovered")
    missed = [event for event in restart_events if event["state"] == "missed"]
    pending = [event for event in restart_events if event["state"] == "pending"]
    latest_restart = restart_events[-1] if restart_events else None
    # An old restart without a cycle is a recorded downtime gap, not a permanent
    # verdict: what matters for gate B is that the *latest* restart recovered.
    recovery_ok = latest_restart is None or latest_restart["state"] != "missed"

    last_cycle_gap_hours = _hours(times[-1], now_dt) if times else None
    live_now = last_cycle_gap_hours is not None and last_cycle_gap_hours <= interval_hours * GAP_TOLERANCE

    # ---- verdict ----------------------------------------------------------
    integrity_ok = not problems
    long_enough = span_days >= float(min_days)
    continuous_enough = on_time is not None and on_time >= on_time_fraction

    if not integrity_ok:
        status = "FAIL"
    elif not recovery_ok:
        status = "FAIL"
    elif long_enough and continuous_enough and live_now:
        status = "PASS"
    else:
        status = "INCOMPLETE"

    reasons: list[str] = []
    if not len(times):
        reasons.append("no PAPER snapshot recorded")
    if not long_enough:
        reasons.append(f"window {span_days} day(s) < {float(min_days):g} required")
    if on_time is None:
        reasons.append("not enough cycles to measure cadence")
    elif not continuous_enough:
        reasons.append(f"on-time fraction {on_time:.3f} < {on_time_fraction:.3f}")
    if not recovery_ok:
        reasons.append(f"latest restart {latest_restart['restart_at']} has no cycle within "
                       f"{2 * interval_hours:g}h")
    if missed and recovery_ok:
        reasons.append(f"{len(missed)} earlier restart(s) missed the {2 * interval_hours:g}h window")
    if pending:
        reasons.append(f"latest restart is still inside its {2 * interval_hours:g}h window")
    if times and not live_now:
        reasons.append(f"no cycle for {last_cycle_gap_hours:g}h; the loop is not producing now")
    reasons.extend(problems)

    days_remaining = round(max(0.0, float(min_days) - span_days), 3)
    milestone_at = _iso(times[0] + timedelta(days=float(min_days))) if times and not long_enough else None

    return {
        "gate": "B",
        "mode": mode,
        "status": status,
        "interval_hours": interval_hours,
        "min_days": float(min_days),
        "on_time_fraction_required": float(on_time_fraction),
        "window": {
            "first_cycle_at": _iso(times[0]) if times else None,
            "last_cycle_at": _iso(times[-1]) if times else None,
            "span_days": span_days,
            "cycles_observed": len(times),
            "cycles_expected": expected_cycles,
            "on_time_fraction": round(on_time, 3) if on_time is not None else None,
            "downtime_gaps": len(downtime),
            "max_gap_hours": max((gap["hours"] for gap in gaps), default=None),
            "days_remaining": days_remaining,
            "milestone_at": milestone_at,
            "last_cycle_age_hours": last_cycle_gap_hours,
            "gaps": sorted(gaps, key=lambda gap: gap["hours"], reverse=True)[:5],
        },
        "restarts": {
            "in_window": len(restart_times),
            "recovered": recovered,
            "historical_missed": len(missed),
            "pending": len(pending),
            "latest": latest_restart,
            "missed": missed[:5],
        },
        "ledger": ledger,
        "checks": {
            "integrity_ok": integrity_ok,
            "recovery_ok": recovery_ok,
            "long_enough": long_enough,
            "continuous_enough": continuous_enough,
            "live_now": live_now,
        },
        "reasons": reasons,
    }
