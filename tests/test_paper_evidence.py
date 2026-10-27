"""Gate-B evidence tests: continuity, downtime, restart recovery and ledger integrity.

Pure-function tests only — no Engine, no network, no database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cat.paper_evidence import analyze

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshots(times: list[datetime], mode: str = "PAPER") -> list[dict]:
    return [
        {"ts": ts.isoformat(), "equity": 1000.0, "drawdown_pct": 0.0, "mode": mode}
        for ts in times
    ]


def _cadence(count: int, hours: float = 3.0) -> list[datetime]:
    return [START + timedelta(hours=index * hours) for index in range(count)]


def _run(times: list[datetime], **kwargs) -> dict:
    params = {
        "snapshots": _snapshots(times),
        "interval_hours": 3.0,
        "now": times[-1] if times else START,
        "min_days": 1.0,
    }
    params.update(kwargs)
    return analyze(**params)


def test_twenty_eight_days_of_three_hour_cycles_pass():
    times = _cadence(28 * 8 + 1)
    result = analyze(
        snapshots=_snapshots(times),
        orders=[{"id": "o1", "client_order_id": "c1", "mode": "PAPER"}],
        fills=[{"id": 1, "client_order_id": "c1", "mode": "PAPER", "fee": 0.1}],
        trades=[{"id": 1, "net": 2.5, "fees": 0.1, "mode": "PAPER"}],
        interval_hours=3.0,
        now=times[-1],
        min_days=28,
    )
    assert result["status"] == "PASS"
    assert result["window"]["span_days"] == 28.0
    assert result["window"]["cycles_observed"] == 225
    assert result["window"]["cycles_expected"] == 225
    assert result["window"]["on_time_fraction"] == 1.0
    assert result["window"]["days_remaining"] == 0
    assert result["ledger"]["orphan_fills"] == 0
    assert result["checks"] == {
        "integrity_ok": True,
        "recovery_ok": True,
        "long_enough": True,
        "continuous_enough": True,
        "live_now": True,
    }


def test_a_short_window_is_incomplete_not_passed():
    times = _cadence(22)  # 63 hours = 2.625 days
    result = analyze(snapshots=_snapshots(times), interval_hours=3.0, now=times[-1], min_days=28)
    assert result["status"] == "INCOMPLETE"
    assert result["window"]["span_days"] == 2.625
    assert result["window"]["days_remaining"] == 25.375
    assert result["window"]["milestone_at"] is not None
    assert any("required" in reason for reason in result["reasons"])


def test_downtime_gaps_lower_the_on_time_fraction():
    times = (
        _cadence(5)
        + [START + timedelta(hours=12 + 14)]  # 14h hole after hour 12
        + [START + timedelta(hours=12 + 14 + 14)]
        + [START + timedelta(hours=12 + 14 + 14 + 20)]
        + [START + timedelta(hours=12 + 14 + 14 + 20 + 18)]
        + [START + timedelta(hours=12 + 14 + 14 + 20 + 18 + 16)]
    )
    result = analyze(snapshots=_snapshots(times), interval_hours=3.0, now=times[-1], min_days=1)
    assert result["window"]["downtime_gaps"] >= 2
    assert result["window"]["on_time_fraction"] < 0.9
    assert result["checks"]["continuous_enough"] is False
    assert result["status"] == "INCOMPLETE"


def test_restart_followed_by_a_cycle_is_recovered():
    times = _cadence(6)
    restart = times[-1] - timedelta(hours=1)
    result = _run(times, restarts=[restart])
    assert result["restarts"]["in_window"] == 1
    assert result["restarts"]["recovered"] == 1
    assert result["restarts"]["historical_missed"] == 0
    assert result["restarts"]["pending"] == 0
    assert result["restarts"]["latest"]["state"] == "recovered"
    assert result["checks"]["recovery_ok"] is True


def test_restart_without_a_following_cycle_is_unresolved():
    times = _cadence(6)
    restart = times[-1] + timedelta(minutes=30)
    result = analyze(
        snapshots=_snapshots(times),
        restarts=[restart],
        interval_hours=3.0,
        now=times[-1] + timedelta(hours=8),
        min_days=1,
    )
    assert result["restarts"]["in_window"] == 1
    assert result["restarts"]["recovered"] == 0
    assert result["restarts"]["historical_missed"] == 1
    assert result["restarts"]["latest"]["state"] == "missed"
    assert result["restarts"]["missed"][0]["next_cycle_at"] is None
    assert result["checks"]["recovery_ok"] is False
    assert result["status"] == "FAIL"


def test_historical_missed_restart_does_not_poison_a_recovered_latest_restart():
    times = [START + timedelta(hours=hour) for hour in (0, 3, 6, 20, 23, 26)]
    result = analyze(
        snapshots=_snapshots(times),
        restarts=[START + timedelta(hours=7), START + timedelta(hours=21)],
        interval_hours=3.0,
        now=times[-1],
        min_days=1,
    )
    assert result["restarts"]["recovered"] == 1
    assert result["restarts"]["historical_missed"] == 1
    assert result["restarts"]["latest"]["state"] == "recovered"
    assert result["checks"]["recovery_ok"] is True
    assert result["status"] == "INCOMPLETE"  # 4 of 5 gaps on time
    assert any("earlier restart" in reason for reason in result["reasons"])


def test_pending_restart_is_not_failed_before_its_deadline():
    times = _cadence(6)
    restart = times[-1] + timedelta(minutes=30)
    result = analyze(
        snapshots=_snapshots(times),
        restarts=[restart],
        interval_hours=3.0,
        now=restart + timedelta(hours=1),
        min_days=1,
    )
    assert result["restarts"]["pending"] == 1
    assert result["checks"]["recovery_ok"] is True
    assert result["status"] == "INCOMPLETE"
    assert any("still inside" in reason for reason in result["reasons"])


def test_a_stale_last_cycle_blocks_pass_even_after_28_days():
    times = _cadence(28 * 8 + 1)
    result = analyze(
        snapshots=_snapshots(times),
        interval_hours=3.0,
        now=times[-1] + timedelta(hours=10),
        min_days=28,
    )
    assert result["checks"]["live_now"] is False
    assert result["status"] == "INCOMPLETE"
    assert any("not producing now" in reason for reason in result["reasons"])


def test_restart_before_the_window_is_ignored():
    times = _cadence(6)
    result = _run(times, restarts=[times[0] - timedelta(hours=5)], min_days=0.5)
    assert result["restarts"]["in_window"] == 0
    assert result["status"] == "PASS"


def test_orphan_fill_is_an_integrity_failure():
    times = _cadence(6)
    result = _run(
        times,
        orders=[{"id": "o1", "client_order_id": "c1", "mode": "PAPER"}],
        fills=[{"id": 1, "client_order_id": "c9", "mode": "PAPER", "fee": 0.1}],
    )
    assert result["ledger"]["orphan_fills"] == 1
    assert result["checks"]["integrity_ok"] is False
    assert result["status"] == "FAIL"


def test_a_live_row_in_the_paper_ledger_fails_the_gate():
    times = _cadence(6)
    result = _run(times, orders=[{"id": "o1", "client_order_id": "c1", "mode": "LIVE"}])
    assert result["checks"]["integrity_ok"] is False
    assert result["status"] == "FAIL"
    assert any("outside mode=PAPER" in reason for reason in result["reasons"])


def test_missing_or_negative_equity_fails_the_gate():
    times = _cadence(4)
    snapshots = _snapshots(times)
    snapshots[2]["equity"] = None
    snapshots[3]["equity"] = -1.0
    result = analyze(snapshots=snapshots, interval_hours=3.0, now=times[-1], min_days=1)
    assert result["checks"]["integrity_ok"] is False
    assert result["status"] == "FAIL"


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        analyze(snapshots=[], interval_hours=0, now=START)


def _cadence_runs(count: int, **kwargs) -> dict:
    times = _cadence(count)
    params = {"snapshots": _snapshots(times), "restarts": [], "interval_hours": 3.0,
              "now": times[-1] + timedelta(hours=1)}
    params.update(kwargs)
    return analyze(**params)


def test_zero_closed_trades_projects_stalled():
    """The recorded PAPER window so far: cycles land on time, nothing trades."""
    result = _cadence_runs(13, trades=[], min_closed_trades=30)
    cadence = result["cadence"]
    assert cadence["state"] == "stalled"
    assert cadence["observed_trades_per_day"] == 0.0
    assert cadence["projected_at_milestone"] == 0.0
    assert cadence["required_trades_per_day"] > 0


def test_a_cadence_below_the_required_rate_is_behind_not_stalled():
    result = _cadence_runs(13, trades=[{"id": 1, "net": 1.0, "fees": 0.1, "mode": "PAPER"}],
                           min_closed_trades=30)
    cadence = result["cadence"]
    assert cadence["state"] == "behind"
    assert cadence["observed_trades_per_day"] < cadence["required_trades_per_day"]


def test_a_cadence_that_clears_the_count_projects_on_track():
    result = _cadence_runs(13, min_closed_trades=30,
                           trades=[{"id": index, "net": 1.0, "fees": 0.1, "mode": "PAPER"}
                                   for index in range(20)])
    assert result["cadence"]["state"] == "on-track"
    assert result["cadence"]["projected_at_milestone"] >= 30


def test_the_cadence_projection_never_changes_the_verdict():
    """30 closed trades inside a 1.5-day window is still INCOMPLETE.

    The projection is a report about arithmetic, not a gate: reaching the trade
    count must not shorten the window, and missing it must not fail anything.
    """
    times = _cadence(13)
    trades = [{"id": index, "net": 1.0, "fees": 0.1, "mode": "PAPER"} for index in range(30)]
    result = analyze(snapshots=_snapshots(times), trades=trades, restarts=[],
                     interval_hours=3.0, now=times[-1] + timedelta(hours=1))
    assert result["status"] == "INCOMPLETE"
    assert result["cadence"]["closed_trades"] == 30
    assert result["cadence"]["state"] == "met"


def _decision(ts: datetime, symbol: str, reason: str, *, action: str = "NO_TRADE") -> dict:
    """One row exactly as the deployed loop writes it into the ``decisions`` table."""
    text = (f"no order for action NO_TRADE: {reason}" if action in ("NO_TRADE", "HOLD")
            else f"{symbol} 4h: regime=TRENDING_UP, price=0.71")
    return {"ts": ts.isoformat(), "symbol": symbol, "action": action,
            "rationale_json": json.dumps([text, "ema20 above ema50 (uptrend structure)"]),
            "mode": "PAPER"}


def _funnel(rows: list[dict]) -> dict:
    times = _cadence(2)
    return analyze(snapshots=_snapshots(times), interval_hours=3.0,
                   now=times[-1] + timedelta(hours=1), decisions=rows)["decision_funnel"]


def test_decision_funnel_counts_the_reasons_the_loop_recorded():
    """The funnel is arithmetic over the build's own rows: every reason is one bucket."""
    first, second = _cadence(2)
    rows = [_decision(first, "BTC_USDT", "regime_filter"),
            _decision(first, "ETH_USDT", "regime_filter"),
            _decision(first, "SOL_USDT", "no_atr"),
            _decision(second, "BTC_USDT", "regime_filter"),
            _decision(second, "ETH_USDT", "insufficient_history")]
    funnel = _funnel(rows)
    assert funnel["decisions"] == 5
    assert funnel["cycles"] == 2
    assert funnel["symbols"] == 3
    assert funnel["by_reason_counts"] == {"regime_filter": 3, "insufficient_history": 1, "no_atr": 1}
    assert funnel["by_reason_pct"]["regime_filter"] == 60.0
    assert sum(funnel["by_reason_pct"].values()) == pytest.approx(100.0)


