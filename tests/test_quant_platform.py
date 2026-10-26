"""Budget, gate, checkpoint and API tests for the quant research platform.

Nothing here needs the data lake or an exchange: the budget is checked against
files on disk, the gates against hand-written metrics, and the API against an
empty workspace. Optional research dependencies are monkeypatched, so the suite
stays runnable in the base install.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The research stack is optional at runtime (requirements-quant.txt). Without this
# guard a half-installed environment dies during collection and hides the whole
# base suite; CI still installs both requirement files and asserts the imports in
# an explicit step, so a real regression cannot pass unnoticed there.
pytest.importorskip("optuna", reason="quant research stack missing: pip install -r requirements-quant.txt")

import cat.api as api_module  # noqa: E402  (must follow the importorskip guard)
from cat.quant import service as quant_service
from cat.quant.engine import Window, walk_forward_windows
from cat.quant.loop import acceptance, analyze_weakness
from cat.quant.settings import LoopSettings, QuantSettings, StorageBudget
from cat.quant.storage import StorageBudgetExceeded, StorageError, storage_report
from cat.quant.store import ResearchStore

BASE = "/api/v1"


@pytest.fixture()
def client(workspace: Path) -> TestClient:
    return TestClient(api_module.create_app(root=workspace))


def quant_settings(root: Path, **overrides) -> QuantSettings:
    return QuantSettings(root=root, data_dir=root / "state" / "quant", **overrides)


def small_budget(root: Path, *, total_gb: float = 0.01, reserve_gb: float = 0.0) -> QuantSettings:
    return quant_settings(root, storage=StorageBudget(
        max_total_gb=total_gb, raw_gb=total_gb, features_gb=total_gb, models_gb=total_gb,
        results_gb=total_gb, reserve_gb=reserve_gb))


def write_bytes(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# --------------------------------------------------------------------- budget

def test_storage_report_counts_each_category_and_the_total(tmp_path: Path):
    settings = quant_settings(tmp_path)
    write_bytes(settings.raw_dir / "BTCUSDT" / "part.parquet", 3 * 1024 * 1024)
    write_bytes(settings.features_dir / "features.parquet", 1024 * 1024)
    report = storage_report(settings)

    assert report["categories"]["raw"]["bytes"] >= 3 * 1024 * 1024
    assert report["categories"]["features"]["bytes"] >= 1024 * 1024
    assert report["total_bytes"] >= 4 * 1024 * 1024
    assert report["within_budget"] is True
    assert 0 < report["used_pct"] < 100
    assert report["policy"].startswith("a breach stops the job")


def test_budget_refuses_a_write_that_would_break_the_total_cap(tmp_path: Path):
    from cat.quant.storage import StorageBudget as Budget

    settings = small_budget(tmp_path, total_gb=0.01)  # ~10.7 MB usable
    write_bytes(settings.raw_dir / "big.parquet", 9 * 1024 * 1024)

    with pytest.raises(StorageBudgetExceeded) as caught:
        Budget(settings).check(4 * 1024 * 1024, "raw", free_margin_bytes=0)
    assert "storage budget would be exceeded" in str(caught.value)
    assert caught.value.report["additional_bytes"] == 4 * 1024 * 1024


def test_budget_refuses_a_write_that_would_break_the_category_cap(tmp_path: Path):
    from cat.quant.storage import StorageBudget as Budget

    settings = quant_settings(tmp_path, storage=StorageBudget(
        max_total_gb=2.5, raw_gb=0.002, features_gb=0.5, models_gb=0.3, results_gb=0.3,
        reserve_gb=0.0))
    write_bytes(settings.raw_dir / "big.parquet", 2 * 1024 * 1024)
    with pytest.raises(StorageBudgetExceeded) as caught:
        Budget(settings).check(1024 * 1024, "raw", free_margin_bytes=0)
    assert caught.value.report["category"] == "raw"


def test_budget_breach_keeps_existing_files_and_rejects_bad_input(tmp_path: Path):
    from cat.quant.storage import StorageBudget as Budget

    settings = small_budget(tmp_path, total_gb=0.01)
    stored = write_bytes(settings.raw_dir / "keep.parquet", 9 * 1024 * 1024)
    budget = Budget(settings)
    with pytest.raises(StorageBudgetExceeded):
        budget.check(4 * 1024 * 1024, "raw", free_margin_bytes=0)
    assert stored.exists() and stored.stat().st_size == 9 * 1024 * 1024
    with pytest.raises(StorageError):
        budget.check(-1, "raw")
    with pytest.raises(StorageError):
        budget.check(1, "nowhere")


@pytest.fixture()
def plenty_of_disk(monkeypatch):
    """Budget tests must not depend on how full the host disk happens to be."""
    monkeypatch.setattr("cat.quant.storage.free_disk_bytes", lambda path: 10 * 1024 ** 3)


def test_reserved_bytes_are_released_when_the_write_ends(tmp_path: Path, plenty_of_disk):
    from cat.quant.storage import StorageBudget as Budget

    settings = small_budget(tmp_path, total_gb=0.01)
    budget = Budget(settings)
    with budget.reserve_for_write(8 * 1024 * 1024, "raw") as reservation:
        assert reservation is not None
        with pytest.raises(StorageBudgetExceeded):
            budget.check(4 * 1024 * 1024, "raw", free_margin_bytes=0)
    budget.check(8 * 1024 * 1024, "raw", free_margin_bytes=0)


# ---------------------------------------------------------- walk-forward plan

def test_walk_forward_folds_are_contiguous_and_never_share_bars():
    window = Window(0, 40 * 86400)
    plans = walk_forward_windows(window, folds=4, test_days=7)
    assert len(plans) == 4
    for index, plan in enumerate(plans):
        assert plan["train"].start_ts == window.start_ts
        assert plan["train"].end_ts == plan["test"].start_ts
        assert plan["test"].end_ts - plan["test"].start_ts == 7 * 86400
        assert plan["fold"] == index + 1
        if index:
            assert plan["test"].start_ts == plans[index - 1]["test"].end_ts
    assert plans[-1]["test"].end_ts == window.end_ts


def test_walk_forward_rejects_a_window_too_short_for_its_folds():
    with pytest.raises(ValueError):
        walk_forward_windows(Window(0, 20 * 86400), folds=4, test_days=7)
    with pytest.raises(ValueError):
        walk_forward_windows(Window(0, 40 * 86400), folds=1, test_days=7)
    with pytest.raises(ValueError):
        walk_forward_windows(Window(0, 40 * 86400), folds=4, test_days=0)


# ------------------------------------------------------------ acceptance gates

PASSING = {
    "validation": {"net_return_pct": 2.0, "effective_drawdown_pct": 5.0,
                   "benchmark_return_pct": 1.0},
    "aggregate": {"trades": 40, "profit_factor_mean": 1.3, "positive_fold_pct": 75.0,
                  "max_drawdown_pct": 6.0, "net_return_pct_sum": 8.0,
                  "benchmark_return_pct_sum": 5.0},
}


def gates(**changes):
    validation = dict(PASSING["validation"])
    aggregate = dict(PASSING["aggregate"])
    for key, value in changes.items():
        (validation if key in validation else aggregate)[key] = value
    return validation, aggregate


def test_acceptance_passes_only_when_every_gate_and_the_champion_are_cleared():
    validation, aggregate = gates()
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["decision"] == "accepted"
    assert all(gate["pass"] for gate in result["gates"].values())

    champion = {"experiment_id": "exp-1", "validation_net_pct": 1.0}
    assert acceptance(validation, aggregate, champion,
                      quant_settings(Path(".")))["decision"] == "accepted"


@pytest.mark.parametrize("change,gate", [
    ({"trades": 10}, "min_trades"),
    ({"profit_factor_mean": 1.0}, "profit_factor"),
    ({"max_drawdown_pct": 12.0}, "max_drawdown_pct"),
    ({"positive_fold_pct": 50.0}, "positive_folds_pct"),
    ({"net_return_pct": -0.5}, "positive_validation_net"),
])
def test_each_gate_rejects_on_its_own(change, gate: str):
    validation, aggregate = gates(**change)
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["decision"] == "rejected"
    assert gate in result["reason"]
    assert result["gates"][gate]["pass"] is False


def test_drawdown_gate_uses_the_worse_of_folds_and_validation():
    validation, aggregate = gates(effective_drawdown_pct=11.0)
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["gates"]["max_drawdown_pct"]["actual"] == 11.0
    assert result["decision"] == "rejected"


def test_drawdown_gate_also_sees_the_fold_effective_drawdown():
    validation, aggregate = gates()
    aggregate["max_effective_drawdown_pct"] = 14.0
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["gates"]["max_drawdown_pct"]["actual"] == 14.0
    assert result["decision"] == "rejected"


def test_champion_must_be_beaten_by_the_configured_margin():
    validation, aggregate = gates(net_return_pct=2.94)
    champion = {"experiment_id": "exp-1", "validation_net_pct": 2.90}
    result = acceptance(validation, aggregate, champion, quant_settings(Path(".")))
    assert result["gates"]["beats_champion"]["pass"] is False
    assert "beats_champion" in result["reason"]

    validation, aggregate = gates(net_return_pct=3.10)
    result = acceptance(validation, aggregate, champion, quant_settings(Path(".")))
    assert result["gates"]["beats_champion"]["pass"] is True


def test_benchmark_gate_rejects_a_candidate_that_only_matches_buy_and_hold():
    validation, aggregate = gates(benchmark_return_pct=2.0)
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["gates"]["beats_validation_benchmark"]["pass"] is False
    assert result["gates"]["beats_validation_benchmark"]["actual"] == 0.0
    assert "beats_validation_benchmark" in result["reason"]

    validation, aggregate = gates(benchmark_return_pct_sum=8.0)
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["gates"]["beats_fold_benchmark"]["pass"] is False
    assert "beats_fold_benchmark" in result["reason"]


def test_benchmark_gate_requires_the_configured_margin():
    validation, aggregate = gates(benchmark_return_pct=1.96)  # excess +0.04 < 0.05
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["gates"]["beats_validation_benchmark"]["actual"] == 0.04
    assert result["gates"]["beats_validation_benchmark"]["pass"] is False


def test_benchmark_gate_fails_closed_when_the_benchmark_is_missing():
    validation, aggregate = gates()
    del validation["benchmark_return_pct"]
    del aggregate["benchmark_return_pct_sum"]
    result = acceptance(validation, aggregate, None, quant_settings(Path(".")))
    assert result["gates"]["beats_validation_benchmark"]["pass"] is False
    assert result["gates"]["beats_fold_benchmark"]["pass"] is False
    assert result["decision"] == "rejected"


def test_benchmark_gate_can_be_disabled_only_by_explicit_settings():
    validation, aggregate = gates()
    del validation["benchmark_return_pct"]
    del aggregate["benchmark_return_pct_sum"]
    legacy = quant_settings(Path("."), loop=LoopSettings(require_benchmark_edge=False))
    result = acceptance(validation, aggregate, None, legacy)
    assert result["gates"]["beats_validation_benchmark"]["required"] == "disabled"
    assert result["decision"] == "accepted"


def test_loop_limits_come_from_settings_not_from_the_engine():
    strict = quant_settings(Path("."), loop=LoopSettings(min_trades=100))
    validation, aggregate = gates()
    result = acceptance(validation, aggregate, None, strict)
    assert result["gates"]["min_trades"]["required"] == 100
    assert result["decision"] == "rejected"


def test_weakness_names_a_signal_drought_instead_of_a_vague_zero():
    finding = analyze_weakness({"validation": {"trades": 0, "signal_diagnostics": {
        "ready_bars": 900, "entry_signals": 0, "funnel": {"ready": 900, "trend": 0}}},
        "walk_forward": {"aggregate": {"trades": 0}}})
    assert finding[0]["finding"] == "no_entry_signal"
    assert "trend" in finding[0]["evidence"]


def test_weakness_names_execution_rejections_when_signals_exist():
    finding = analyze_weakness({"validation": {"trades": 0, "rejections": {"max_open_positions": 4},
                                               "signal_diagnostics": {"ready_bars": 900,
                                                                      "entry_signals": 25}},
                                "walk_forward": {"aggregate": {"trades": 0}}})
    assert finding[0]["finding"] == "signals_rejected_before_fill"
    assert "max_open_positions" in finding[0]["evidence"]


# ------------------------------------------------------------------- checkpoints

def test_store_checkpoint_round_trip_and_experiment_lifecycle(tmp_path: Path):
    store = ResearchStore(tmp_path / "research.sqlite3")
    try:
        loop_id = store.start_loop({"plan": []})
        store.update_loop(loop_id, message="experiment 1 running", experiments=1)
        store.set_checkpoint(f"loop:{loop_id}", {"next_seq": 1, "plan": [{"key": "a"}]})
        assert store.get_checkpoint(f"loop:{loop_id}")["next_seq"] == 1

        experiment_id = store.create_experiment(
            loop_id=loop_id, seq=0, hypothesis="h", strategy="ema_trend", timeframe="1h", params={})
        assert store.get_experiment(experiment_id)["status"] == "running"
        store.mark_interrupted(loop_id)
        assert store.get_experiment(experiment_id)["status"] == "interrupted"

        store.finish_experiment(experiment_id, status="completed", trials=4,
                                metrics={"net_return_pct": 1.0}, acceptance="accepted",
                                reason="ok", duration_s=12.5, params={"ema_fast": 10})
        stored = store.get_experiment(experiment_id)
        assert stored["status"] == "completed"
        assert stored["metrics"]["net_return_pct"] == 1.0
        assert stored["params"]["ema_fast"] == 10

        store.set_champion({"experiment_id": experiment_id, "validation_net_pct": 1.0})
        assert store.get_champion()["experiment_id"] == experiment_id
        counts = store.counts()
        assert counts["loop_runs"] == 1 and counts["experiments"] == 1

        latest = store.latest_loop()
        assert latest["status"] == "running"
        store.update_loop(loop_id, status="stopped", experiments=1)
        assert store.latest_loop()["status"] == "stopped"
        assert store.latest_loop()["ended_at"]
    finally:
        store.close()


def test_store_counts_a_completed_experiment_key_for_resume(tmp_path: Path):
    store = ResearchStore(tmp_path / "research.sqlite3")
    try:
        loop_id = store.start_loop({})
        experiment_id = store.create_experiment(loop_id=loop_id, seq=0, hypothesis="h",
                                                strategy="ema_trend", timeframe="1h", params={"a": 1})
        store.finish_experiment(experiment_id, status="completed", params={"a": 1})
        keys = store.completed_keys(loop_id)
        assert len(keys) == 1
        assert "h|ema_trend|1h|" in next(iter(keys))
    finally:
        store.close()


# ------------------------------------------------------------------------- API

def test_status_endpoint_is_paper_only_and_reads_an_empty_lake(client: TestClient):
    response = client.get(f"{BASE}/quant/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "PAPER"
    assert payload["live_trading_enabled"] is False
    assert payload["enabled"] is True
    assert payload["data"]["rows"] == 0
    assert payload["data"]["coverage_pct"] in (0, 0.0, None)
    assert payload["storage"]["within_budget"] is True
    assert payload["gates"]["min_trades"] > 0
    assert payload["loop"]["running"] is False
    assert payload["counts"]["experiments"] == 0


def test_status_endpoint_reports_a_disabled_engine_instead_of_failing(client: TestClient,
                                                                      workspace: Path,
                                                                      monkeypatch):
    monkeypatch.setattr(quant_service, "_settings",
                        lambda root: (_ for _ in ()).throw(quant_service.QuantDisabled(
                            "quant.enabled is false in config.yaml")))
    payload = client.get(f"{BASE}/quant/status").json()
    assert payload["enabled"] is False
    assert "quant.enabled" in payload["reason"]
    assert payload["mode"] == "PAPER"


def test_backtest_without_optional_dependencies_returns_503(client: TestClient, monkeypatch):
    def missing(_modules):
        raise quant_service.QuantUnavailable(["pyarrow"])

    monkeypatch.setattr(quant_service, "_require", missing)
    response = client.post(f"{BASE}/quant/backtest", json={"strategy": "ema_trend"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "quant_dependencies_missing"
    assert "pyarrow" in detail["missing"]
    assert "requirements-quant.txt" in detail["hint"]


def test_backtest_rejects_an_unknown_strategy_with_400(client: TestClient, monkeypatch):
    monkeypatch.setattr(quant_service, "_require", lambda _modules: None)
    response = client.post(f"{BASE}/quant/backtest", json={"strategy": "not_a_strategy"})
    assert response.status_code == 400
    assert "unknown strategy" in response.json()["detail"]


def test_backtest_body_is_bounded_by_the_api_model(client: TestClient):
    response = client.post(f"{BASE}/quant/backtest",
                           json={"strategy": "ema_trend", "position_pct": 80})
    assert response.status_code == 422


def test_loop_start_is_refused_while_a_process_is_alive(client: TestClient, workspace: Path):
    pid_file = workspace / "state" / "quant" / "loop.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-10-26T00:00:00Z"}),
                        encoding="utf-8")
    response = client.post(f"{BASE}/quant/loop/start", json={})
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_loop_start_reports_a_broken_interpreter_as_503(client: TestClient, workspace: Path,
                                                        monkeypatch):
    monkeypatch.setattr(quant_service, "quant_python", lambda root: "C:/fake/python.exe")
    monkeypatch.setattr(quant_service, "interpreter_probe",
                        lambda python: {"python": python, "ok": False, "missing": ["optuna"],
                                        "error": None})
    response = client.post(f"{BASE}/quant/loop/start", json={})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "quant_dependencies_missing"
    assert any("optuna" in item for item in detail["missing"])


def test_loop_stop_without_a_process_is_a_no_op(client: TestClient, workspace: Path):
    pid_file = workspace / "state" / "quant" / "loop.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(json.dumps({"pid": 999_999_999}), encoding="utf-8")
    payload = client.post(f"{BASE}/quant/loop/stop").json()
    assert payload["stopped"] is False
    assert payload["reason"] == "no loop process is running"
    assert not pid_file.exists()


def test_experiments_endpoint_returns_an_empty_page_before_any_run(client: TestClient):
    payload = client.get(f"{BASE}/quant/experiments").json()
    assert payload["items"] == []
    assert payload["limit"] == 50


# ---------------------------------------------------- selection-bias regressions

def test_optimizer_scores_trials_on_train_only(tmp_path: Path, monkeypatch):
    import cat.quant.optimizer as optimizer_module

    train_window = Window(0, 600_000)
    validation_window = Window(600_000, 800_000)
    monkeypatch.setattr(optimizer_module, "data_window", lambda settings, symbols: Window(0, 1_000_000))
    monkeypatch.setattr(optimizer_module, "chronological_splits",
                        lambda window, train, validation: {"train": train_window,
                                                           "validation": validation_window,
                                                           "test": Window(800_000, 1_000_000)})
    calls: list[tuple[int, int]] = []

    def fake_run_backtest(settings, symbols, *, strategy, timeframe, params, sim,
                          start_ts, end_ts):
        calls.append((start_ts, end_ts))
        return {"metrics": {"trades": 50, "net_return_pct": 1.0, "effective_drawdown_pct": 2.0}}

    monkeypatch.setattr(optimizer_module, "run_backtest", fake_run_backtest)
    result = optimizer_module.optimize(quant_settings(tmp_path), ["TESTUSDT"], strategy="breakout",
                                       timeframe="1h", trials=3)
    assert calls, "the search must have scored at least one trial"
    assert set(calls) == {(train_window.start_ts, train_window.end_ts)}, \
        "validation must never be read by the search"
    assert result["note"].startswith("trials ranked on the train window only")


def test_nested_optimizer_chooses_finalists_on_the_inner_selection_window(tmp_path: Path, monkeypatch):
    import cat.quant.optimizer as optimizer_module

    search = Window(0, 600_000)
    selection = Window(600_000, 700_000)
    calls: list[tuple[int, int]] = []

    def fake_run_backtest(settings, symbols, *, strategy, timeframe, params, sim,
                          start_ts, end_ts):
        calls.append((start_ts, end_ts))
        return {"metrics": {"trades": 50, "net_return_pct": 1.0, "effective_drawdown_pct": 2.0}}

    monkeypatch.setattr(optimizer_module, "run_backtest", fake_run_backtest)
    result = optimizer_module.optimize_windows(
        quant_settings(tmp_path), ["TESTUSDT"], strategy="breakout", timeframe="1h",
        search_window=search, selection_window=selection,
        allowed_window=Window(0, 700_000), trials=3, finalists=2)
    assert set(calls) == {(search.start_ts, search.end_ts),
                          (selection.start_ts, selection.end_ts)}, \
        "only the search and inner selection windows may be scored"
    assert calls.count((selection.start_ts, selection.end_ts)) == 2, \
        "only the finalists are scored on the inner selection window"
    assert result["chosen_on"] == "inner selection"
    assert result["best"]["selection_score"] == pytest.approx(0.5)
    assert "outer fold test was never read" in result["note"]


def test_nested_optimizer_refuses_a_window_that_leaves_the_fold_train_slice(tmp_path: Path):
    import cat.quant.optimizer as optimizer_module

    with pytest.raises(ValueError, match="outside the allowed training window"):
        optimizer_module.optimize_windows(
            quant_settings(tmp_path), ["TESTUSDT"], strategy="breakout", timeframe="1h",
            search_window=Window(0, 600_000), selection_window=Window(650_000, 800_000),
            allowed_window=Window(0, 700_000), trials=1)
    with pytest.raises(ValueError, match="must start after the search window"):
        optimizer_module.optimize_windows(
            quant_settings(tmp_path), ["TESTUSDT"], strategy="breakout", timeframe="1h",
            search_window=Window(0, 600_000), selection_window=Window(500_000, 700_000),
            allowed_window=Window(0, 700_000), trials=1)


def test_ml_threshold_is_chosen_before_the_outer_validation_is_scored(tmp_path: Path, monkeypatch):
    import cat.quant.ml as ml_module

    train = Window(0, 80 * 86400)
    validation = Window(80 * 86400, 100 * 86400)
    test = Window(100 * 86400, 120 * 86400)
    built: list[Window] = []
    threshold_windows: list[Window] = []
    scored: list[tuple[int, int]] = []

    monkeypatch.setattr(ml_module, "build_training_set",
                        lambda settings, symbols, *, timeframe, params, window, horizon_hours=None,
                        progress=None: (built.append(window), {"label": [0, 1]})[1])
    monkeypatch.setattr(ml_module, "train_model",
                        lambda train_rows, validation_rows, *, seed=42, horizon_bars=4:
                        {"model": object(), "metrics": {}})
    monkeypatch.setattr(ml_module, "choose_threshold",
                        lambda settings, symbols, *, timeframe, params, model, thresholds, window,
                        sim=None: (threshold_windows.append(window),
                                   {"best": {"threshold": 0.55}, "thresholds": []})[1])
    monkeypatch.setattr(ml_module, "ml_backtest",
                        lambda settings, symbols, *, timeframe, params, model, threshold, sim=None,
                        start_ts=None, end_ts=None, horizon_hours=None, progress=None:
                        (scored.append((start_ts, end_ts)),
                         {"metrics": {"trades": 0}, "trades": [], "benchmark": {}, "errors": {}})[1])
    monkeypatch.setattr(ml_module, "walk_forward_ml",
                        lambda *args, **kwargs: {"kind": "ml_lgbm", "folds": [],
                                                 "aggregate": {}})

    result = ml_module.run_ml_walk_forward(quant_settings(tmp_path), ["TESTUSDT"], ["TESTUSDT"],
                                           timeframe="1h", train=train, validation=validation,
                                           test=test)
    assert len(built) == 2
    assert built[0].start_ts == train.start_ts and built[1].end_ts == train.end_ts
    assert all(window.end_ts <= train.end_ts for window in built)
    assert threshold_windows and threshold_windows[0].end_ts <= train.end_ts, \
        "the threshold must be picked inside train, never on the outer validation slice"
    assert scored == [(validation.start_ts, validation.end_ts), (test.start_ts, test.end_ts)]
    assert result["selected_threshold"] == 0.55


# ------------------------------------------------------------ benchmark & funnel

def _bars_1m(count: int, *, start: int = 0, base: float = 100.0, drift: float = 0.001):
    import pandas as pd

    closes = [base + index * drift for index in range(count)]
    return pd.DataFrame({"ts": [start + 60 * index for index in range(count)],
                         "open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": [1.0] * count})


def test_benchmark_ignores_indicator_warmup_before_the_evaluated_window(tmp_path: Path,
                                                                       monkeypatch):
    import cat.quant.engine as engine

    frame = _bars_1m(400)
    monkeypatch.setattr(engine, "load_bars", lambda *args, **kwargs: frame)
    settings = quant_settings(tmp_path)
    start_ts = 300 * 60
    data = engine.symbol_candidates(settings, "TESTUSDT", strategy="ema_trend", timeframe="1h",
                                    params=engine.QuantParams(), sim=engine.simulate_settings(settings),
                                    start_ts=start_ts, end_ts=400 * 60)
    assert data["first_close"] == pytest.approx(100.0 + 300 * 0.001)
    assert data["first_close"] != pytest.approx(frame["close"].iloc[0])


def test_backtest_reports_the_gate_funnel_even_with_zero_trades(tmp_path: Path, monkeypatch):
    import cat.quant.engine as engine

    frame = _bars_1m(6000, drift=0.0)
    monkeypatch.setattr(engine, "load_bars", lambda *args, **kwargs: frame)
    monkeypatch.setattr(engine, "current_data_version", lambda settings: {})
    settings = quant_settings(tmp_path)
    result = engine.run_backtest(settings, ["TESTUSDT"], strategy="ema_trend", timeframe="1h",
                                 params=engine.QuantParams())
    diagnostics = result["metrics"]["signal_diagnostics"]
    assert diagnostics["ready_bars"] > 0
    assert diagnostics["entry_signals"] == 0
    assert diagnostics["funnel"]["trend"] == 0
    assert result["per_symbol"][0]["funnel"]["trend"] == 0
    assert result["metrics"]["trades"] == 0


# ------------------------------------------------------- market regime gate (D-012)

def test_market_regime_requires_closed_complete_bars(tmp_path: Path, monkeypatch):
    import cat.quant.engine as engine

    rising = _bars_1m(600, drift=0.01)
    monkeypatch.setattr(engine, "load_bars", lambda *args, **kwargs: rising)
    settings = quant_settings(tmp_path)
    sim = engine.simulate_settings(settings, regime_symbol="BTCUSDT", regime_ema=3)
    allowed = engine.market_regime(settings, sim, "1h", start_ts=0, end_ts=600 * 60)
    assert allowed.tolist() == [10800 + 3600 * step for step in range(7)], \
        "EMA(3) is defined from the third complete bucket, and the window end is exclusive"

    gapped = rising.drop(index=70).reset_index(drop=True)  # one missing 1m bar in hour two
    monkeypatch.setattr(engine, "load_bars", lambda *args, **kwargs: gapped)
    engine._REGIME_CACHE.clear()
    allowed = engine.market_regime(settings, sim, "1h", start_ts=0, end_ts=600 * 60)
    assert 7200 not in allowed.tolist(), "an incomplete bucket is an unknown regime, so it blocks"


def test_market_regime_is_empty_but_valid_in_a_downtrend(tmp_path: Path, monkeypatch):
    import cat.quant.engine as engine

    falling = _bars_1m(600, base=1_000.0, drift=-0.05)
    monkeypatch.setattr(engine, "load_bars", lambda *args, **kwargs: falling)
    settings = quant_settings(tmp_path)
    sim = engine.simulate_settings(settings, regime_symbol="BTCUSDT", regime_ema=3)
    allowed = engine.market_regime(settings, sim, "1h", start_ts=0, end_ts=600 * 60)
    assert allowed.size == 0, "a whole down window is a result, not a crash"


def test_market_regime_raises_when_the_ema_is_never_defined(tmp_path: Path, monkeypatch):
    import cat.quant.engine as engine

    short = _bars_1m(120, drift=0.01)
    monkeypatch.setattr(engine, "load_bars", lambda *args, **kwargs: short)
    settings = quant_settings(tmp_path)
    sim = engine.simulate_settings(settings, regime_symbol="BTCUSDT", regime_ema=200)
    with pytest.raises(engine.StorageError):
        engine.market_regime(settings, sim, "1h", start_ts=0, end_ts=120 * 60)


def test_backtest_fails_loud_when_the_regime_proxy_is_missing(tmp_path: Path, monkeypatch):
    import cat.quant.engine as engine

    frame = _bars_1m(600, drift=0.01)

    def fake_load(settings, symbol, **kwargs):
        if symbol == "MISSINGUSDT":
            raise engine.StorageError(f"{symbol}: no stored bars")
        return frame

    monkeypatch.setattr(engine, "load_bars", fake_load)
    monkeypatch.setattr(engine, "current_data_version", lambda settings: {})
    settings = quant_settings(tmp_path)
    sim = engine.simulate_settings(settings, regime_symbol="MISSINGUSDT", regime_ema=3)
    with pytest.raises(engine.StorageError):
        engine.run_backtest(settings, ["TESTUSDT"], strategy="ema_trend", timeframe="1h",
                            params=engine.QuantParams(), sim=sim)


# ------------------------------------------------------------------ data guards

def test_load_bars_reads_only_months_that_overlap_the_window():
    from cat.quant.engine import months_for_window

    settings = quant_settings(Path("."))
    assert months_for_window(settings, None, None) == list(settings.months)
    june_start = 1780272000  # 2026-06-01T00:00:00Z
    july_start = 1782864000
    assert months_for_window(settings, june_start, july_start) == ["2026-06"]
    assert months_for_window(settings, july_start + 60, july_start + 120) == ["2026-07"]


def test_load_bars_fails_closed_on_a_corrupt_lake(tmp_path: Path):
    import pandas as pd

    from cat.quant.engine import load_bars
    from cat.quant.storage import month_path

    settings = quant_settings(tmp_path)
    frame = pd.DataFrame({"ts": [0, 60], "open": [1.0, float("nan")], "high": [1.0, 1.0],
                          "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [1.0, 1.0]})
    path = month_path(settings, "TESTUSDT", "2026-06")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    with pytest.raises(StorageError, match="non-finite"):
        load_bars(settings, "TESTUSDT")
