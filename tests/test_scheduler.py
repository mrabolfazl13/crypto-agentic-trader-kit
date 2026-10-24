"""Scheduler tests: due bookkeeping, PAPER guards, and the review→loop chain.

The network never runs here: the paper-cycle job is exercised with a fake Engine
and a fake mirror, while the due stamps, mode/kill-switch guards and the weekly
review wiring are the real code paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

import cat.api as api_module
import cat.optimizer as optimizer_mod
import cat.scheduler as scheduler_mod
import cat.trade_analysis as trade_analysis
from cat.config import ConfigError, load_config
from cat.scheduler import Automation
from cat.store import open_store

UTC = timezone.utc
BASE = "/api/v1"


def _loader(root: Path, mode: str = "PAPER"):
    kill = root / "STOP_TRADING"
    return lambda: SimpleNamespace(
        mode=mode,
        kill_switch_file=kill,
        auto_cycle_hours=24.0,
        auto_review_days=7.0,
        market="crypto",
        maker_fee_pct=0.10,
        taker_fee_pct=0.10,
    )


def _set_automation(workspace: Path, hours: float, days: float) -> None:
    path = workspace / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["automation"] = {"paper_cycle_hours": hours, "research_review_days": days}
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_negative_interval_is_rejected(workspace: Path):
    _set_automation(workspace, -1, 7)
    with pytest.raises(ConfigError):
        load_config(root=workspace)


def test_intervals_are_exposed_by_config(workspace: Path):
    _set_automation(workspace, 12, 3)
    config = load_config(root=workspace)
    assert config.auto_cycle_hours == 12.0
    assert config.auto_review_days == 3.0


# ---------------------------------------------------------------------------
# due bookkeeping
# ---------------------------------------------------------------------------

def test_due_stamps_persist_across_restart(workspace: Path):
    store = open_store(workspace)
    auto = Automation(workspace, store, cycle_hours=24, review_days=7)
    calls: list[str] = []
    auto.run_cycle = lambda now=None: calls.append("cycle") or {"status": "ok"}
    auto.run_review = lambda now=None: calls.append("review") or {"status": "ok"}

    t0 = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    auto.run_due(t0)
    assert calls == ["cycle", "review"]
    assert store.get_meta(scheduler_mod.CYCLE_LAST) == "2026-10-25T00:00:00+00:00"

    auto.run_due(t0 + timedelta(hours=23))
    assert calls == ["cycle", "review"]

    auto.run_due(t0 + timedelta(hours=24))
    assert calls == ["cycle", "review", "cycle"]

    restarted = Automation(workspace, store, cycle_hours=24, review_days=7)
    restarted_calls: list[str] = []
    restarted.run_cycle = lambda now=None: restarted_calls.append("cycle") or {"status": "ok"}
    restarted.run_review = lambda now=None: restarted_calls.append("review") or {"status": "ok"}
    restarted.run_due(t0 + timedelta(hours=25))
    assert restarted_calls == []


def test_disabled_jobs_stay_idle(workspace: Path):
    store = open_store(workspace)
    auto = Automation(workspace, store, cycle_hours=0, review_days=0)
    calls: list[str] = []
    auto.run_cycle = lambda now=None: calls.append("cycle") or {"status": "ok"}
    auto.run_review = lambda now=None: calls.append("review") or {"status": "ok"}
    assert auto.run_due(datetime(2026, 10, 25, tzinfo=UTC)) == {}
    assert calls == []


def test_start_is_refused_outside_paper(workspace: Path):
    store = open_store(workspace)
    auto = Automation(workspace, store, config_loader=_loader(workspace, mode="LIVE"))
    assert auto.start() is False
    assert auto.status()["enabled"] is False
    last = store._rows("SELECT message FROM logs WHERE source='automation' ORDER BY ts DESC, id DESC LIMIT 1")
    assert "PAPER-only" in last[0]["message"]


def test_start_runs_a_daemon_thread_and_stops(workspace: Path):
    store = open_store(workspace)
    auto = Automation(workspace, store, config_loader=_loader(workspace), initial_delay_seconds=3600)
    assert auto.start() is True
    assert auto.status()["enabled"] is True
    auto.stop()
    assert auto.status()["enabled"] is False


# ---------------------------------------------------------------------------
# paper cycle job
# ---------------------------------------------------------------------------

def test_kill_switch_skips_without_building_engine(workspace: Path, monkeypatch):
    store = open_store(workspace)
    (workspace / "STOP_TRADING").write_text("stop\n", encoding="utf-8")
    built: list[object] = []
    monkeypatch.setattr(scheduler_mod, "Engine", lambda config: built.append(config))
    auto = Automation(workspace, store, config_loader=_loader(workspace))

    result = auto.run_cycle(datetime(2026, 10, 25, tzinfo=UTC))

    assert result["status"] == "skipped"
    assert "kill switch" in result["detail"]
    assert built == []
    assert store.get_meta(scheduler_mod.CYCLE_STATUS) == "skipped"


def test_cycle_in_live_mode_is_skipped(workspace: Path, monkeypatch):
    store = open_store(workspace)
    built: list[object] = []
    monkeypatch.setattr(scheduler_mod, "Engine", lambda config: built.append(config))
    auto = Automation(workspace, store, config_loader=_loader(workspace, mode="LIVE"))

    result = auto.run_cycle(datetime(2026, 10, 25, tzinfo=UTC))

    assert result["status"] == "skipped"
    assert "PAPER-only" in result["detail"]
    assert built == []


def test_cycle_success_mirrors_and_records(workspace: Path, monkeypatch):
    store = open_store(workspace)
    mirrored: list[str] = []
    seen: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, config):
            seen["config"] = config

        def run_cycle(self, *, now, dry_run):
            seen["dry_run"] = dry_run
            return SimpleNamespace(
                outcomes=[object()], errors=[], kill_switch=False,
                summary=lambda: "cycle ok: 1 outcome",
            )

    monkeypatch.setattr(scheduler_mod, "Engine", FakeEngine)
    monkeypatch.setattr(scheduler_mod, "mirror_cycle",
                        lambda store, engine, config, result, source="paper": mirrored.append(source))
    auto = Automation(workspace, store, config_loader=_loader(workspace))

    result = auto.run_cycle(datetime(2026, 10, 25, tzinfo=UTC))

    assert result["status"] == "ok"
    assert seen["dry_run"] is False
    assert mirrored == ["paper"]
    assert store.get_meta(scheduler_mod.CYCLE_STATUS) == "ok"
    assert store.get_meta(scheduler_mod.CYCLE_SUMMARY) == "cycle ok: 1 outcome"


def test_unreachable_data_never_mirrors(workspace: Path, monkeypatch):
    store = open_store(workspace)
    mirrored: list[str] = []

    class FakeEngine:
        def __init__(self, config):
            pass

        def run_cycle(self, *, now, dry_run):
            return SimpleNamespace(outcomes=[], errors=["network down"], kill_switch=False,
                                   summary=lambda: "should not be used")

    monkeypatch.setattr(scheduler_mod, "Engine", FakeEngine)
    monkeypatch.setattr(scheduler_mod, "mirror_cycle", lambda *a, **k: mirrored.append("x"))
    auto = Automation(workspace, store, config_loader=_loader(workspace))

    result = auto.run_cycle(datetime(2026, 10, 25, tzinfo=UTC))

    assert result["status"] == "failed"
    assert mirrored == []


# ---------------------------------------------------------------------------
# weekly review job
# ---------------------------------------------------------------------------

def _review_payload() -> dict:
    return {
        "report_id": "ta-000000000001",
        "run_labels": [
            # The newest run is ETH with 5 trades; BTC carries 22 across two runs,
            # so the review must target BTC even though ETH comes first.
            {"run_id": "run-b", "symbol": "ETH/USDT", "timeframe": "4h", "dataset_id": "ds-eth",
             "trades": 5},
            {"run_id": "run-a", "symbol": "BTC/USDT", "timeframe": "4h", "dataset_id": "ds-btc",
             "trades": 12},
            {"run_id": "run-c", "symbol": "BTC/USDT", "timeframe": "4h", "dataset_id": "ds-btc",
             "trades": 10},
        ],
        "trade_count": 42,
        "discovery": {"hypotheses": [
            {"id": "h-a", "question": "q", "grid": {"atr_target_mult": [2.5, 3.5]}},
            {"id": "h-b", "question": "q", "grid": {"regime_gate": ["RANGING"]}},
            {"id": "h-c", "question": "q", "grid": {"atr_stop_mult": [2.4, 3.6]}},
            {"id": "h-d", "question": "q", "grid": {"atr_stop_mult": [2.2, 3.4]}},
        ]},
    }


def _claim(workspace: Path, dataset_id: str, hypothesis_id: str) -> None:
    path = workspace / "state" / "research" / "optimizer" / "opt-old000000001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": "opt-old000000001", "dataset_id": dataset_id,
        "experiments": [{"hypothesis_id": hypothesis_id}], "queue": [], "current": None,
    }), encoding="utf-8")


def test_review_skips_without_stored_backtests(workspace: Path):
    store = open_store(workspace)
    auto = Automation(workspace, store, config_loader=_loader(workspace))

    result = auto.run_review(datetime(2026, 10, 25, tzinfo=UTC))

    assert result["status"] == "skipped"
    assert "بک‌تست" in result["detail"]
    assert store.get_meta(scheduler_mod.REVIEW_REPORT) is None


def test_review_starts_loop_with_fresh_hypotheses(workspace: Path, monkeypatch):
    store = open_store(workspace)
    payload = _review_payload()
    _claim(workspace, "ds-btc", "h-a")
    captured: dict[str, object] = {}

    monkeypatch.setattr(trade_analysis, "build_report",
                        lambda store, limit_runs=10: json.loads(json.dumps(payload)))
    monkeypatch.setattr(trade_analysis, "save_report",
                        lambda root, body: workspace / "state" / f"{body['report_id']}.json")

    def fake_start(config, store, **kwargs):
        captured.update(kwargs)
        return {"run_id": "opt-new000000001"}

    monkeypatch.setattr(optimizer_mod, "start", fake_start)
    auto = Automation(workspace, store, config_loader=_loader(workspace))

    result = auto.run_review(datetime(2026, 10, 25, tzinfo=UTC))

    # The report's majority dataset (ds-btc, two runs) is the target and the
    # already-claimed h-a on that dataset is skipped.
    assert captured["dataset_id"] == "ds-btc"
    assert captured["symbol"] == "BTC/USDT"
    assert captured["timeframe"] == "4h"
    assert [item["id"] for item in captured["hypotheses"]] == ["h-b", "h-c", "h-d"]
    assert captured["max_experiments"] == 3
    assert result["status"] == "ok"
    assert result["loop_id"] == "opt-new000000001"
    assert result["report_id"] == "ta-000000000001"
    assert result["maturity"]["paper_trades"] == 0
    assert result["maturity"]["activation_date"] == "2026-10-25"
    assert store.get_meta(scheduler_mod.REVIEW_LOOP) == "opt-new000000001"


def test_review_target_falls_back_to_frequency(workspace: Path):
    store = open_store(workspace)
    auto = Automation(workspace, store, cycle_hours=24, review_days=7)
    payload = {"run_labels": [
        {"dataset_id": "ds-a", "symbol": "A/USDT", "timeframe": "4h"},
        {"dataset_id": "ds-b", "symbol": "B/USDT", "timeframe": "4h"},
        {"dataset_id": "ds-a", "symbol": "A/USDT", "timeframe": "4h"},
    ]}
    assert auto._review_target(payload) == ("ds-a", "A/USDT", "4h")


def test_review_handles_active_loop_gracefully(workspace: Path, monkeypatch):
    store = open_store(workspace)
    payload = _review_payload()
    monkeypatch.setattr(trade_analysis, "build_report",
                        lambda store, limit_runs=10: json.loads(json.dumps(payload)))
    monkeypatch.setattr(trade_analysis, "save_report",
                        lambda root, body: workspace / "state" / f"{body['report_id']}.json")

    def busy_start(config, store, **kwargs):
        raise ValueError("یک حلقه‌ی بهینه‌سازی دیگر در حال اجراست؛ اول آن را متوقف یا تمام کنید.")

    monkeypatch.setattr(optimizer_mod, "start", busy_start)
    auto = Automation(workspace, store, config_loader=_loader(workspace))

    result = auto.run_review(datetime(2026, 10, 25, tzinfo=UTC))

    assert result["status"] == "ok"
    assert result["loop_id"] is None
    assert "loop not started" in result["detail"]
    assert store.get_meta(scheduler_mod.REVIEW_REPORT) == "ta-000000000001"


# ---------------------------------------------------------------------------
# shipped endpoint
# ---------------------------------------------------------------------------

def test_automation_endpoint_reports_real_intervals(workspace: Path):
    _set_automation(workspace, 24, 7)
    client = TestClient(api_module.create_app(root=workspace))

    body = client.get(f"{BASE}/automation").json()

    assert body["enabled"] is False
    assert body["paper_cycle"]["interval_hours"] == 24.0
    assert body["paper_cycle"]["last_attempt_at"] is None
    assert body["research_review"]["interval_days"] == 7.0
    assert "PAPER-only" in body["guard"]
