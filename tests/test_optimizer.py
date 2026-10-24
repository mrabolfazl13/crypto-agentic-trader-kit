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
    with pytest.raises(ValueError, match="در فهرست پیش‌ثبت‌شده نیست"):
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

    base = default_params()
    ids = [item["id"] for item in optimizer.PLAN]
    assert len(ids) == len(set(ids))
    for item in optimizer.PLAN:
        keys = research.validate_grid(item["grid"], base)
        assert keys, item["id"]
        combos = 1
        for values in item["grid"].values():
            combos *= len(values)
        assert combos <= research.MAX_GRID_EVALS, item["id"]
