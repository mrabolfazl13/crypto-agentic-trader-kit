"""Offline tests for the resumable autonomous optimization loop."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cat.api as api_module
import cat.optimizer as optimizer

BASE = "/api/v1"
STEP_4H = 14400


@pytest.fixture()
def app(workspace: Path):
    return api_module.create_app(root=workspace)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _write_csv(path: Path, count: int = 720) -> None:
    lines = ["timestamp,open,high,low,close,volume"]
    previous = 50_000.0
    for index in range(count):
        close = previous * (1.004 if index % 7 else 0.994)
        lines.append(f"{1_700_000_000 + index * STEP_4H},{previous},{max(previous, close) * 1.001},"
                     f"{min(previous, close) * 0.999},{close},100")
        previous = close
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _body(label: str, *, verdict: str, net: float, reasons: list[str]) -> dict:
    return {
        "verdict": verdict,
        "reasons": reasons,
        "candidate_label": label,
        "champion_label": "trend-pullback:v1@base",
        "dataset_id": "ds-test",
        "split": {"train": {"bars": 400}},
        "validation": {"metrics": {"net_return_pct": net, "max_drawdown_pct": 2.0, "trades": 12}},
        # real aggregates use median/mean names; the recorder must read those too
        "walk_forward": {"aggregate": {"net_return_pct_median": net, "max_drawdown_pct": 2.0,
                                       "profit_factor_mean": 1.1, "trades": 12}},
    }


def test_choose_next_answers_a_trade_shortage_with_a_wider_entry(monkeypatch):
    queue = ["target-distance", "stop-distance", "rsi-band"]
    reordered, reason = optimizer.choose_next(
        queue, [{"verdict": "rejected", "reasons": ["only 4 out-of-sample trades, minimum is 8"]}])
    assert reordered[0] == "rsi-band"  # tagged more_trades, unlike the others
    assert "rsi-band" in reason
    assert sorted(reordered) == sorted(queue)  # no hypothesis is lost or duplicated


def test_choose_next_answers_a_drawdown_rejection_with_a_risk_hypothesis():
    reordered, _ = optimizer.choose_next(
        ["target-distance", "rsi-band", "stop-distance"],
        [{"verdict": "rejected", "reasons": ["max drawdown 9% is more than champion 3%"]}])
    assert reordered[0] == "stop-distance"


def test_learning_text_records_the_reason_not_a_promise():
    text = optimizer.learning_text(_body(
        "trend-pullback:v2", verdict="rejected", net=-1.5,
        reasons=["only 2 out-of-sample trades, minimum is 8"]))
    assert "رد شد" in text
    assert "نمونه‌ی خارج از نمونه" in text
    assert "تضمین" not in text and "سود قطعی" not in text


def test_learning_text_calls_an_unchanged_candidate_no_evidence():
    text = optimizer.learning_text(_body(
        "trend-pullback:v1@81646835a339", verdict="rejected", net=0.0,
        reasons=["candidate is the unchanged champion: the grid selected the current parameters, "
                 "so this hypothesis did not alter behaviour on this dataset and nothing new was tested"]))
    assert "همان پارامترهای قهرمان" in text
    assert "شواهد جدیدی" in text
    assert "تضمین" not in text and "سود قطعی" not in text


def test_learning_text_reports_the_market_the_candidate_raced_against():
    body = _body("trend-pullback:v2", verdict="rejected", net=-1.5,
                 reasons=["no out-of-sample net-return improvement"])
    body["validation"]["buy_hold_pct"] = 12.3
    text = optimizer.learning_text(body)
    assert "+12.3%" in text
    assert "بازار در همان بازه" in text


def test_the_experiment_record_carries_the_oos_window_context_and_curves():
    body = _body("trend-pullback:v2", verdict="rejected", net=-1.5,
                 reasons=["only 4 out-of-sample trades, minimum is 8"])
    body["validation"].update({"buy_hold_pct": 4.0,
                               "equity_curve": [[1.0, 100.0], [2.0, 101.0]],
                               "benchmark_curve": [[1.0, 100.0], [2.0, 104.0]]})
    body["champion_validation"] = {"metrics": {"net_return_pct": 0.5},
                                   "equity_curve": [[1.0, 100.0], [2.0, 100.5]]}
    record = optimizer._record(body, {"id": "hyp", "question": "q", "grid": {}})
    assert record["oos_net_return_pct"] == -1.5
    assert record["champion_oos_net_return_pct"] == 0.5
    assert record["buy_hold_pct"] == 4.0
    assert record["oos_equity_curve"] == [[1.0, 100.0], [2.0, 101.0]]
    assert record["oos_benchmark_curve"] == [[1.0, 100.0], [2.0, 104.0]]
    assert record["champion_oos_equity_curve"] == [[1.0, 100.0], [2.0, 100.5]]


def test_new_state_only_queues_the_requested_number_of_experiments():
    state = optimizer.new_state(symbol="BTC_USDT", timeframe="4h", dataset_id="ds-1",
                                max_experiments=2, folds=3, warmup=80, spread_pct=0.05)
    assert state["run_id"].startswith("opt-")
    assert len(state["queue"]) == 2
    assert state["status"] == "running"
    assert state["experiments"] == []


def test_start_at_selects_a_later_round_of_the_pre_registered_plan():
    state = optimizer.new_state(symbol="BTC_USDT", timeframe="4h", dataset_id="ds-1",
                                max_experiments=3, folds=3, warmup=80, spread_pct=0.05,
                                start_at="holding-minimum")
    assert state["queue"] == ["holding-minimum", "rsi-ceiling", "entry-volume-quality"]
    assert state["current"] == "holding-minimum"
    with pytest.raises(ValueError, match="در فهرست این اجرا نیست"):
        optimizer.new_state(symbol="BTC_USDT", timeframe="4h", dataset_id="ds-1",
                            max_experiments=1, folds=3, warmup=80, spread_pct=0.05,
                            start_at="made-up-hypothesis")


def test_optimizer_runs_experiments_and_exports_them(client: TestClient, workspace: Path, monkeypatch):
    csv_path = workspace / "opt.csv"
    _write_csv(csv_path)
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()

    calls: list[str] = []

    def fake_experiment(config, store, **kwargs):
        calls.append(kwargs["hypothesis"]["id"])
        if len(calls) == 1:
            return _body("trend-pullback:v2", verdict="rejected", net=-1.2,
                         reasons=["only 4 out-of-sample trades, minimum is 8"])
        return _body("trend-pullback:v3", verdict="accepted", net=2.4, reasons=[])

    monkeypatch.setattr(optimizer, "run_experiment", fake_experiment)
    started = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "dataset_id": imported["dataset_id"],
        "max_experiments": 2, "folds": 2, "warmup": 80})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    detail = {}
    for _ in range(80):
        detail = client.get(f"{BASE}/optimizer/{run_id}").json()
        if detail["status"] in {"done", "failed", "stopped"}:
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail
    assert len(detail["experiments"]) == 2
    assert detail["experiments"][0]["verdict"] == "rejected"
    assert detail["experiments"][1]["verdict"] == "accepted"
    assert detail["experiments"][1]["net_return_pct"] == 2.4
    assert detail["experiments"][1]["profit_factor"] == 1.1
    assert detail["champion"]["params_label"] == "trend-pullback:v3"
    assert calls[0] == "entry-speed"  # pre-registered first question
    assert detail["experiments"][0]["learning"]

    listed = client.get(f"{BASE}/optimizer").json()["runs"]
    assert any(run["run_id"] == run_id for run in listed)
    assert next(run for run in listed if run["run_id"] == run_id)["accepted"] == 1

    csv_text = client.get(f"{BASE}/optimizer/{run_id}/export", params={"format": "csv"}).text
    assert csv_text.lstrip("\ufeff").startswith("index,hypothesis_id,question")
    assert csv_text.count("\n") >= 3  # header + two experiments
    exported = client.get(f"{BASE}/optimizer/{run_id}/export", params={"format": "json"}).json()
    assert exported["run_id"] == run_id
    assert client.get(f"{BASE}/optimizer/{run_id}/export", params={"format": "xml"}).status_code == 422
    assert client.get(f"{BASE}/optimizer/opt-nope").status_code == 404


def test_summary_does_not_call_a_no_trade_experiment_the_best():
    state = {
        "run_id": "opt-x", "status": "done", "symbol": "BTC_USDT", "timeframe": "4h",
        "dataset_id": "ds", "created_at": None, "updated_at": None, "queue": [], "current": None,
        "experiments": [
            {"verdict": "rejected", "net_return_pct": 0.0, "trades": 0},
            {"verdict": "rejected", "net_return_pct": -1.2, "trades": 9},
        ],
    }
    assert optimizer.summary(state)["best_net_return_pct"] == -1.2


def test_optimizer_refuses_a_dataset_too_small_for_honest_segments(client: TestClient, workspace: Path):
    csv_path = workspace / "opt-small.csv"
    _write_csv(csv_path, 360)
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "dataset_id": imported["dataset_id"],
        "max_experiments": 1, "folds": 2, "warmup": 80})
    assert resp.status_code == 422
    assert "600" in resp.json()["detail"]


def test_optimizer_stop_and_resume_continue_without_repeating_a_hypothesis(
        client: TestClient, workspace: Path, monkeypatch):
    csv_path = workspace / "opt-resume.csv"
    _write_csv(csv_path)
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()

    finished: list[str] = []
    gate = threading.Event()

    def slow_experiment(config, store, **kwargs):
        gate.wait(timeout=2.0)
        finished.append(kwargs["hypothesis"]["id"])
        return _body(f"trend-pullback:{kwargs['hypothesis']['id']}", verdict="rejected", net=-0.5,
                     reasons=["no out-of-sample net-return improvement"])

    monkeypatch.setattr(optimizer, "run_experiment", slow_experiment)
    started = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "dataset_id": imported["dataset_id"],
        "max_experiments": 3, "folds": 2, "warmup": 80})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    assert client.post(f"{BASE}/optimizer/{run_id}/stop").status_code == 200
    gate.set()
    detail = {}
    for _ in range(80):
        detail = client.get(f"{BASE}/optimizer/{run_id}").json()
        if detail["status"] == "stopped":
            break
        time.sleep(0.05)
    assert detail["status"] == "stopped", detail
    assert len(detail["experiments"]) < 3

    resumed = client.post(f"{BASE}/optimizer/{run_id}/resume")
    assert resumed.status_code == 200, resumed.text
    for _ in range(80):
        detail = client.get(f"{BASE}/optimizer/{run_id}").json()
        if detail["status"] == "done":
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail
    ids = [item["hypothesis_id"] for item in detail["experiments"]]
    assert len(ids) == 3
    assert len(set(ids)) == 3  # resume never re-measures a recorded hypothesis
    assert client.post(f"{BASE}/optimizer/{run_id}/resume").status_code == 409


def test_every_registered_hypothesis_grid_is_validatable():
    """A pre-registered hypothesis must be legally measurable: keys tunable, values in bounds."""
    from cat import research
    from cat.strategies import default_params
    from cat.strategy_lab import LAB_STRATEGIES

    base = default_params()
    ids = [item["id"] for item in optimizer.PLAN]
    assert len(ids) == len(set(ids))
    for item in optimizer.PLAN:
        keys = research.validate_grid(item["grid"], base)
        family = item.get("strategy")
        if family is not None:
            assert family in LAB_STRATEGIES, item["id"]
        assert keys or family, item["id"]  # an empty grid is only legal when the entry family itself changes
        combos = 1
        for values in item["grid"].values():
            combos *= len(values)
        assert combos <= research.MAX_GRID_EVALS, item["id"]


def test_the_loop_passes_a_family_hypothesis_through_to_research(monkeypatch):
    """`strategy` on a hypothesis reaches run_research, and the stored record keeps the family visible."""
    seen: dict[str, object] = {}

    def fake_run_research(store, config, **kwargs):
        seen.update(kwargs)
        return {"verdict": "rejected", "reasons": [], "candidate_label": f"{kwargs['strategy']}|trend-pullback:v1@x",
                "champion_label": "trend-pullback:v1@x", "candidate_params": {},
                "strategy": kwargs.get("strategy") or "", "champion_strategy": "", "dataset_id": kwargs["dataset_id"],
                "split": {"train": {"bars": 400}},
                "walk_forward": {"aggregate": {"net_return_pct_median": 0.1, "max_drawdown_pct": 0.2,
                                               "profit_factor_mean": 1.1, "trades": 9}},
                "train": {"metrics": {}}, "validation": {}, "decision": "rejected"}

    monkeypatch.setattr("cat.research.run_research", fake_run_research)
    hypothesis = next(item for item in optimizer.PLAN if item.get("strategy"))
    body = optimizer.run_experiment(object(), object(), symbol="BTC_USDT", timeframe="4h", bars=[], higher=[],
                                   filters=object(), dataset_id="ds-1", hypothesis=hypothesis, champion=None,
                                   folds=3, warmup=80, spread_pct=0.05)
    assert seen["strategy"] == hypothesis["strategy"] and seen["grid"] == {}
    record = optimizer._record(body, hypothesis)
    assert record["strategy"] == hypothesis["strategy"] and record["champion_strategy"] == ""


GEN_HYP = {"id": "h-test-stop", "question": "آیا استاپ بازتر روی این داده بهتر است؟",
           "grid": {"atr_stop_mult": [2.4]}}


def _import_dataset(client: TestClient, workspace: Path) -> str:
    csv_path = workspace / "opt-pool.csv"
    _write_csv(csv_path)
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    return imported["dataset_id"]


def _wait_for_status(client: TestClient, run_id: str, statuses: set[str], tries: int = 120) -> dict:
    detail = {}
    for _ in range(tries):
        detail = client.get(f"{BASE}/optimizer/{run_id}").json()
        if detail["status"] in statuses:
            break
        time.sleep(0.05)
    return detail


def test_generated_hypotheses_run_in_the_loop(client: TestClient, workspace: Path, monkeypatch):
    dataset_id = _import_dataset(client, workspace)
    calls: list[str] = []

    def fake_experiment(config, store, **kwargs):
        calls.append(kwargs["hypothesis"]["id"])
        return _body("trend-pullback:v2", verdict="rejected", net=-0.5,
                     reasons=["no out-of-sample net-return improvement"])

    monkeypatch.setattr(optimizer, "run_experiment", fake_experiment)
    started = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "dataset_id": dataset_id, "max_experiments": 1,
        "folds": 2, "warmup": 80, "hypotheses": [GEN_HYP]})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    detail = _wait_for_status(client, run_id, {"done", "failed"})
    assert detail["status"] == "done", detail
    assert calls == ["h-test-stop"]
    assert detail["experiments"][0]["hypothesis_id"] == "h-test-stop"


def test_start_at_selects_inside_a_custom_pool():
    pool = [GEN_HYP, {"id": "h-test-target", "question": "q2", "grid": {"atr_target_mult": [2.5]}}]
    state = optimizer.new_state(symbol="BTC_USDT", timeframe="4h", dataset_id="ds-1",
                                max_experiments=2, folds=2, warmup=80, spread_pct=0.05,
                                start_at="h-test-target", hypotheses=pool)
    # start_at keeps the tail of the pool from that hypothesis onward
    assert state["queue"] == ["h-test-target"]
    assert [h["id"] for h in state["hypotheses"]] == ["h-test-stop", "h-test-target"]


def test_generated_pool_rejections(client: TestClient, workspace: Path):
    dataset_id = _import_dataset(client, workspace)
    cases = [
        ({"hypotheses": [dict(GEN_HYP, id="entry-speed")]}, "تداخل", "ds-anything"),
        ({"hypotheses": [GEN_HYP, dict(GEN_HYP)]}, "تکراری", "ds-anything"),
        ({"hypotheses": [dict(GEN_HYP, grid={"moon_phase": [1]})]}, "moon_phase", "ds-anything"),
        ({"hypotheses": []}, "غیرخالی", "ds-anything"),
        ({"hypotheses": [dict(GEN_HYP, strategy="nope")]}, "خانواده‌ی ناشناخته", "ds-anything"),
        # the count bound is checked inside start(), i.e. after the dataset resolves
        ({"hypotheses": [GEN_HYP], "max_experiments": 2}, "max_experiments باید بین", dataset_id),
    ]
    for payload, expected, ds in cases:
        body = {"symbol": "BTC/USDT", "dataset_id": ds, "max_experiments": 1,
                "folds": 2, "warmup": 80, **payload}
        resp = client.post(f"{BASE}/optimizer/start", json=body)
        assert resp.status_code == 422, (payload, resp.status_code, resp.text)
        assert expected in resp.json()["detail"], payload


def test_pre_upgrade_state_without_hypotheses_falls_back_to_plan(
        client: TestClient, workspace: Path, monkeypatch):
    dataset_id = _import_dataset(client, workspace)
    gate = threading.Event()
    finished: list[str] = []

    def slow_experiment(config, store, **kwargs):
        gate.wait(timeout=2.0)
        finished.append(kwargs["hypothesis"]["id"])
        return _body(f"trend-pullback:{kwargs['hypothesis']['id']}", verdict="rejected", net=-0.5,
                     reasons=["no out-of-sample net-return improvement"])

    monkeypatch.setattr(optimizer, "run_experiment", slow_experiment)
    started = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "dataset_id": dataset_id, "max_experiments": 2,
        "folds": 2, "warmup": 80})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    assert client.post(f"{BASE}/optimizer/{run_id}/stop").status_code == 200
    gate.set()
    _wait_for_status(client, run_id, {"stopped"})

    # Simulate a state written by the pre-pool build: no "hypotheses" key at all.
    state = optimizer.load_state(workspace, run_id)
    state.pop("hypotheses", None)
    optimizer._write(state, workspace)

    resumed = client.post(f"{BASE}/optimizer/{run_id}/resume")
    assert resumed.status_code == 200, resumed.text
    detail = _wait_for_status(client, run_id, {"done", "failed"})
    assert detail["status"] == "done", detail
    ids = [item["hypothesis_id"] for item in detail["experiments"]]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_a_state_whose_queue_id_is_missing_from_its_pool_fails_closed(
        client: TestClient, workspace: Path, monkeypatch):
    dataset_id = _import_dataset(client, workspace)

    def broken_experiment(config, store, **kwargs):
        raise AssertionError("the loop must refuse an unknown hypothesis before measuring")

    monkeypatch.setattr(optimizer, "run_experiment", broken_experiment)
    started = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "dataset_id": dataset_id, "max_experiments": 1,
        "folds": 2, "warmup": 80, "hypotheses": [GEN_HYP]})
    run_id = started.json()["run_id"]

    state = optimizer.load_state(workspace, run_id)
    state["hypotheses"] = [dict(GEN_HYP, id="h-other")]
    optimizer._write(state, workspace)
    client.post(f"{BASE}/optimizer/{run_id}/resume")

    detail = _wait_for_status(client, run_id, {"done", "failed"})
    assert detail["status"] == "failed", detail
    assert "ثبت نشده" in (detail["error"] or "")
