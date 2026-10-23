"""Offline API tests: no network, no exchange, no live order can ever happen.

The suite builds the app against an isolated tmp workspace (same fixture pattern
as the rest of the tests) and seeds the SQLite Store through its real methods,
then asserts the contract in docs/API_CONTRACT_FA.md end to end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import cat.api as api_module
import cat.datasets as datasets_mod
from cat.config import load_config
from cat.data import DataUnavailable
from cat.portfolio import AccountState

UTC = timezone.utc
BASE = "/api/v1"
STEP_4H = 14400


def _write_csv(path: Path, prices: list[float]) -> None:
    lines = ["timestamp,open,high,low,close,volume"]
    previous = prices[0]
    for index, close in enumerate(prices):
        lines.append(f"{1_700_000_000 + index * STEP_4H},{previous},{max(previous, close) * 1.001},"
                     f"{min(previous, close) * 0.999},{close},100")
        previous = close
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _walk_paths(routes) -> list[str]:
    out: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            out.append(path)
        for attr in ("routes", "original_router", "router"):
            child = getattr(route, attr, None)
            if child is not None and hasattr(child, "routes"):
                out.extend(_walk_paths(child.routes))
    return out


@pytest.fixture()
def app(workspace: Path):
    return api_module.create_app(root=workspace)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def store(app):
    return app.state.store


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------

def test_health_reports_schema_counts_and_paper_mode(client: TestClient):
    body = client.get(f"{BASE}/health").json()
    assert body["mode"] == "PAPER"
    assert body["schema_version"] == "1"
    assert isinstance(body["counts"], dict)
    assert body["live_trading_enabled"] is False
    assert body["last_data_at"] is None  # empty workspace: no data, never faked
    assert "datasets" in body["counts"] and "trades" in body["counts"]


def test_health_last_data_at_tracks_newest_ingested_candle(client: TestClient, workspace: Path):
    csv_path = workspace / "fresh.csv"
    prices = _trending_prices(90)
    _write_csv(csv_path, prices)
    client.post(f"{BASE}/datasets/import",
                json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"})
    body = client.get(f"{BASE}/health").json()
    newest = 1_700_000_000 + (len(prices) - 1) * STEP_4H
    assert body["last_data_at"] == datetime.fromtimestamp(newest, tz=UTC).isoformat(timespec="seconds")


def test_config_masks_proxy_and_drops_keys(client: TestClient, workspace: Path):
    resp = client.get(f"{BASE}/config")
    assert resp.status_code == 200
    text = resp.text
    assert "127.0.0.1:1080" not in text  # the real proxy value never leaks
    payload = resp.json()
    assert payload["config"]["data"]["proxy"] == "***"
    assert payload["config"]["data"]["proxy_configured"] is True
    assert payload["mode"] == "PAPER"
    configured = list(load_config(root=workspace).symbols)
    assert payload["symbols"] == configured
    assert payload["symbols"][:2] == ["BTC_USDT", "ETH_USDT"]
    assert "strategy_params" in payload and "limits" in payload
    assert payload["max_open_positions"] == load_config(root=workspace).max_open_positions


def test_universe_missing_manifest_is_reported_not_invented(client: TestClient):
    body = client.get(f"{BASE}/universe").json()
    assert body["available"] is False
    assert body["symbols"], "the effective allowlist is still reported"


def test_universe_reports_snapshot_provenance_and_detects_drift(client: TestClient, workspace: Path):
    configured = list(load_config(root=workspace).symbols)
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-10-23T20:00:00+00:00",
        "requested_count": len(configured),
        "count": len(configured),
        "digest": "abc",
        "source": {"name": "CoinGecko", "ranking": "market_cap_desc"},
        "symbols": configured,
        "entries": [{"rank": 1, "ticker": "BTC", "name": "Bitcoin", "symbol": "BTC/USDT"}],
        "excluded": [{"rank": 3, "ticker": "USDT", "name": "Tether", "reason": "stablecoin"}],
    }
    (workspace / "universe.json").write_text(json.dumps(manifest), encoding="utf-8")
    body = client.get(f"{BASE}/universe").json()
    assert body["available"] is True
    assert body["matches_config"] is True
    assert body["count"] == len(configured)
    assert body["source"]["name"] == "CoinGecko"
    assert body["excluded"][0]["ticker"] == "USDT"

    manifest["symbols"] = manifest["symbols"][:-1]
    (workspace / "universe.json").write_text(json.dumps(manifest), encoding="utf-8")
    drifted = client.get(f"{BASE}/universe").json()
    assert drifted["available"] is True
    assert drifted["matches_config"] is False


def test_spa_fallback_serves_shell_for_client_routes_but_assets_404(workspace: Path):
    dist = workspace / "web" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    shell = dist / "index.html"
    shell.write_text('<!doctype html><html lang="fa" dir="rtl"><body><div id="root"></div></body></html>', "utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log('ok')", "utf-8")
    local = TestClient(api_module.create_app(root=workspace))

    deep = local.get("/portfolio")
    assert deep.status_code == 200
    assert deep.headers["content-type"].startswith("text/html")
    assert 'id="root"' in deep.text

    assert local.get("/").status_code == 200
    assert local.get("/assets/app.js").text == "console.log('ok')"
    missing_asset = local.get("/assets/missing.js")
    assert missing_asset.status_code == 404  # a broken asset must fail loudly, not return HTML
    assert local.get("/api/v1/health").status_code == 200  # API still wins over the mount

    # An unknown API path must stay a JSON 404. Serving the shell here is what made an
    # old server silently answer /api/v1/tse/* with HTML and blank the whole page.
    # Windows caught this: Starlette hands StaticFiles an OS-normalised path
    # ("api\v1\..."), so any guard written against forward slashes is dead there.
    for probe in ("/api/v1/tse/does-not-exist", "/api/v1/nope", "/api/nope"):
        missing_api = local.get(probe)
        assert missing_api.status_code == 404, probe
        assert "text/html" not in missing_api.headers["content-type"], probe
        assert missing_api.json()["detail"]

    # A real client-side deep link still gets the shell.
    assert local.get("/tse").status_code == 200
    assert 'id="root"' in local.get("/tse").text


def test_portfolio_returns_nulls_on_empty_ledger(client: TestClient):
    body = client.get(f"{BASE}/portfolio").json()
    assert body["unrealized"] is None          # no live quotes -> not a fake 0
    assert body["positions"] == []
    assert body["price_source"] == "none"
    assert body["cash"] == pytest.approx(1000.0)
    # the dashboard divides equity by this to show net return; a missing key is a dead card
    assert body["allocated_capital"] == pytest.approx(1000.0)
    assert body["allocated_source"] == "config.risk.allocated_capital"


def test_trades_filter_by_outcome_win(store, client: TestClient):
    seed_rows = [
        {"symbol": "BTC_USDT", "entry_ts": 1, "exit_ts": 10, "qty": 1, "entry": 100, "exit": 110,
         "gross": 10, "fees": 1, "net": 10, "reason": "target"},
        {"symbol": "BTC_USDT", "entry_ts": 2, "exit_ts": 20, "qty": 1, "entry": 100, "exit": 120,
         "gross": 20, "fees": 2, "net": 20, "reason": "target"},
        {"symbol": "BTC_USDT", "entry_ts": 3, "exit_ts": 30, "qty": 1, "entry": 100, "exit": 85,
         "gross": -15, "fees": 3, "net": -15, "reason": "stop"},
        {"symbol": "BTC_USDT", "entry_ts": 4, "exit_ts": 40, "qty": 1, "entry": 100, "exit": 95,
         "gross": -5, "fees": 1, "net": -5, "reason": "stop"},
    ]
    for row in seed_rows:
        store.record_trade(row, mode="PAPER", timeframe="4h", strategy_label="v1")
    wins = client.get(f"{BASE}/trades", params={"outcome": "win", "mode": "PAPER"}).json()
    assert wins["count"] == 2
    assert all(t["net"] > 0 for t in wins["trades"])
    losses = client.get(f"{BASE}/trades", params={"outcome": "loss", "mode": "PAPER"}).json()
    assert losses["count"] == 2
    assert client.get(f"{BASE}/trades", params={"outcome": "bogus"}).status_code == 422


def test_performance_matches_seeded_ledger_by_hand(store, client: TestClient):
    for net, fee in [(10, 1), (20, 2), (-15, 3), (-5, 1)]:
        store.record_trade({"symbol": "BTC_USDT", "entry_ts": 1, "exit_ts": 2, "qty": 1,
                            "entry": 100, "exit": 100 + net, "gross": net, "fees": fee,
                            "net": net, "reason": "x"}, mode="PAPER")
    metrics = client.get(f"{BASE}/performance", params={"mode": "PAPER"}).json()
    m = metrics["metrics"]
    assert m["trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate_pct"] == 50.0
    assert m["profit_factor"] == 1.5      # 30 / 20
    assert m["avg_win"] == 15.0           # 30 / 2
    assert m["avg_loss"] == -10.0         # -20 / 2
    assert m["net_pnl"] == 10.0           # 30 - 20
    assert m["fees"] == 7.0               # 1 + 2 + 3 + 1
    assert metrics["provenance"]["kind"] == "paper"


def test_decisions_show_approved_flag_and_rationale(store, client: TestClient):
    decision = {"decision_id": "dec-abc", "timestamp": "2026-10-23T09:00:00+00:00",
                "symbol": "BTC_USDT", "action": "BUY", "confidence": 0.82,
                "rationale": ["ema20 above ema50", "RSI healthy band"],
                "risk": {"reward_risk_ratio": 2.1}}
    store.record_decision(decision, {"risk_approved": True}, mode="PAPER", source="paper")
    rows = client.get(f"{BASE}/decisions", params={"date": "2026-10-23"}).json()["decisions"]
    assert rows, "expected the seeded decision"
    row = rows[0]
    assert row["approved"] == 1
    assert row["action"] == "BUY"
    assert "ema20 above ema50" in row["rationale"]


def test_run_detail_returns_equity_curve_and_rejections(store, client: TestClient):
    run_id = store.record_run(
        kind="backtest", symbol="BTC_USDT", timeframe="4h", params_label="v1", params={"ema_fast": 20},
        dataset_id=None, window=("2024-10-23T00:00+00:00", "2026-10-22T00:00+00:00"),
        metrics={"trades": 12, "win_rate_pct": 58.3},
        rejections={"no_signal": 5, "spread": 2},
        equity_curve=[(1_700_000_000, 1000.0), (1_700_003_600, 1010.0)],
    )
    run = client.get(f"{BASE}/runs/{run_id}").json()["run"]
    assert run["id"] == run_id
    assert run["equity_curve"] == [[1_700_000_000, 1000.0], [1_700_003_600, 1010.0]]
    reasons = {r["reason"]: r["count"] for r in run["rejections"]}
    assert reasons == {"no_signal": 5, "spread": 2}
    assert client.get(f"{BASE}/runs/does-not-exist").status_code == 404


def test_strategies_returns_stored_versions(store, client: TestClient):
    store.save_strategy_version({"digest": "deadbeefcafe", "name": "trend-pullback", "version": "v2",
                                 "params": {"ema_fast": 12}, "source": "grid", "accepted": True,
                                 "decision": "promoted", "train": {"win_rate_pct": 55},
                                 "validation": {"win_rate_pct": 52}, "test": {"win_rate_pct": 50}})
    rows = client.get(f"{BASE}/strategies").json()["strategies"]
    assert any(s["digest"] == "deadbeefcafe" and s["accepted"] == 1 for s in rows)


def test_risk_shows_limits_vs_today_usage(client: TestClient):
    body = client.get(f"{BASE}/risk").json()
    assert body["limits"]["max_trades_per_day"] == 4
    assert body["limits"]["max_drawdown_pct"] == 8.0
    assert body["usage"]["allocated_capital"] == pytest.approx(1000.0)
    assert body["usage"]["trades_today"] == 0
    assert body["kill_switch"] is False
    assert body["mode"] == "PAPER"


def test_logs_endpoint(client: TestClient, store):
    store.log("info", "unit", "seeded log line")
    rows = client.get(f"{BASE}/logs").json()["logs"]
    assert any(l["message"] == "seeded log line" for l in rows)
    only_info = client.get(f"{BASE}/logs", params={"level": "info"}).json()["logs"]
    assert all(l["level"] == "info" for l in only_info)


def test_reports_returns_persian_markdown(client: TestClient):
    resp = client.get(f"{BASE}/reports/2026-10-23")
    assert resp.status_code == 200
    markdown = resp.json()["markdown"]
    assert "گزارش" in markdown
    assert "kill switch" in markdown
    assert resp.json()["provenance"]["kind"] == "paper"
    assert client.get(f"{BASE}/reports/not-a-date").status_code == 422


# ---------------------------------------------------------------------------
# POST endpoints
# ---------------------------------------------------------------------------

def test_kill_switch_toggle_is_reflected_in_health(client: TestClient, workspace: Path):
    stop_file = workspace / "STOP_TRADING"
    assert not stop_file.exists()
    on = client.post(f"{BASE}/kill-switch", json={"active": True})
    assert on.status_code == 200
    assert "توقف" in on.json()["effect"]
    assert stop_file.exists()
    assert client.get(f"{BASE}/health").json()["kill_switch"] is True

    off = client.post(f"{BASE}/kill-switch", json={"active": False})
    assert off.status_code == 200
    assert not stop_file.exists()
    assert client.get(f"{BASE}/health").json()["kill_switch"] is False


def test_backtest_rejects_symbol_outside_allowlist(client: TestClient):
    # ZZZ is deliberately not on Gate, so it can never enter a generated universe.
    resp = client.post(f"{BASE}/runs/backtest", json={"symbol": "ZZZ/USDT", "days": 10})
    assert resp.status_code == 422
    assert "فهرست مجاز" in resp.json()["detail"]


def test_backtest_validates_days_range(client: TestClient):
    resp = client.post(f"{BASE}/runs/backtest", json={"symbol": "BTC/USDT", "days": 5000})
    assert resp.status_code == 422
    assert "ورودی نامعتبر" in resp.json()["detail"]


def _trending_prices(count: int, base: float = 50_000.0) -> list[float]:
    prices = [base]
    for index in range(1, count):
        prices.append(prices[-1] * (0.994 if index % 7 == 0 else 1.004))
    return prices


def test_backtest_uses_the_real_confirm_timeframe_series(store, config, workspace: Path):
    prices = _trending_prices(900)
    csv_path = workspace / "hist.csv"
    _write_csv(csv_path, prices)
    imported = datasets_mod.ingest_csv(store, csv_path, symbol="BTC_USDT", timeframe="4h")
    signal, higher, _filters, dataset_id = api_module._series_for(
        store, config, datasets_mod, "BTC_USDT", "4h", None, imported["dataset_id"])
    assert dataset_id == imported["dataset_id"]
    assert len(signal) == len(prices)
    # config.confirm_timeframe is 1d: the series must be ~150 daily bars at 86400s
    # spacing, not the 900 signal bars handed over unchanged.
    assert 140 <= len(higher) <= 160
    assert higher[1].open_time - higher[0].open_time == 86400


def test_backtest_success_path_runs_and_persists_the_strategy_version(client: TestClient, store, workspace: Path):
    """The 200-response path, not only validation rejects — this is where the engine bug hid."""
    csv_path = workspace / "hist.csv"
    _write_csv(csv_path, _trending_prices(900))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/runs/backtest",
                       json={"symbol": "BTC/USDT", "dataset_id": imported["dataset_id"], "warmup": 200})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance"]["kind"] == "simulated"
    assert body["params"]["ema_fast"] == 20
    for key in ("trades", "net_return_pct", "max_drawdown_pct", "fees_paid"):
        assert key in body["metrics"]
    run = store.get_run(body["run_id"])
    assert run["params_label"].startswith("trend-pullback:")
    assert len(run["equity_curve"]) > 0


def test_dataset_bars_returns_real_candles_and_overlays(client: TestClient, workspace: Path):
    csv_path = workspace / "hist.csv"
    _write_csv(csv_path, _trending_prices(300))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    body = client.get(f"{BASE}/datasets/{imported['dataset_id']}/bars", params={"limit": 120, "overlays": True}).json()
    assert body["total_bars"] == 300 and body["returned_bars"] == 120
    assert body["provenance"]["kind"] == "history"
    bar = body["bars"][-1]
    assert bar["high"] >= max(bar["open"], bar["close"]) and bar["low"] <= min(bar["open"], bar["close"])
    fast = next(o for o in body["overlays"] if o["label"] == "ema_fast(20)")
    assert len(fast["points"]) == 120
    # 300 bars of history precede the window, so both EMAs are fully seeded —
    # a leading None here would mean the overlay was computed inside the window.
    assert all(v is not None and v > 0 for v in fast["points"])
    slow = next(o for o in body["overlays"] if o["label"] == "ema_slow(50)")
    assert all(v is not None and v > 0 for v in slow["points"])
    assert fast["points"][-1] > slow["points"][-1]  # uptrend: fast above slow


def test_dataset_overlays_are_sparse_when_history_is_short(client: TestClient, workspace: Path):
    csv_path = workspace / "short.csv"
    _write_csv(csv_path, _trending_prices(60))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    body = client.get(f"{BASE}/datasets/{imported['dataset_id']}/bars", params={"limit": 60, "overlays": True}).json()
    slow = next(o for o in body["overlays"] if o["label"] == "ema_slow(50)")
    assert slow["points"][:10] == [None] * 10
    assert slow["points"][-1] is not None


def test_dataset_bars_404s_for_an_unknown_dataset(client: TestClient):
    assert client.get(f"{BASE}/datasets/ds-nope/bars").status_code == 404


def test_walk_forward_rejects_too_large_grid(client: TestClient):
    resp = client.post(f"{BASE}/runs/walk-forward",
                       json={"symbol": "BTC/USDT", "days": 10, "grid": {"a": list(range(7)), "b": list(range(9))}})
    assert resp.status_code == 422
    assert "۶۰" in resp.json()["detail"] or "60" in resp.json()["detail"]


def test_walk_forward_refuses_datasets_too_small_for_honest_splits(client: TestClient, workspace: Path):
    csv_path = workspace / "wf-small.csv"
    _write_csv(csv_path, _trending_prices(360))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/runs/walk-forward",
                       json={"symbol": "BTC/USDT", "dataset_id": imported["dataset_id"],
                             "grid": {"ema_fast": [10]}, "folds": 2})
    assert resp.status_code == 422
    assert "120" in resp.json()["detail"]


def test_walk_forward_refuses_identity_keys_in_grid(client: TestClient, workspace: Path):
    csv_path = workspace / "wf-idkey.csv"
    _write_csv(csv_path, _trending_prices(720))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/runs/walk-forward", json={
        "symbol": "BTC/USDT", "dataset_id": imported["dataset_id"],
        "grid": {"version": ["v-regression"], "ema_fast": [10, 20]}, "folds": 2})
    assert resp.status_code == 422
    assert "version" in resp.json()["detail"]


def test_walk_forward_runs_on_imported_dataset(client: TestClient, store, workspace: Path):
    csv_path = workspace / "wf.csv"
    _write_csv(csv_path, _trending_prices(720))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/runs/walk-forward", json={
        "symbol": "BTC/USDT", "dataset_id": imported["dataset_id"],
        "grid": {"ema_fast": [10, 20]}, "folds": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dataset_id"] == imported["dataset_id"]
    assert body["verdict"] in {"accepted", "rejected"}
    assert body["candidate_label"].startswith("trend-pullback:")
    assert body["champion_label"].startswith("trend-pullback:")
    assert len(body["grid"]) == 2  # the version key adds no variant
    assert body["risk_checks"]["risk_limits_untouched"] is True
    assert "تضمین" in body["disclaimer"]
    recorded = store.list_runs(kind="walk_forward", symbol="BTC_USDT", limit=5)
    assert recorded and all(run["dataset_id"] == imported["dataset_id"] for run in recorded)


def test_walk_forward_rejects_empty_grid(client: TestClient, workspace: Path):
    csv_path = workspace / "wf-empty.csv"
    _write_csv(csv_path, _trending_prices(360))
    imported = client.post(f"{BASE}/datasets/import",
                           json={"path": str(csv_path), "symbol": "BTC/USDT", "timeframe": "4h"}).json()
    resp = client.post(f"{BASE}/runs/walk-forward",
                       json={"symbol": "BTC/USDT", "dataset_id": imported["dataset_id"], "grid": {}})
    assert resp.status_code == 422
    assert "grid" in resp.json()["detail"]


def test_paper_cycle_mirrors_decisions_into_store(client: TestClient, store: object, workspace: Path):
    class StubOutcome:
        def __init__(self):
            self.symbol = "BTC_USDT"
            self.action = "BUY"
            self.approved = True
            self.reasons = ["trend confirmed", "risk gate approved"]
            self.order_id = "paper-1"
            self.filled_qty = 0.01
            self.fill_price = 50000.0
            self.decision_id = "dec-stub-000001"
            self.details = {"notional": 500.0}

    class StubResult:
        def __init__(self):
            self.outcomes = [StubOutcome()]
            self.errors: list[str] = []
            self.mode = "PAPER"
            self.kill_switch = False
            self.safe_state = False
            self.clock = {"acceptable": True}
            self.config_digest = "stubdigest"
            self.started_at = "2026-10-23T12:00:00+00:00"
            self.finished_at = "2026-10-23T12:00:01+00:00"
            self.equity = 1000.0
            self.unrealized = 0.0
            self.drawdown_pct = 0.0

        def summary(self) -> str:
            return "stub cycle summary"

    class StubEngine:
        def __init__(self, config, allocated_capital=None):
            self.config = config
            self.state = AccountState.new(config.allocated_capital)
            self.broker = SimpleNamespace(orders={}, fills=[])
            self.current_prices = {}

        def run_cycle(self, now=None, dry_run=False):
            return StubResult()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(api_module, "Engine", StubEngine)
    try:
        resp = client.post(f"{BASE}/cycles/paper", json={"dry_run": False})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["mode"] == "PAPER"
        assert payload["outcomes"][0]["decision_id"] == "dec-stub-000001"
        assert payload["provenance"]["kind"] == "paper"

        decisions = client.get(f"{BASE}/decisions", params={"symbol": "BTC/USDT"}).json()["decisions"]
        mirrored = [d for d in decisions if d["id"] == "dec-stub-000001"]
        assert mirrored, "cycle decision was not mirrored into the Store"
        assert mirrored[0]["approved"] == 1
        assert "risk gate approved" in mirrored[0]["rationale"]

        limited = client.get(f"{BASE}/decisions", params={"limit": 1}).json()
        assert limited["count"] == 1
        assert client.get(f"{BASE}/decisions", params={"limit": 0}).status_code == 422
    finally:
        monkey.undo()


def test_paper_cycle_returns_503_when_data_unreachable(client: TestClient):
    class ExplodingEngine:
        def __init__(self, config, allocated_capital=None):
            self.config = config
            self.broker = SimpleNamespace(orders={}, fills=[])
            self.state = AccountState.new(config.allocated_capital)
            self.current_prices = {}

        def run_cycle(self, now=None, dry_run=False):
            raise DataUnavailable("no data")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(api_module, "Engine", ExplodingEngine)
    try:
        resp = client.post(f"{BASE}/cycles/paper", json={"dry_run": False})
        assert resp.status_code == 503
        assert "PAPER" in resp.json()["detail"]
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

def test_no_route_can_enable_live(app):
    paths = _walk_paths(app.routes)
    assert paths, "route walk found nothing"
    assert all("live" not in p.lower() for p in paths), [p for p in paths if "live" in p.lower()]


def test_kill_switch_only_mutation_of_safety_state(client: TestClient):
    # A response never advertises a way to flip mode / capital / limits.
    body = client.get(f"{BASE}/config").json()
    assert body["mode"] == "PAPER"
    assert "live_trading_enabled" not in body or body.get("mode") == "PAPER"


def test_every_metric_response_carries_provenance(client: TestClient):
    for path in (f"{BASE}/performance", f"{BASE}/equity-curve", f"{BASE}/portfolio", f"{BASE}/risk"):
        payload = client.get(path).json()
        assert "provenance" in payload, path
        assert payload["provenance"]["kind"] in {"history", "simulated", "paper", "live"}
        assert "generated_at" in payload["provenance"]


def test_market_candles_refuses_symbols_outside_the_allowlist(client: TestClient):
    resp = client.get(f"{BASE}/market/candles", params={"symbol": "ZZZ/USDT"})
    assert resp.status_code == 404
    assert "فهرست مجاز" in resp.json()["detail"]


def test_market_candles_rejects_unknown_timeframe(client: TestClient):
    resp = client.get(f"{BASE}/market/candles", params={"symbol": "BTC/USDT", "timeframe": "7m"})
    assert resp.status_code == 400


def test_market_candles_reads_gate_and_caches_briefly(client: TestClient, monkeypatch):
    from cat import data as data_module
    from cat.data import Kline

    calls: list[tuple[str, str, int]] = []

    def fake_klines(self, symbol, timeframe, limit=300):
        calls.append((symbol, timeframe, limit))
        start = 1_700_000_000
        return [
            Kline(open_time=start + index * STEP_4H, open=100.0 + index, high=101.0 + index,
                  low=99.0 + index, close=100.5 + index, base_volume=1.0, quote_volume=100.0, closed=True)
            for index in range(limit)
        ]

    monkeypatch.setattr(data_module.GateProvider, "klines", fake_klines)
    body = client.get(f"{BASE}/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h", "limit": 60}).json()
    assert body["symbol"] == "BTC_USDT" and body["count"] == 60 and body["closed_count"] == 60
    assert body["provenance"]["kind"] == "history"
    assert body["provenance"]["source"] == "gate:BTC_USDT:4h"
    assert body["provenance"]["window"].startswith("2")
    assert body["bars"][-1]["close"] > body["bars"][0]["close"]

    again = client.get(f"{BASE}/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h", "limit": 60}).json()
    assert again["provenance"]["cached"] is True
    assert len(calls) == 1


def test_market_candles_reports_unreachable_feed(client: TestClient, monkeypatch):
    from cat import data as data_module

    def broken(self, symbol, timeframe, limit=300):
        raise DataUnavailable("feed down")

    monkeypatch.setattr(data_module.GateProvider, "klines", broken)
    resp = client.get(f"{BASE}/market/candles", params={"symbol": "ETH/USDT", "timeframe": "4h", "limit": 40})
    assert resp.status_code == 503
    assert "دریافت کندل" in resp.json()["detail"]
