"""Per-trade evidence from the real engine, verified against hand-computed numbers.

Scenario (slippage and fees forced to 0 in config so every number is exact):
bars are 1h; the stub signals BUY on the decision at bar 2, filled at bar 3 open=103,
stop=100, target=112. Bar 3 spans high=106/low=102; bar 4 spans high=105/low=99.5
and touches the stop.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cat.backtest import run_backtest  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.data import Kline, SymbolFilter  # noqa: E402
from cat.strategy import Analysis, Proposal  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
STEP = 3600
START = 1_700_000_000

SYMBOL_FILTER = SymbolFilter(symbol="BTC/USDT", price_precision=1, qty_precision=6,
                             min_qty=0.000001, min_quote=3.0, max_quote=5_000_000.0,
                             tradable=True, fee_pct=0.1)


def _bars(rows: list[tuple[float, float, float, float]]) -> list[Kline]:
    return [Kline(open_time=START + i * STEP, open=o, high=h, low=l, close=c,
                  base_volume=100.0, quote_volume=100.0 * c, closed=True)
            for i, (o, h, l, c) in enumerate(rows)]


@pytest.fixture()
def config(tmp_path: Path):
    shutil.copytree(REPO / "schemas", tmp_path / "schemas")
    payload = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    payload["risk"]["max_slippage_pct"] = 0.0
    payload["fees"]["taker_pct"] = 0.0
    payload["fees"]["maker_pct"] = 0.0
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    (tmp_path / "state").mkdir()
    return load_config(root=tmp_path)


def _stub(buy_at: int, sell_reason: str | None = None):
    def propose(snap, position, **kwargs):
        if position is None:
            if snap.klines[-1].open_time == START + buy_at * STEP:
                return Proposal(action="BUY", confidence=0.9, rationale=["test entry"],
                                entry=103.0, stop=100.0, target=112.0,
                                analysis=Analysis(regime="TRENDING_UP", summary="stub"),
                                reason="entry_signal")
            return Proposal(action="NO_TRADE", confidence=0.0, rationale=[], entry=None,
                            stop=None, target=None, analysis=Analysis(regime="UNKNOWN", summary="stub"),
                            reason="no_signal")
        reason = sell_reason or "no_signal"
        action = "SELL" if sell_reason else "NO_TRADE"
        return Proposal(action=action, confidence=0.9, rationale=["test exit"], entry=None,
                        stop=None, target=None, analysis=Analysis(regime="TRENDING_UP", summary="stub"),
                        reason=reason)
    return propose


STOP_ROWS = [
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 101.0, 99.8, 101.0),
    (101.0, 101.8, 100.5, 103.0),
    (103.0, 106.0, 102.0, 104.0),
    (104.0, 105.0, 99.5, 100.2),
    (100.2, 100.8, 100.0, 100.4),
    (100.4, 101.0, 100.1, 100.6),
    (100.6, 101.2, 100.2, 100.8),
    (100.8, 101.4, 100.3, 101.0),
    (101.0, 101.6, 100.4, 101.2),
]


def test_stop_exit_carries_the_full_hand_computed_entry_evidence(config):
    result = run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                          filters=SYMBOL_FILTER, timeframe="1h", warmup=2,
                          propose_fn=_stub(buy_at=2))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "long"
    assert trade.reason == "stop"
    assert trade.entry_reason == "entry_signal"
    assert trade.decision_time == START + 3 * STEP
    assert trade.confidence == pytest.approx(0.9)
    assert trade.regime in {"UNKNOWN", "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"}
    assert isinstance(trade.indicators, dict)
    assert trade.entry == pytest.approx(103.0)
    assert trade.exit == pytest.approx(100.0)
    assert trade.initial_stop == pytest.approx(100.0)
    assert trade.initial_target == pytest.approx(112.0)
    assert trade.final_stop == pytest.approx(100.0)
    assert trade.final_target == pytest.approx(112.0)
    # risk = 3/103, reward/risk = 9/3
    assert trade.risk_pct == pytest.approx(2.913, abs=0.001)
    assert trade.rr_ratio == pytest.approx(3.0, abs=0.001)
    # floating extremes across bar 3 (high 106 / low 102) and bar 4 (high 105 / low 99.5)
    assert trade.mfe_pct == pytest.approx(2.913, abs=0.001)
    assert trade.mae_pct == pytest.approx(-3.398, abs=0.001)
    # fill at bar 3 open, closed on bar 4 -> exactly one hour held
    assert trade.entry_time == START + 3 * STEP
    assert trade.exit_time == START + 4 * STEP
    assert trade.holding_hours == pytest.approx(1.0)
    # deployed notional over the 1000 paper account, recorded at decision time
    assert trade.capital_pct == pytest.approx(trade.qty * 103.0 / 1000.0 * 100.0, abs=0.01)
    assert 0 < trade.capital_pct <= 100


def test_signal_exit_keeps_entry_reason_separate_from_exit_reason(config):
    rows = STOP_ROWS[:5]
    result = run_backtest(config, config.symbols[0], bars=_bars(rows), higher_bars=[],
                          filters=SYMBOL_FILTER, timeframe="1h", warmup=2,
                          propose_fn=_stub(buy_at=2, sell_reason="exit_trend_broken"))

    trade = result.trades[0]
    assert trade.reason == "exit_trend_broken"
    assert trade.entry_reason == "entry_signal"
    # the position never existed during bar 4 (exited at its open), so only bar 3 counts
    assert trade.mfe_pct == pytest.approx(2.913, abs=0.001)
    assert trade.mae_pct == pytest.approx(-0.971, abs=0.001)
    assert trade.final_stop == pytest.approx(100.0)
    assert trade.final_target == pytest.approx(112.0)


def test_end_of_test_close_reports_the_whole_holding_window(config):
    hold = (103.5, 104.8, 102.6, 104.0)
    rows = STOP_ROWS[:3] + [hold] * 7
    result = run_backtest(config, config.symbols[0], bars=_bars(rows), higher_bars=[],
                          filters=SYMBOL_FILTER, timeframe="1h", warmup=2,
                          propose_fn=_stub(buy_at=2))

    trade = result.trades[0]
    assert trade.reason == "end_of_test"
    assert trade.exit == pytest.approx(104.0)
    assert trade.holding_hours == pytest.approx(7.0)
    # entry fills at the first hold bar's open (103.5, not the stub's 103);
    # seven identical bars: high 104.8 / low 102.6 against entry 103.5
    assert trade.entry == pytest.approx(103.5)
    assert trade.mfe_pct == pytest.approx(1.256, abs=0.001)
    assert trade.mae_pct == pytest.approx(-0.870, abs=0.001)


def test_store_persists_and_returns_the_evidence_blob(config, tmp_path):
    from cat.store import Store

    result = run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                          filters=SYMBOL_FILTER, timeframe="1h", warmup=2,
                          propose_fn=_stub(buy_at=2))
    store = Store(tmp_path / "platform.sqlite3")
    run_id = store.record_run(kind="backtest", symbol=config.symbols[0], timeframe="1h",
                              params_label="test", params={}, dataset_id=None,
                              window=("a", "b"), metrics=result.metrics, trades=result.trades)

    row = store.get_run(run_id)["trades"][0]
    evidence = row["evidence"]
    assert evidence["entry_reason"] == "entry_signal"
    assert evidence["side"] == "long"
    assert evidence["mfe_pct"] == pytest.approx(2.913, abs=0.001)
    assert evidence["mae_pct"] == pytest.approx(-3.398, abs=0.001)
    assert evidence["risk_pct"] == pytest.approx(2.913, abs=0.001)
    assert evidence["rr_ratio"] == pytest.approx(3.0, abs=0.001)
    assert evidence["holding_hours"] == pytest.approx(1.0)
    assert evidence["regime"] in {"UNKNOWN", "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"}


def test_existing_database_gains_the_evidence_column_in_place(tmp_path):
    from cat.store import Store

    path = tmp_path / "platform.sqlite3"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE run_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,"
        " symbol TEXT NOT NULL, entry_time INTEGER, exit_time INTEGER, entry REAL, exit REAL,"
        " qty REAL, pnl REAL, fees REAL, reason TEXT)"
    )
    raw.commit()
    raw.close()

    store = Store(path)
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(run_trades)")}
    assert "evidence_json" in columns


def _spy(seen: list[tuple[int, int]]):
    """A propose_fn that records (window length, last bar) instead of trading."""
    def propose(snap, position, **kwargs):
        seen.append((len(snap.klines), snap.klines[-1].open_time))
        return Proposal(action="NO_TRADE", confidence=0.0, rationale=[], entry=None,
                        stop=None, target=None, analysis=Analysis(regime="UNKNOWN", summary="stub"),
                        reason="no_signal")
    return propose


def _flat(count: int) -> list[Kline]:
    return _bars([(100.0, 101.0, 99.0, 100.0)] * count)


def test_window_bars_clamps_the_replay_window_to_the_size_production_uses(config):
    """D-020 loyalty-to-input rule: the classifier sees at most N trailing bars.

    Production hands `GateProvider.klines(limit=300)` a fixed trailing window, so a
    replay that grows without bound measures a different protocol (F-020). The clamp
    must never widen past window_bars and must always end on the decision bar.
    """
    seen: list[tuple[int, int]] = []
    run_backtest(config, config.symbols[0], bars=_flat(20), higher_bars=[], filters=SYMBOL_FILTER,
                 timeframe="1h", warmup=2, window_bars=6, propose_fn=_spy(seen))
    assert seen, "the replay never reached a decision"
    assert max(length for length, _ in seen) == 6
    assert all(length == 6 for length, _ in seen)
    for index, (length, last_ts) in enumerate(seen):
        assert last_ts == START + (index + 6) * STEP  # decision bar, never a future one


def test_the_unclamped_replay_still_grows_one_bar_per_step(config):
    """window_bars=None is today's untouched behaviour, not a hidden default."""
    seen: list[tuple[int, int]] = []
    run_backtest(config, config.symbols[0], bars=_flat(12), higher_bars=[], filters=SYMBOL_FILTER,
                 timeframe="1h", warmup=2, propose_fn=_spy(seen))
    assert [length for length, _ in seen] == [3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_an_impossible_window_fails_closed_instead_of_silently_shrinking(config):
    with pytest.raises(ValueError):
        run_backtest(config, config.symbols[0], bars=_flat(20), higher_bars=[], filters=SYMBOL_FILTER,
                     timeframe="1h", window_bars=1, propose_fn=_spy([]))
    with pytest.raises(ValueError):
        run_backtest(config, config.symbols[0], bars=_flat(5), higher_bars=[], filters=SYMBOL_FILTER,
                     timeframe="1h", warmup=4, window_bars=4, propose_fn=_spy([]))


def _ticks(seen: list[int]):
    """A propose_fn that always wants in when flat, and records the bar it was asked about."""
    def propose(snap, position, **kwargs):
        seen.append(snap.klines[-1].open_time)
        if position is None:
            return Proposal(action="BUY", confidence=0.9, rationale=["test entry"],
                            entry=103.0, stop=100.0, target=112.0,
                            analysis=Analysis(regime="TRENDING_UP", summary="stub"),
                            reason="entry_signal")
        return Proposal(action="NO_TRADE", confidence=0.0, rationale=[], entry=None, stop=None,
                        target=None, analysis=Analysis(regime="TRENDING_UP", summary="stub"),
                        reason="no_signal")
    return propose


def test_off_tick_bars_are_never_asked_for_a_proposal(config):
    """Production decides on its own clock, so an off-tick bar must not even reach propose."""
    seen: list[int] = []
    run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                 filters=SYMBOL_FILTER, timeframe="1h", warmup=2, decision_every=3,
                 propose_fn=_ticks(seen))
    assert seen == [START + i * STEP for i in (2, 5, 8)]


