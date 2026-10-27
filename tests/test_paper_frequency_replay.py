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
