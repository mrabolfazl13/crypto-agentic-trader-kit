"""End-to-end research chain on a real (tiny) lake: import → backtest → walk-forward → verdict.

Unit tests elsewhere hand the engine frames and metrics. This one drives the same
functions the tools call, from parquet files on disk through to a persisted verdict,
so a break in the seams between them cannot hide. No network, no real lake.

Synthetic prices are chosen so a trend entry must occur; nothing here is evidence
about real markets — the point of the file is the chain, not the number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cat.quant import engine
from cat.quant.features import QuantParams
from cat.quant.loop import acceptance
from cat.quant.settings import QuantSettings
from cat.quant.settings import StorageBudget as BudgetLimits
from cat.quant.storage import (StorageBudget, empty_manifest, file_sha256, load_manifest, manifest_digest,
                               month_path, save_manifest, summarize_symbol, universe_symbols,
                               validate_coverage, write_json_atomic, write_parquet_atomic)
from cat.quant.store import ResearchStore

MONTH = "2026-01"
MONTH_START = 1767225600  # 2026-01-01T00:00:00Z
SYMBOLS = ("AAAAUSDT", "BBBBUSDT", "CCCCUSDT")
MINUTES = 20 * 24 * 60  # twenty days of 1m bars per symbol


def make_settings(root: Path) -> QuantSettings:
    return QuantSettings(root=root, data_dir=root / "quant", universe_path=root / "quant" / "universe.json",
                         months=(MONTH,), signal_timeframes=("1h",),
                         storage=BudgetLimits(max_total_gb=1.0, raw_gb=1.0, features_gb=1.0,
                                              models_gb=1.0, results_gb=1.0, reserve_gb=0.0))


def trending_bars(seed: int) -> pd.DataFrame:
    """A deterministic up-tape with a weekly dip: enough for trend entries to fire."""
    import math

    closes = []
    for index in range(MINUTES):
        wave = math.sin((index / 1440.0) * 2 * math.pi + seed) * 0.004
        closes.append(100.0 * (1.0 + index * 0.000_010) * (1.0 + wave))
    return pd.DataFrame({
        "ts": [MONTH_START + 60 * index for index in range(MINUTES)],
        "open": closes, "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
        "close": closes, "volume": [10.0] * MINUTES,
    })


def import_lake(settings: QuantSettings) -> dict:
    """Write the parquet months, the universe and the manifest the way ingest does."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    budget = StorageBudget(settings)
    manifest = empty_manifest(settings)
    for offset, symbol in enumerate(SYMBOLS):
        frame = trending_bars(offset)
        path = month_path(settings, symbol, MONTH)
        size = write_parquet_atomic(frame, path, budget, category="raw")
        coverage = validate_coverage(frame, MONTH)
        assert coverage["gaps"] == 0 and coverage["missing_minutes"] == 0, coverage
        record = {"symbol": symbol,
                  "months": {MONTH: {**coverage, "bytes": size, "expected_minutes": MINUTES,
                                     "sha256": file_sha256(path)}}}
        manifest["symbols"][symbol] = summarize_symbol(record)
    save_manifest(settings, manifest)
    write_json_atomic(settings.universe_path, {
        "schema_version": 1,
        "selection": {"months": [MONTH]},
        "entries": [{"binance_symbol": symbol} for symbol in SYMBOLS],
    })
    return manifest


@pytest.fixture()
def lake(tmp_path: Path):
    settings = make_settings(tmp_path)
    manifest = import_lake(settings)
    return settings, manifest


def test_chain_reads_back_the_lake_it_imported(lake):
    settings, manifest = lake
    assert universe_symbols(settings) == list(SYMBOLS)
    window = engine.data_window(settings, universe_symbols(settings))
    assert window.end_ts - window.start_ts == pytest.approx(MINUTES * 60, rel=1e-6)
    # the manifest the tools stamp is content-addressed, so a rewrite is detectable
    assert manifest["digest"] == manifest_digest(load_manifest(settings))
    assert manifest["totals"]["rows"] == MINUTES * len(SYMBOLS)


