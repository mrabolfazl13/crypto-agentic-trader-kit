"""Offline tests for the staged experiment ladder that feeds the stages dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

import pytest

from conftest import make_snapshot

from cat.profiles import (
    _verdict,
    horizon_bars,
    list_runs,
    load_run,
    pooled_stats,
    render_markdown,
    save_profiles,
    stage_plan,
)
from cat.strategies import default_params
from cat.strategy import Position
from cat.strategy_lab import LAB_STRATEGIES


def _signal_kwargs(config):
    return {
        "confidence_threshold": config.confidence_threshold,
        "require_multiple_signals": config.require_multiple_signals,
        "no_trade_if_signals_conflict": config.no_trade_if_signals_conflict,
        "min_reward_risk_ratio": config.limits.min_reward_risk_ratio,
        "maker_fee_pct": config.maker_fee_pct,
        "taker_fee_pct": config.taker_fee_pct,
        "est_slippage_pct": config.limits.max_slippage_pct,
        "params": default_params(),
    }


def test_entry_only_stage_overrides_do_not_break_an_open_position(config):
    """A stage bound with min_atr_pct must still use the shared exit policy.

    Regression: the extra switch was forwarded into strategy.propose, which does
    not accept it, and every symbol with an open position was silently skipped.
    """
    snapshot = make_snapshot()
    last = snapshot.klines[-1].close
    opened = datetime.fromtimestamp(snapshot.klines[-6].open_time, tz=timezone.utc).isoformat(timespec="seconds")
    position = Position(
        symbol="BTC_USDT", qty=1.0, avg_entry=last, stop=last * 0.9, target=last * 1.2,
        decision_id="test-open", opened_at=opened,
    )
    with_extra = [stage for round_no in (1, 2, 3) for stage in stage_plan("4h", "day", round_no) if stage.extra]
    assert with_extra  # the plan does exercise partial-bound switches
    for stage in with_extra:
        signal = partial(LAB_STRATEGIES[stage.strategy], **stage.extra)
        proposal = signal(snapshot, position, **_signal_kwargs(config))
        assert proposal.action in ("HOLD", "SELL")
        assert proposal.reason != "insufficient_history"


def test_horizon_bars_never_pretends_a_bar_can_be_shorter_than_the_timeframe():
    assert horizon_bars("1h", "short") == 3
    assert horizon_bars("4h", "short") == 1  # 4h candles cannot produce a 3h hold
    assert horizon_bars("1h", "day") == 23
    assert horizon_bars("4h", "day") == 5
    assert horizon_bars("15m", "day") == 95
    assert horizon_bars("1h", "none") == 0


def test_stage_plan_changes_one_thing_at_a_time_and_shares_the_horizon():
    stages = stage_plan("1h", "short")
    ids = [stage.id for stage in stages]
    assert ids[0] == "baseline-trend" and len(set(ids)) == len(ids)
    timed = [stage for stage in stages if stage.params.max_hold_bars]
    assert timed and all(stage.params.max_hold_bars == 3 for stage in timed)
    assert stages[0].params.max_hold_bars == 0
    assert any(stage.extra for stage in stages)  # volatility stages are explicitly different


def test_round_two_tests_the_fee_churn_hypothesis_with_fresh_crosses():
    stages = stage_plan("4h", "day", 2)
    ids = [stage.id for stage in stages]
    assert len(set(ids)) == len(ids) == 5
    assert {stage.strategy for stage in stages} == {"pullback_rsi", "momentum_20", "breakout_30"}
    timed = [stage for stage in stages if stage.params.max_hold_bars]
    assert all(stage.params.max_hold_bars == 5 for stage in timed)  # 5 × 4h = 20h
    assert stages[0].id == "pullback" and stages[0].params.max_hold_bars == 0
    fresh = {stage.strategy for stage in stages if (stage.extra or {}).get("fresh_only")}
    assert fresh == {"momentum_20", "breakout_30"}
    volatility = [stage for stage in stages if (stage.extra or {}).get("min_atr_pct")]
    assert volatility and volatility[0].extra["min_atr_pct"] == 1.8


def test_round_one_plan_is_unchanged_by_the_second_round():
    assert [stage.id for stage in stage_plan("4h", "day", 1)][0] == "baseline-trend"
    assert len(stage_plan("4h", "day", 1)) == 6


def test_round_three_perturbs_only_the_pullback_family():
    stages = stage_plan("4h", "day", 3)
    assert len(stages) == 5 and len({stage.id for stage in stages}) == 5
    assert {stage.strategy for stage in stages} == {"pullback_rsi"}
    untimed = {stage.id for stage in stages if stage.params.max_hold_bars == 0}
    assert untimed == {"pullback-rsi45", "pullback-wide"}
    assert any((stage.extra or {}).get("rsi_reset") == 45.0 for stage in stages)
    targets = {stage.params.atr_target_mult for stage in stages}
    assert {4.0, 5.0}.issubset(targets)


def _row(symbol: str, split: int, trades: list[dict]) -> dict:
    return {"symbol": symbol, "split_ts": split, "trades": trades, "metrics": {"max_drawdown_pct": 1.0}}


def test_pooled_stats_splits_by_exit_time_and_never_double_counts():
    split = 1_000_000
    rows = [_row("A", split, [
        {"pnl": 10.0, "fees": 0.2, "reason": "target", "exit_time": split - 10, "holding_hours": 3.0},
        {"pnl": -4.0, "fees": 0.2, "reason": "stop", "exit_time": split + 10, "holding_hours": 2.0},
    ])]
    train = pooled_stats(rows, "train", 1000.0)
    validation = pooled_stats(rows, "validation", 1000.0)
    assert train["trades"] == 1 and validation["trades"] == 1
    assert train["gross_profit"] == 10.0 and train["gross_loss"] == 0.0
    assert validation["gross_loss"] == -4.0
    assert validation["within_3h_pct"] == 100.0
    assert pooled_stats(rows, "all", 1000.0)["net_pnl"] == 6.0


def test_pooled_stats_without_trades_reports_no_ratios():
    stats = pooled_stats([], "all", 1000.0)
    assert stats["trades"] == 0
    assert stats["win_rate_pct"] is None and stats["profit_factor"] is None
    assert stats["median_hold_hours"] is None


def _stage(stage_id: str, train_pf, validation_pf, train_net, validation_net, trades=30) -> dict:
    def block(pf, net):
        return {"trades": trades, "profit_factor": pf, "net_pnl": net, "mean_net_return_pct": net / 100.0}
    return {"id": stage_id, "train": block(train_pf, train_net), "validation": block(validation_pf, validation_net),
            "all": block(train_pf, train_net + validation_net)}


def test_verdict_promotes_only_a_stage_that_survives_validation():
    survived = _stage("survived", 1.8, 1.4, 50.0, 20.0)
    collapsed = _stage("collapsed", 2.5, 0.4, 90.0, -30.0)
    verdict = _verdict([collapsed, survived])
    assert verdict["finalist"] == "collapsed"  # selected on train alone
    assert verdict["promoted"] is False
    assert verdict["stable_stages"] == ["survived"]
    assert "collapsed" in verdict["text_fa"]


def test_verdict_does_not_promote_a_finalist_whose_train_half_lost():
    losing_train = _stage("late-bloomer", 0.979, 1.5, -1.1, 32.2)
    verdict = _verdict([losing_train])
    assert verdict["finalist"] == "late-bloomer"
    assert verdict["promoted"] is False
    assert verdict["stable_stages"] == []
    assert "train" in verdict["text_fa"] and "0.98" in verdict["text_fa"]


def test_verdict_refuses_to_judge_with_too_few_train_trades():
    verdict = _verdict([_stage("thin", 3.0, 3.0, 10.0, 10.0, trades=5)])
    assert verdict["finalist"] is None and verdict["promoted"] is False
    assert verdict["stable_stages"] == []
    assert "۲۰" in verdict["text_fa"]


def _payload(run_id: str) -> dict:
    return {
        "meta": {
            "run_id": run_id, "generated_at": "2026-10-24T00:00:00+00:00", "market": "crypto",
            "timeframe": "1h", "days": 30, "symbols_requested": 100, "symbols_with_data": 98,
            "source": "gate:spot/candlesticks", "currency": "USDT", "capital_per_symbol": 1000.0,
            "assumed_spread_pct": 0.05, "split_rule": "split", "disclaimer": "past only",
            "live_gate": "paper", "horizon_note": "note",
        },
        "stages": [{
            "id": "baseline-trend", "title": "پایه", "change": "x", "strategy": "trend_v1",
            "horizon": "none", "max_hold_bars": 0, "max_hold_hours": None, "params": {},
            "all": {"trades": 0, "gross_profit": 0.0, "gross_loss": 0.0, "net_pnl": 0.0, "fees_sum": 0.0,
                    "win_rate_pct": None, "profit_factor": None, "median_hold_hours": None,
                    "within_day_pct": None, "exits": {}},
            "train": {"trades": 0, "profit_factor": None, "net_pnl": 0.0},
            "validation": {"trades": 0, "profit_factor": None, "net_pnl": 0.0},
            "top_symbols": [], "bottom_symbols": [], "trades": [], "trades_truncated": 0,
            "skipped": {}, "basket_curve": [],
        }],
        "verdict": {"finalist": None, "promoted": False, "stable_stages": [], "text_fa": "داوری ممکن نیست",
                    "stable_rule": "rule"},
    }


def test_saved_runs_are_listed_and_rendered_without_invented_numbers(config):
    run_id = "crypto-1h-30d-20261024t000000"  # the generator lowercases its timestamp
    payload = _payload(run_id)
    path, report = save_profiles(config, payload)
    assert path.exists() and report.exists()
    text = report.read_text(encoding="utf-8")
    assert "پایه" in text and "past only" in text
    runs = list_runs(config)
    assert [run["run_id"] for run in runs] == [run_id]
    assert load_run(config, runs[0]["run_id"])["stages"][0]["id"] == "baseline-trend"


def test_run_ids_cannot_escape_the_profiles_directory(config):
    assert load_run(config, "../../etc/passwd") is None
    with pytest.raises(ValueError):
        save_profiles(config, _payload("../escape"))


def test_listing_and_loading_are_scoped_to_the_current_market(config):
    """A TSE run in the same folder must never appear on the crypto board."""
    from dataclasses import replace

    crypto_id = "crypto-1h-30d-20261024t000010"
    tse_id = "tse-1d-30d-20261024t000011"
    save_profiles(config, _payload(crypto_id))
    tse_payload = _payload(tse_id)
    tse_payload["meta"]["market"] = "tse"
    save_profiles(config, tse_payload)

    assert [run["run_id"] for run in list_runs(config)] == [crypto_id]
    assert load_run(config, tse_id) is None

    tse_config = replace(config, market="tse")
    assert [run["run_id"] for run in list_runs(tse_config)] == [tse_id]
    assert load_run(tse_config, crypto_id) is None