def test_a_throttled_replay_only_enters_on_decision_ticks(config):
    seen: list[int] = []
    result = run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                          filters=SYMBOL_FILTER, timeframe="1h", warmup=2, decision_every=4,
                          propose_fn=_ticks(seen))
    assert result.trades, "the fixture must actually take a position"
    for trade in result.trades:
        decided_bar = (trade.entry_time - START) // STEP - 1
        assert (decided_bar - 2) % 4 == 0


def test_resting_brackets_still_close_a_position_between_ticks(config):
    """Off-tick bars abandon the *proposal*, not the trade: stop/target are exchange-side."""
    result = run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                          filters=SYMBOL_FILTER, timeframe="1h", warmup=2, decision_every=6,
                          propose_fn=_ticks([]))
    assert result.trades[0].reason == "stop"
    assert result.trades[0].exit_time == START + 4 * STEP


def test_decision_every_one_leaves_the_historical_replay_untouched(config):
    seen_default: list[int] = []
    default = run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                           filters=SYMBOL_FILTER, timeframe="1h", warmup=2,
                           propose_fn=_ticks(seen_default))
    seen_explicit: list[int] = []
    explicit = run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                            filters=SYMBOL_FILTER, timeframe="1h", warmup=2, decision_every=1,
                            propose_fn=_ticks(seen_explicit))
    assert seen_explicit == seen_default
    assert [(t.entry_time, t.exit_time, t.pnl, t.reason) for t in explicit.trades] == \
           [(t.entry_time, t.exit_time, t.pnl, t.reason) for t in default.trades]
    assert explicit.rejected == default.rejected


def test_a_cadence_below_one_bar_fails_closed(config):
    with pytest.raises(ValueError):
        run_backtest(config, config.symbols[0], bars=_bars(STOP_ROWS), higher_bars=[],
                     filters=SYMBOL_FILTER, timeframe="1h", decision_every=0, propose_fn=_ticks([]))
