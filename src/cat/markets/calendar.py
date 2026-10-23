"""Iran/Tehran trading-session calendar for the TSE market module.

Two facts drive everything here and both come from recorded exchange evidence,
not from assumptions about a 24/7 crypto feed:

* sessions run Saturday..Wednesday (recorded daily list: 2026-10-17, 18, 19, 20,
  21 then 2026-10-14 — Thursday 22 and Friday 23 are absent);
* the continuous session ends 12:30 Tehran (recorded last-trade times 12:29:59
  and 12:30:02 in the same payloads).

Why this matters: freshness gates written for a 24/7 feed would mark every TSE
snapshot ``stale`` at 13:00 and again every night, which would silently disable
the whole system. For a session market the freshness clock pauses at session
close — ``staleness_reference`` is the newest instant at which new data *could*
have arrived.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")

# Python weekday(): Mon=0 .. Sun=6. TSE trades Sat(5), Sun(6), Mon(0), Tue(1), Wed(2).
SESSION_WEEKDAYS = (5, 6, 0, 1, 2)
SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(12, 30)

CLOSED = "closed"
OPEN = "open"
PRE_OPEN = "pre_open"


def to_tehran(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TEHRAN)


def is_session_day(day: date) -> bool:
    return day.weekday() in SESSION_WEEKDAYS


def _at(day: date, when: time) -> datetime:
    return datetime.combine(day, when, tzinfo=TEHRAN)


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    if not is_session_day(day):
        return None
    return _at(day, SESSION_OPEN), _at(day, SESSION_CLOSE)


def previous_session_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(14):  # longest realistic gap is the Thu/Fri weekend
        if is_session_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise ValueError(f"no TSE session day found before {day}")


def next_session_day(day: date) -> date:
    """The next exchange session after ``day`` (skips Thursday/Friday)."""
    cursor = day + timedelta(days=1)
    for _ in range(14):
        if is_session_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise ValueError(f"no TSE session day found after {day}")


def last_session_close(moment: datetime) -> datetime:
    """The most recent session close at or before ``moment`` (always in the past)."""
    tehran = to_tehran(moment)
    bounds = session_bounds(tehran.date())
    if bounds is not None and tehran >= bounds[1]:
        return bounds[1]
    day = tehran.date() if bounds is None else tehran.date() - timedelta(days=1)
    while True:
        candidate = session_bounds(day)
        if candidate is not None:
            return candidate[1]
        day -= timedelta(days=1)

def next_session_open(moment: datetime) -> datetime:
    tehran = to_tehran(moment)
    bounds = session_bounds(tehran.date())
    if bounds is not None and tehran < bounds[0]:
        return bounds[0]
    day = tehran.date() + timedelta(days=1)
    while True:
        candidate = session_bounds(day)
        if candidate is not None:
            return candidate[0]
        day += timedelta(days=1)


def staleness_reference(now_epoch: float) -> float:
    """Newest instant new TSE data could have arrived — the freshness clock anchor."""
    return min(now_epoch, last_session_close(datetime.fromtimestamp(now_epoch, tz=timezone.utc)).timestamp())


def bar_gap_ok(previous_epoch: float, next_epoch: float) -> bool:
    """True when two bars are adjacent *sessions*, not adjacent calendar days.

    A 24/7 validator treats the Thursday/Friday shutdown as a data gap. For a
    session venue the expected gap is Wednesday 09:00 -> Saturday 09:00, so the
    check compares Tehran session dates instead of wall-clock seconds. Weekly
    bars (Saturday to Saturday) are also legitimate.
    """
    previous = to_tehran(datetime.fromtimestamp(previous_epoch, tz=timezone.utc))
    following = to_tehran(datetime.fromtimestamp(next_epoch, tz=timezone.utc))
    if (previous.hour, previous.minute) != (9, 0) or (following.hour, following.minute) != (9, 0):
        return False
    delta_days = (following.date() - previous.date()).days
    if delta_days == 7 and previous.weekday() == 5:
        return True
    return following.date() == next_session_day(previous.date())


def phase(moment: datetime) -> dict[str, Any]:
    tehran = to_tehran(moment)
    bounds = session_bounds(tehran.date())
    next_open = next_session_open(tehran)
    if bounds is None:
        return {
            "state": CLOSED,
            "label": "تعطیل (پایان هفته‌ی کاری ایران: پنجشنبه و جمعه)",
            "session_day": False,
            "session_open": None,
            "session_close": None,
            "next_open": next_open.isoformat(timespec="minutes"),
            "tehran_now": tehran.isoformat(timespec="seconds"),
        }
    open_at, close_at = bounds
    if tehran < open_at:
        state, label = PRE_OPEN, "پیش از بازگشایی (سفارش‌گذاری فقط در سامانه‌ی کارگزار)"
    elif tehran < close_at:
        state, label = OPEN, "باز — دادوستد پیوسته"
    else:
        state, label = CLOSED, "بسته (پایان دادوستد پیوسته ۱۲:۳۰)"
    return {
        "state": state,
        "label": label,
        "session_day": True,
        "session_open": open_at.isoformat(timespec="minutes"),
        "session_close": close_at.isoformat(timespec="minutes"),
        "next_open": next_open.isoformat(timespec="minutes"),
        "tehran_now": tehran.isoformat(timespec="seconds"),
    }