def test_decision_funnel_separates_filled_orders_from_rejections():
    """A BUY is not a rejection reason; it must not be folded into a NO_TRADE bucket."""
    first = _cadence(1)[0]
    funnel = _funnel([_decision(first, "BTC_USDT", "regime_filter"),
                      _decision(first, "ASTER_USDT", "", action="BUY")])
    assert funnel["executed"] == {"executed:buy": 1}
    assert funnel["by_reason_counts"] == {"executed:buy": 1, "regime_filter": 1}


def test_decision_funnel_keeps_unparseable_rationale_visible():
    """Free text with no snake_case token stays ``unparsed`` instead of a guess."""
    first = _cadence(1)[0]
    row = {"ts": first.isoformat(), "symbol": "XUSDT", "action": "NO_TRADE",
           "rationale_json": json.dumps(["market looked fine today"]), "mode": "PAPER"}
    assert _funnel([row])["by_reason_counts"] == {"unparsed": 1}


def test_an_empty_decision_table_is_a_zero_funnel_not_a_failure():
    funnel = _funnel([])
    assert funnel == {"decisions": 0, "cycles": 0, "symbols": 0, "by_reason_counts": {},
                      "by_reason_pct": {}, "executed": {}, "note": funnel["note"]}


def test_the_decision_funnel_never_changes_the_verdict():
    """93% regime_filter, or a single executed trade, or nothing at all: same status.

    The funnel is evidence about the loop, not a gate — D-020 pre-registers it as
    diagnostic, so the verdict computed with the loop's own rows must equal the
    verdict computed without them, key for key except the funnel itself.
    """
    times = _cadence(13)
    first, second = times[0], times[1]
    rows = [_decision(first, f"SYM{index}_USDT", "regime_filter") for index in range(99)]
    rows.append(_decision(second, "ASTER_USDT", "", action="BUY"))
    with_funnel = analyze(snapshots=_snapshots(times), interval_hours=3.0,
                          now=times[-1] + timedelta(hours=1), decisions=rows)
    without = analyze(snapshots=_snapshots(times), interval_hours=3.0,
                      now=times[-1] + timedelta(hours=1))
    assert with_funnel["status"] == without["status"] == "INCOMPLETE"
    assert with_funnel["reasons"] == without["reasons"]
    assert with_funnel["checks"] == without["checks"]
    assert with_funnel["cadence"] == without["cadence"]
    assert with_funnel["decision_funnel"]["by_reason_counts"] == {"regime_filter": 99, "executed:buy": 1}
