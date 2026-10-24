"""End-to-end pipeline on offline fixture data: import → backtest → loop verdict →
exports → trade-analysis report. No mocks: the real engine, the real acceptance gate
and the real report builder all run inside the API process."""

from __future__ import annotations

import csv
import io
import json
import shutil
import time
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
    # Same cost model as the other engine tests: fees stay untouched, slippage is set
    # to 0.05% so the synthetic series can clear the net reward/risk gate.
    shutil.copytree(REPO / "schemas", tmp_path / "schemas")
    shutil.copy(REPO / "config.yaml", tmp_path / "config.yaml")
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    raw["risk"]["max_slippage_pct"] = 0.05
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    (tmp_path / "state").mkdir()
    return tmp_path


@pytest.fixture()
def client(workspace: Path) -> TestClient:
    return TestClient(api_module.create_app(root=workspace))


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


def _wait_for_status(client: TestClient, run_id: str, statuses: set[str], tries: int = 600) -> dict:
    detail = {}
    for _ in range(tries):
        detail = client.get(f"{BASE}/optimizer/{run_id}").json()
        if detail["status"] in statuses:
            break
        time.sleep(0.1)
    return detail


def test_offline_pipeline_from_import_to_report(client: TestClient, workspace: Path):
    csv_path = workspace / "e2e.csv"
    _write_csv(csv_path)
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"})
    assert imported.status_code == 200, imported.text
    dataset_id = imported.json()["dataset_id"]

    baseline = client.post(f"{BASE}/runs/backtest",
                           json={"symbol": "BTC/USDT", "dataset_id": dataset_id, "warmup": 80})
    assert baseline.status_code == 200, baseline.text

    started = client.post(f"{BASE}/optimizer/start", json={
        "symbol": "BTC/USDT", "timeframe": "4h", "dataset_id": dataset_id,
        "max_experiments": 1, "folds": 3, "warmup": 80, "assumed_spread_pct": 0.05,
        "hypotheses": [{"id": "e2e-exit-target",
                        "question": "آیا هدف دورتر روی این داده تفاوت قابل‌سنجش می‌سازد؟",
                        "grid": {"atr_target_mult": [3.5, 4.5]}}]})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    detail = _wait_for_status(client, run_id, {"done", "failed"})
    assert detail["status"] == "done", detail
    assert len(detail["experiments"]) == 1
    exp = detail["experiments"][0]
    assert exp["hypothesis_id"] == "e2e-exit-target"
    assert exp["verdict"] in {"accepted", "rejected"}, exp
    assert exp.get("candidate_label"), exp
    if exp["verdict"] == "rejected":
        assert exp.get("reasons"), exp

    json_export = client.get(f"{BASE}/optimizer/{run_id}/export")
    assert json_export.status_code == 200
    assert json.loads(json_export.content)["status"] == "done"

    csv_export = client.get(f"{BASE}/optimizer/{run_id}/export", params={"format": "csv"})
    assert csv_export.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_export.content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["index", "hypothesis_id", "question", "verdict"]
    assert len(rows) == 2 and rows[1][1] == "e2e-exit-target"

    report = client.post(f"{BASE}/trade-analysis", json={"limit_runs": 10})
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["trade_count"] > 0, body.get("note")
    assert body["config_snapshot"]["mode"] == "PAPER"
    assert isinstance(body["discovery"]["hypotheses"], list)

    stored = client.get(f"{BASE}/trade-analysis/{body['report_id']}")
    assert stored.status_code == 200
    assert stored.json()["report_id"] == body["report_id"]
