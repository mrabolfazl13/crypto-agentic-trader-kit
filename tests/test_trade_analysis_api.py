"""API tests for trade-analysis reports, built only from real stored backtest runs."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import cat.api as api_module

BASE = "/api/v1"
STEP_4H = 14400

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    # The repo config's 0.2% slippage ceiling makes the round-trip cost so heavy that
    # no synthetic uptrend clears the net reward/risk gate; 0.05% keeps the cost model
    # real (fees untouched) while letting the engine actually trade.
    shutil.copytree(REPO / "schemas", tmp_path / "schemas")
    shutil.copy(REPO / "config.yaml", tmp_path / "config.yaml")
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    raw["risk"]["max_slippage_pct"] = 0.05
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    (tmp_path / "state").mkdir()
    return tmp_path


@pytest.fixture()
def app(workspace: Path):
    return api_module.create_app(root=workspace)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _write_csv(path: Path, count: int = 720) -> None:
    # 8 gentle climbs then a deeper dip per cycle: TRENDING_UP with RSI in the
    # healthy band, and an ATR wide enough to clear the net reward/risk gate.
    lines = ["timestamp,open,high,low,close,volume"]
    previous = 50_000.0
    for index in range(count):
        close = previous * (1.005 if index % 10 < 8 else 0.99)
        lines.append(f"{1_700_000_000 + index * STEP_4H},{previous},{max(previous, close) * 1.001},"
                     f"{min(previous, close) * 0.999},{close},100")
        previous = close
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_falling_csv(path: Path, count: int = 720) -> None:
    # A one-way slide: no pullback entry can trigger, so the engine must record zero trades.
    lines = ["timestamp,open,high,low,close,volume"]
    previous = 50_000.0
    for index in range(count):
        close = previous * 0.998
        lines.append(f"{1_700_000_000 + index * STEP_4H},{previous},{max(previous, close) * 1.001},"
                     f"{min(previous, close) * 0.999},{close},100")
        previous = close
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _import_and_backtest(client: TestClient, workspace: Path, writer, name: str) -> str:
    csv_path = workspace / name
    writer(csv_path)
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/runs/backtest",
                       json={"symbol": "BTC/USDT", "dataset_id": imported["dataset_id"], "warmup": 200})
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"]


def test_report_is_built_from_a_real_backtest_run_and_persisted(client: TestClient, workspace: Path):
    run_id = _import_and_backtest(client, workspace, _write_csv, "ta.csv")

    resp = client.post(f"{BASE}/trade-analysis", json={"run_ids": [run_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["report_id"]) == 15 and body["report_id"].startswith("ta-")
    assert body["run_ids"] == [run_id]
    assert body["trade_count"] > 0
    assert body["symbols"] == ["BTC_USDT"]
    # every report dimension carries its grouped stats, losers never dropped
    for dim in ("symbol", "regime", "exit_reason", "entry_reason", "side", "hold_bucket", "weekday", "hour"):
        assert dim in body["groups"], dim
        assert len(body["groups"][dim]) > 0, dim
    assert len(body["explanations"]) == body["trade_count"]
    assert len(body["discovery"]["hypotheses"]) <= 3
    # the report on disk is the one served back
    listed = client.get(f"{BASE}/trade-analysis").json()
    assert [item["report_id"] for item in listed] == [body["report_id"]]
    assert listed[0]["trade_count"] == body["trade_count"]
    detail = client.get(f"{BASE}/trade-analysis/{body['report_id']}")
    assert detail.status_code == 200
    assert detail.json()["report_id"] == body["report_id"]
    assert detail.json()["config_snapshot"]["mode"] == "PAPER"


def test_auto_selection_uses_the_latest_backtest_runs(client: TestClient, workspace: Path):
    _import_and_backtest(client, workspace, _write_csv, "ta-auto.csv")
    resp = client.post(f"{BASE}/trade-analysis", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trade_count"] > 0
    assert len(body["run_ids"]) == 1


def test_unknown_run_id_is_a_422_not_a_silent_empty_report(client: TestClient):
    resp = client.post(f"{BASE}/trade-analysis", json={"run_ids": ["bt-nonexistent00"]})
    assert resp.status_code == 422
    assert "یافت نشد" in resp.json()["detail"]


def test_limit_runs_bounds_are_enforced(client: TestClient):
    resp = client.post(f"{BASE}/trade-analysis", json={"limit_runs": 0})
    assert resp.status_code == 422
    resp = client.post(f"{BASE}/trade-analysis", json={"limit_runs": 51})
    assert resp.status_code == 422


def test_report_id_that_cannot_exist_is_a_404(client: TestClient):
    assert client.get(f"{BASE}/trade-analysis/ta-nothere0000").status_code == 404
    assert client.get(f"{BASE}/trade-analysis/not-a-report-id").status_code == 404


def test_a_zero_trade_run_produces_an_honest_report_not_a_crash(client: TestClient, workspace: Path):
    run_id = _import_and_backtest(client, workspace, _write_falling_csv, "ta-falling.csv")
    resp = client.post(f"{BASE}/trade-analysis", json={"run_ids": [run_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trade_count"] == 0
    assert "هیچ معامله‌ای" in body["note"]
    assert body["discovery"]["hypotheses"] == []