def test_chain_backtest_produces_priced_trades_and_diagnostics(lake):
    settings, _ = lake
    result = engine.run_backtest(settings, list(SYMBOLS), strategy="ema_trend", timeframe="1h",
                                 params=QuantParams(), sim=engine.simulate_settings(settings),
                                 start_ts=MONTH_START, end_ts=MONTH_START + MINUTES * 60)
    assert result["errors"] == {}
    assert result["metrics"]["trades"] > 0, "a rising tape must produce trend entries"
    assert result["regime"]["enabled"] is False
    trade = result["trades"][0]
    for key in ("entry_ts", "exit_ts", "entry", "exit", "gross", "net", "fees", "reason"):
        assert key in trade, f"trade record lost {key}"
    diagnostics = result["metrics"]["signal_diagnostics"]
    assert diagnostics["funnel"]["momentum_above_threshold"] == diagnostics["entry_signals"]
    assert result["benchmark"]["return_pct"] > 0, "equal-weight B&H of an up-tape is positive"
    assert result["data_version"]["manifest"] == manifest_digest(load_manifest(settings))
    assert result["data_version"]["rows"] == MINUTES * len(SYMBOLS)


def test_chain_window_start_blocks_entries_although_warmup_runs_before_it(lake):
    """``--start`` must never let a warm-up bar open a trade.

    The third-window measurements pass a start date into a lake that begins weeks
    earlier, so the warm-up region is loaded on purpose; an entry inside it would be
    a look-ahead that flatters every number in D-021.
    """
    settings, _ = lake
    sim = engine.simulate_settings(settings)
    mid = MONTH_START + (MINUTES // 2) * 60
    kwargs = dict(strategy="ema_trend", timeframe="1h", params=QuantParams(), sim=sim)
    full = engine.run_backtest(settings, list(SYMBOLS), end_ts=MONTH_START + MINUTES * 60,
                               start_ts=MONTH_START, **kwargs)
    late = engine.run_backtest(settings, list(SYMBOLS), end_ts=MONTH_START + MINUTES * 60,
                               start_ts=mid, **kwargs)
    assert full["errors"] == {} and late["errors"] == {}
    assert late["metrics"]["trades"] > 0, "a truncated rising tape must still enter"
    assert late["metrics"]["trades"] < full["metrics"]["trades"]
    assert all(trade["entry_ts"] >= mid for trade in late["trades"])
    assert all(trade["entry_ts"] < MONTH_START + MINUTES * 60 for trade in late["trades"])


def test_chain_walk_forward_folds_are_chronological_and_stop_before_the_holdout(lake):
    settings, _ = lake
    window = engine.data_window(settings, list(SYMBOLS))
    splits = engine.chronological_splits(window, train=0.5, validation=0.3)
    guarded = engine.Window(window.start_ts, splits["test"].start_ts)
    forward = engine.walk_forward(settings, list(SYMBOLS), strategy="ema_trend", timeframe="1h",
                                  params=QuantParams(), folds=2, test_days=3, window=guarded)
    assert len(forward["folds"]) == 2
    holdout_start = pd.Timestamp(guarded.end_ts, unit="s", tz="UTC")
    previous_test_end = None
    for fold in forward["folds"]:
        train_end = pd.Timestamp(fold["train"]["end"])
        test_start = pd.Timestamp(fold["test"]["start"])
        test_end = pd.Timestamp(fold["test"]["end"])
        assert train_end == test_start, "train must run right up to the slice it predicts"
        assert test_end <= holdout_start, "no fold may reach into the final holdout"
        if previous_test_end is not None:
            assert test_start >= previous_test_end, "fold test slices must not overlap"
        previous_test_end = test_end
    assert forward["aggregate"]["folds"] == 2


def test_chain_verdict_is_persisted_and_reproducible_end_to_end(lake):
    """The same lake twice must give the same verdict — the reproducibility claim of the mission."""
    settings, _ = lake
    window = engine.data_window(settings, list(SYMBOLS))
    splits = engine.chronological_splits(window, train=0.5, validation=0.3)
    params = QuantParams()

    def run_once() -> dict:
        validation = engine.run_backtest(settings, list(SYMBOLS), strategy="ema_trend", timeframe="1h",
                                         params=params, sim=engine.simulate_settings(settings),
                                         start_ts=splits["validation"].start_ts,
                                         end_ts=splits["validation"].end_ts)
        forward = engine.walk_forward(settings, list(SYMBOLS), strategy="ema_trend", timeframe="1h",
                                      params=params, folds=2, test_days=3,
                                      window=engine.Window(window.start_ts, splits["test"].start_ts))
        verdict = acceptance(validation["metrics"], forward["aggregate"], None, settings)
        return {"metrics": validation["metrics"], "aggregate": forward["aggregate"], "verdict": verdict}

    first, second = run_once(), run_once()
    assert first["metrics"] == second["metrics"], "the engine is not deterministic"
    assert first["verdict"]["decision"] == second["verdict"]["decision"]

    store = ResearchStore(settings.database_path)
    loop_id = store.start_loop({"reason": "chain test"})
    experiment_id = store.create_experiment(loop_id=loop_id, seq=1, hypothesis="chain smoke",
                                            strategy="ema_trend", timeframe="1h", params=params.as_dict(),
                                            code_version="test", data_version={"digest": "x"})
    store.finish_experiment(experiment_id, status="completed", trials=1,
                            metrics=first["metrics"], validation=first["metrics"],
                            walk_forward=first["aggregate"],
                            acceptance=first["verdict"]["decision"], reason=first["verdict"]["reason"])
    stored = store.get_experiment(experiment_id)
    assert stored["acceptance"] == first["verdict"]["decision"]
    assert stored["validation"]["trades"] == first["metrics"]["trades"], "validation must round-trip"
    store.close()

    report = settings.results_dir / "chain_report.json"
    write_json_atomic(report, {"decision": first["verdict"]["decision"], "reason": first["verdict"]["reason"],
                               "gates": first["verdict"]["gates"], "params": params.as_dict(),
                               "metrics": first["metrics"]})
    reloaded = json.loads(report.read_text(encoding="utf-8"))
    assert reloaded["decision"] == first["verdict"]["decision"]
    assert reloaded["gates"].keys() == first["verdict"]["gates"].keys()


def test_chain_gate_verdict_follows_its_own_gates_not_the_narrative(tmp_path: Path):
    """Hand the acceptance function numbers that must pass, then numbers that must not."""
    settings = make_settings(tmp_path)
    passing_validation = {"net_return_pct": 4.0, "effective_drawdown_pct": 2.0,
                          "benchmark_return_pct": 1.0, "trades": 40}
    passing_aggregate = {"trades": 40, "profit_factor_mean": 1.5, "max_drawdown_pct": 3.0,
                         "max_effective_drawdown_pct": 3.0, "positive_fold_pct": 75.0,
                         "net_return_pct_sum": 8.0, "benchmark_return_pct_sum": 2.0, "folds": 4}
    verdict = acceptance(passing_validation, passing_aggregate, None, settings)
    assert verdict["decision"] == "accepted", verdict["reason"]
    assert all(gate["pass"] for gate in verdict["gates"].values()), verdict

    thin = dict(passing_aggregate, positive_fold_pct=10.0)
    rejected = acceptance(passing_validation, thin, None, settings)
    assert rejected["decision"] == "rejected"
    assert "positive_folds_pct" in rejected["reason"]


# ------------------------------------------------ D-016 train-only symbol breadth selection

def _wf_tool():
    import sys

    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import quant_nested_walk_forward as wf
    return wf


def test_chain_breadth_selection_reads_only_the_slice_it_is_given(monkeypatch, lake):
    """A fold may pick its basket from an earlier slice, never from the slice that scores it."""
    wf = _wf_tool()
    settings, _ = lake
    seen: dict[str, int] = {}

    def fake_backtest(_settings, symbols, **kwargs):
        seen["start_ts"] = kwargs["start_ts"]
        seen["end_ts"] = kwargs["end_ts"]
        return {"per_symbol": [{"symbol": "BBBBUSDT", "candidates": 9, "entry_signals": 40},
                               {"symbol": "AAAAUSDT", "candidates": 3, "entry_signals": 12},
                               {"symbol": "CCCCUSDT", "candidates": 0, "entry_signals": 7}],
                "errors": {}}

    monkeypatch.setattr(wf, "run_backtest", fake_backtest)
    slice_window = engine.Window(MONTH_START + 86400, MONTH_START + 3 * 86400)
    subset = wf.breadth_ranking(settings, list(SYMBOLS), strategy="ema_trend", timeframe="1h",
                                sim=engine.simulate_settings(settings), slice_window=slice_window,
                                top_k=2, min_candidates=1)
    assert (seen["start_ts"], seen["end_ts"]) == (slice_window.start_ts, slice_window.end_ts)
    assert subset["symbols"] == ["AAAAUSDT", "BBBBUSDT"], "top_k must carry the guard-passing basket"
    assert subset["counts"] == {"BBBBUSDT": 9, "AAAAUSDT": 3}
    assert subset["kept"] == 2 and subset["requested"] == len(SYMBOLS)


def test_chain_breadth_floor_leaves_a_symbol_without_candidates_out(monkeypatch, lake):
    wf = _wf_tool()
    settings, _ = lake

    def fake_backtest(_settings, symbols, **kwargs):
        return {"per_symbol": [{"symbol": symbol, "candidates": 0, "entry_signals": 1}
                               for symbol in symbols], "errors": {}}

    monkeypatch.setattr(wf, "run_backtest", fake_backtest)
    subset = wf.breadth_ranking(settings, list(SYMBOLS), strategy="ema_trend", timeframe="1h",
                                sim=engine.simulate_settings(settings),
                                slice_window=engine.Window(MONTH_START, MONTH_START + 86400),
                                top_k=25, min_candidates=1)
    assert subset["kept"] == 0, "a symbol with no guard-passing candidate has no breadth"


def test_chain_thin_breadth_falls_back_to_the_pre_registered_protocol(monkeypatch, lake):
    """Pre-registered in D-016: a thin basket falls back, and the fallback may not widen the search."""
    wf = _wf_tool()
    settings, _ = lake
    baskets: list[int] = []
    search_baskets: list[int] = []

    monkeypatch.setattr(wf, "breadth_ranking", lambda *a, **k: {
        "rule": "train_breadth", "selection_slice": {"start": "x", "end": "y"}, "top_k": 25,
        "min_candidates": 1, "requested": len(SYMBOLS), "kept": 2, "symbols": list(SYMBOLS[:2]),
        "counts": {}, "probe_errors": {}, "elapsed_seconds": 0.0})
    monkeypatch.setattr(wf, "optimize_windows", lambda _s, symbols, **kw: {
        "best": {"params": QuantParams().as_dict()}, "chosen_on": "inner selection",
        "finalists": 1, "trials": 1, "stop_reason": "done", "elapsed_seconds": 0.0,
        "_captured": search_baskets.append(len(symbols))})
    monkeypatch.setattr(wf, "simulate_settings", engine.simulate_settings)

    def fake_backtest(_settings, symbols, **kwargs):
        baskets.append(len(symbols))
        return {"metrics": {"trades": 0}, "benchmark": {}, "trades": [],
                "symbols_evaluated": len(symbols), "errors": {},
                "sim": {"max_hold_hours": kwargs["sim"].max_hold_hours}}

    monkeypatch.setattr(wf, "run_backtest", fake_backtest)
    plan = {"fold": 1, "train": engine.Window(MONTH_START, MONTH_START + 10 * 86400),
            "test": engine.Window(MONTH_START + 10 * 86400, MONTH_START + 13 * 86400)}
    fold = wf.evaluate_fold(settings, list(SYMBOLS[:2]), list(SYMBOLS), strategy="ema_trend",
                            timeframe="1h", plan=plan, trials=1, finalists=1,
                            inner_selection_fraction=0.2, seed=1,
                            sim=engine.simulate_settings(settings),
                            symbol_selection="train_breadth")
    assert fold["symbol_subset"]["fell_back"].startswith("only 2")
    assert search_baskets == [2], "the fallback must keep the pre-registered search slice"
    assert baskets == [len(SYMBOLS)], "the scored fold keeps the whole universe"
    assert fold["search_basket"] == 2 and fold["scored_basket"] == len(SYMBOLS)
    assert fold["symbols_evaluated"] == len(SYMBOLS)


def test_chain_usable_breadth_does_narrow_the_fold_basket(monkeypatch, lake):
    """The other half of D-016: with three or more usable symbols the subset is actually applied."""
    wf = _wf_tool()
    settings, _ = lake
    basket = list(SYMBOLS) + ["DDDDUSDT"]
    kept = list(SYMBOLS)
    baskets: list[list[str]] = []

    monkeypatch.setattr(wf, "breadth_ranking", lambda *a, **k: {
        "rule": "train_breadth", "selection_slice": {"start": "x", "end": "y"}, "top_k": 25,
        "min_candidates": 1, "requested": len(basket), "kept": len(kept), "symbols": kept,
        "counts": {}, "probe_errors": {}, "elapsed_seconds": 0.0})
    monkeypatch.setattr(wf, "optimize_windows", lambda _s, symbols, **kw: {
        "best": {"params": QuantParams().as_dict()}, "chosen_on": "inner selection",
        "finalists": 1, "trials": 1, "stop_reason": "done", "elapsed_seconds": 0.0,
        "_captured": baskets.append(list(symbols))})
    monkeypatch.setattr(wf, "simulate_settings", engine.simulate_settings)

    def fake_backtest(_settings, symbols, **kwargs):
        baskets.append(list(symbols))
        return {"metrics": {"trades": 0}, "benchmark": {}, "trades": [],
                "symbols_evaluated": len(symbols), "errors": {},
                "sim": {"max_hold_hours": kwargs["sim"].max_hold_hours}}

    monkeypatch.setattr(wf, "run_backtest", fake_backtest)
    plan = {"fold": 1, "train": engine.Window(MONTH_START, MONTH_START + 10 * 86400),
            "test": engine.Window(MONTH_START + 10 * 86400, MONTH_START + 13 * 86400)}
    fold = wf.evaluate_fold(settings, list(basket), list(basket), strategy="ema_trend",
                            timeframe="1h", plan=plan, trials=1, finalists=1,
                            inner_selection_fraction=0.2, seed=1,
                            sim=engine.simulate_settings(settings),
                            symbol_selection="train_breadth")
    assert "fell_back" not in fold["symbol_subset"]
    assert baskets == [kept, kept], "search and test scoring must run on the same selected basket"
    assert fold["search_basket"] == len(kept) and fold["scored_basket"] == len(kept)
    assert fold["symbols_evaluated"] == len(kept)


def test_chain_fold_records_the_hold_cap_it_was_actually_scored_on(monkeypatch, lake):
    """D-034/F-041: a run that overrides the exit cap must not be able to claim a cap it never used."""
    wf = _wf_tool()
    settings, _ = lake
    seen: list[float] = []

    monkeypatch.setattr(wf, "optimize_windows", lambda _s, symbols, **kw: {
        "best": {"params": QuantParams().as_dict()}, "chosen_on": "inner selection",
        "finalists": 1, "trials": 1, "stop_reason": "done", "elapsed_seconds": 0.0})

    def fake_backtest(_settings, symbols, **kwargs):
        seen.append(kwargs["sim"].max_hold_hours)
        return {"metrics": {"trades": 0}, "benchmark": {}, "trades": [],
                "symbols_evaluated": len(symbols), "errors": {},
                "sim": {"max_hold_hours": kwargs["sim"].max_hold_hours}}

    monkeypatch.setattr(wf, "run_backtest", fake_backtest)
    plan = {"fold": 1, "train": engine.Window(MONTH_START, MONTH_START + 10 * 86400),
            "test": engine.Window(MONTH_START + 10 * 86400, MONTH_START + 13 * 86400)}
    overridden = engine.simulate_settings(settings, max_hold_hours=720.0)
    fold = wf.evaluate_fold(settings, list(SYMBOLS), list(SYMBOLS), strategy="ema_trend",
                            timeframe="1h", plan=plan, trials=1, finalists=1,
                            inner_selection_fraction=0.2, seed=1, sim=overridden)
    assert settings.backtest.max_hold_hours != 720.0, "the config cap must stay the untouched control"
    assert seen and set(seen) == {720.0}, "the scored fold must execute under the override"
    assert fold["max_hold_hours"] == 720.0
