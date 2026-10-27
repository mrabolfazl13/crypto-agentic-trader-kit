"""Pure-function tests for the frequency replay's symbol normalizer and its bounds.

The replay speaks to `cat.risk.evaluate`, which rejects any symbol outside the
allowlist. Stored ids are `BTCUSDT`, the allowlist is `BTC_USDT`, so a normalizer
that silently returns the stored id turns "the gate refused" into "the allowlist
refused" — and the cadence this tool exists to measure comes out too low.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from paper_frequency_replay import (MIN_WINDOW_BARS, canonical_symbol,  # noqa: E402
                          decision_every_bars, serial_account_schedule)


@pytest.mark.parametrize("stored, expected", [
    ("BTCUSDT", "BTC_USDT"),
    ("LINKUSDT", "LINK_USDT"),
    ("AAVEUSDT", "AAVE_USDT"),
    ("ASTERUSDT", "ASTER_USDT"),
    ("1000PEPEUSDT", "1000PEPE_USDT"),
    ("ETHFDUSD", "ETH_FDUSD"),
    ("BTCUSDC", "BTC_USDC"),
    ("SOLUSD", "SOL_USD"),
])
def test_every_quote_suffix_is_stripped_regardless_of_base_length(stored: str, expected: str):
    assert canonical_symbol(stored) == expected


def test_a_base_longer_than_three_letters_is_never_left_unchanged():
    """The first two runs of the tool dropped LINK/AAVE/DOGE/ASTER by a length heuristic."""
    for stored in ("LINKUSDT", "DOGEUSDT", "AAVEUSDT", "ASTERUSDT", "PUMPUSDT", "NEARUSDT"):
        assert canonical_symbol(stored) != stored
        assert canonical_symbol(stored).endswith("_USDT")


def test_an_id_without_a_known_quote_passes_through_unchanged():
    assert canonical_symbol("BTC") == "BTC"


def test_the_replay_refuses_a_window_smaller_than_production_uses():
    assert MIN_WINDOW_BARS == 300


def test_every_replayed_lake_symbol_exists_in_the_deployed_allowlist():
    """The failure this guards is silent: an unmapped id is rejected by risk.py:95 and
    counted as 'the build refused to trade', which lowers the measured cadence.

    Skipped where the candle lake is not installed.
    """
    from pathlib import Path

    from cat.config import load_config
    from cat.quant.settings import load_quant_settings
    from cat.quant.storage import universe_symbols

    try:
        config = load_config(root=Path(__file__).resolve().parents[1])
        settings = load_quant_settings(config)
        symbols = universe_symbols(settings, limit=30)
    except Exception as exc:  # no lake on this machine
        pytest.skip(f"lake unavailable: {type(exc).__name__}: {exc}")
    if not symbols:
        pytest.skip("lake has no universe")
    unmapped = [stored for stored in symbols if canonical_symbol(stored) not in config.symbols]
    assert unmapped == [], f"would be rejected by the allowlist: {unmapped}"


def _trade(entry: int, exit_: int, pnl: float, fees: float = 0.05) -> dict:
    return {"symbol": "XUSDT", "entry_time": entry, "exit_time": exit_,
            "pnl": pnl, "fees": fees, "reason": "stop"}


DAY = 86400


def test_the_schedule_takes_one_position_at_a_time():
    """Overlapping entries are not extra trades: the allocation cap serialises them."""
    schedule = serial_account_schedule([[_trade(0, 5 * DAY, 1.0), _trade(2 * DAY, 9 * DAY, -1.0)]],
                                       window_days=10.0, max_trades_per_day=4,
                                       cooldown_minutes_after_loss=0)
    assert schedule["taken"] == 1
    assert schedule["skipped"]["overlapping_position"] == 1
    assert schedule["trades_per_day"] == 0.1


def test_the_schedule_applies_the_per_day_cap_and_the_loss_cooldown():
    same_day = [_trade(index * 2 * 3600, index * 2 * 3600 + 3600, 0.5) for index in range(6)]
    capped = serial_account_schedule([same_day], window_days=1.0, max_trades_per_day=4,
                                     cooldown_minutes_after_loss=0)
    assert capped["taken"] == 4
    assert capped["skipped"] == {"overlapping_position": 0, "max_trades_per_day": 2,
                                "cooldown_after_loss": 0}

    cooled = [_trade(0, 3600, -1.0), _trade(1.5 * 3600, 1.7 * 3600, 1.0), _trade(3 * 3600, 4 * 3600, 1.0)]
    schedule = serial_account_schedule([cooled], window_days=1.0, max_trades_per_day=4,
                                       cooldown_minutes_after_loss=60)
    assert schedule["taken"] == 2
    assert schedule["skipped"]["cooldown_after_loss"] == 1
    assert schedule["skipped"]["overlapping_position"] == 0


def test_the_schedule_reports_the_units_gate_b_is_judged_in():
    """30 trades in 28 days is the gate: days_to_30_trades is the honest answer."""
    week = [_trade(index * 2 * DAY, index * 2 * DAY + DAY, 1.0 if index % 2 else -1.0)
            for index in range(4)]
    schedule = serial_account_schedule([week], window_days=28.0, max_trades_per_day=4,
                                       cooldown_minutes_after_loss=0)
    assert schedule["taken"] == 4
    assert schedule["trades_per_day"] == round(4 / 28.0, 4)
    assert schedule["days_to_30_trades"] == round(30.0 / (4 / 28.0), 2)
    assert schedule["profit_factor"] == 1.0


def test_production_cadence_uses_the_deployed_cycle_clock():
    """A 3h cycle is one decision per 12 15m bars, 3 hourly bars, and still every 4h bar."""
    assert decision_every_bars(3.0, "15m", cadence="production") == 12
    assert decision_every_bars(3.0, "1h", cadence="production") == 3
    assert decision_every_bars(3.0, "4h", cadence="production") == 1
    assert decision_every_bars(24.0, "4h", cadence="production") == 6


def test_the_replay_cadence_default_decides_every_bar():
    for timeframe in ("15m", "1h", "4h"):
        assert decision_every_bars(3.0, timeframe, cadence="replay") == 1


def _serial_reference(timelines, *, window_days, max_trades_per_day, cooldown_minutes_after_loss):
    """The D-020/D-022 schedule, copied verbatim so `max_concurrent=1` has something to be checked against."""
    trades = sorted((row for timeline in timelines for row in timeline), key=lambda row: row["entry_time"])
    taken, per_day, free_at = [], {}, 0
    overlap = cap = cooldown = 0
    for trade in trades:
        if trade["entry_time"] < free_at:
            overlap += 1
            continue
        day = trade["entry_time"] // 86400
        if per_day.get(day, 0) >= max_trades_per_day:
            cap += 1
            continue
        if taken:
            last = taken[-1]
            if last["pnl"] <= 0 and trade["entry_time"] < last["exit_time"] + cooldown_minutes_after_loss * 60:
                cooldown += 1
                continue
        taken.append(trade)
        per_day[day] = per_day.get(day, 0) + 1
        free_at = trade["exit_time"]
    wins = [row for row in taken if row["pnl"] > 0]
    gross_loss = abs(sum(row["pnl"] for row in taken if row["pnl"] <= 0))
    days = max(window_days, 0.001)
    return {"taken": len(taken),
            "skipped": {"overlapping_position": overlap, "max_trades_per_day": cap,
                        "cooldown_after_loss": cooldown},
            "trades_per_day": round(len(taken) / days, 4),
            "win_rate_pct": round(len(wins) / len(taken) * 100.0, 2) if taken else None,
            "profit_factor": round(sum(row["pnl"] for row in wins) / gross_loss, 3) if gross_loss > 0 else None,
            "net_pnl_per_1000": round(sum(row["pnl"] for row in taken), 4),
            "fees": round(sum(row["fees"] for row in taken), 4),
            "days_to_30_trades": round(30.0 / (len(taken) / days), 2) if taken else None}


@pytest.mark.parametrize("cap,cooldown", [(4, 0), (1, 0), (4, 60), (2, 240)])
def test_k_one_reproduces_the_recorded_serial_schedule_exactly(cap: int, cooldown: int):
    """D-023 was pre-registered on this: widening the budget must not move the K=1 numbers."""
    timelines = [[_trade(index * 3600, index * 3600 + 11 * 3600, 1.0 if index % 3 else -1.0)
                  for index in range(20)],
                 [_trade(index * 5400, index * 5400 + 900, 0.5) for index in range(15)],
                 [_trade(0, 4 * 3600, -1.0), _trade(4 * 3600, 5 * 3600, 2.0),
                  _trade(4 * 3600 + 1800, 9 * 3600, 3.0)]]
    kwargs = dict(window_days=5.0, max_trades_per_day=cap, cooldown_minutes_after_loss=cooldown)
    got = serial_account_schedule(timelines, **kwargs)
    want = _serial_reference(timelines, **kwargs)
    assert {key: got[key] for key in want} == want
    assert got["max_concurrent"] == 1
    assert got["peak_concurrent_positions"] == (1 if got["taken"] else 0)


def test_a_wider_position_budget_never_takes_fewer_trades_and_respects_its_cap():
    timelines = [[_trade(index * 3600, index * 3600 + 6 * 3600, 1.0 if index % 3 else -1.0)
                  for index in range(30)]]
    previous = 0
    for k in (1, 2, 3, 5):
        schedule = serial_account_schedule(timelines, window_days=5.0, max_trades_per_day=40,
                                           cooldown_minutes_after_loss=0, max_concurrent=k)
        assert schedule["max_concurrent"] == k
        assert schedule["peak_concurrent_positions"] <= k
        assert schedule["taken"] >= previous
        previous = schedule["taken"]
    serial = serial_account_schedule(timelines, window_days=5.0, max_trades_per_day=40,
                                     cooldown_minutes_after_loss=0)
    wide = serial_account_schedule(timelines, window_days=5.0, max_trades_per_day=40,
                                   cooldown_minutes_after_loss=0, max_concurrent=3)
    assert wide["taken"] > serial["taken"]
    assert wide["skipped"]["overlapping_position"] < serial["skipped"]["overlapping_position"]


def test_the_per_day_cap_still_binds_when_positions_may_overlap():
    """Concurrency is a position budget, not a licence to break max_trades_per_day."""
    timelines = [[_trade(index * 3600, index * 3600 + 12 * 3600, 1.0) for index in range(20)]]
    schedule = serial_account_schedule(timelines, window_days=1.0, max_trades_per_day=4,
                                       cooldown_minutes_after_loss=0, max_concurrent=5)
    assert schedule["taken"] == 4
    assert schedule["skipped"]["max_trades_per_day"] == 16


def test_the_concurrency_budget_must_be_at_least_one_position():
    with pytest.raises(ValueError):
        serial_account_schedule([[]], window_days=1.0, max_trades_per_day=4,
                                cooldown_minutes_after_loss=0, max_concurrent=0)
