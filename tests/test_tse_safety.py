"""Safety gates for the TSE market: PAPER-only, allowlist, read-only API.

The crypto suite already proves no live path exists. These tests repeat that
claim for the new market and add the venue-specific refusals (unknown ticker,
missing universe, no order route).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cat.api as api_module
from cat.config import ConfigError, load_config
from tse_helpers import (
    FOLD_CODE,
    FOLD_TICKER,
    fixture_provider,
    write_tse_config,
    write_universe,
)

BASE = "/api/v1"


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


def _walk_routes(routes):
    for route in routes:
        yield route
        for attr in ("routes", "original_router", "router"):
            child = getattr(route, attr, None)
            if child is not None and hasattr(child, "routes"):
                yield from _walk_routes(child.routes)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_tse_config_is_paper_only_with_a_numeric_allowlist(workspace: Path):
    write_universe(workspace)
    write_tse_config(workspace)

    config = load_config(root=workspace)

    assert config.market == "tse"
    assert config.mode == "PAPER"
    assert config.live_trading_enabled is False
    assert config.symbols == (FOLD_CODE,)
    assert config.timeframe == "1d"
    assert config.confirm_timeframe == "1w"
    assert config.maker_fee_pct == pytest.approx(0.325)
    assert config.taker_fee_pct == pytest.approx(0.325)


def test_live_flag_on_tse_still_loads_as_paper(workspace: Path):
    write_universe(workspace)
    write_tse_config(workspace, mode="LIVE", live=True)

    config = load_config(root=workspace)

    assert config.mode == "PAPER"
    assert config.warnings  # the operator is told why the mode was overridden


def test_short_margin_and_leverage_are_refused_for_tse(workspace: Path):
    write_universe(workspace)
    path = write_tse_config(workspace)
    text = path.read_text(encoding="utf-8")
    text = text.replace("allow_short: false", "allow_short: true")
    text = text.replace("allow_margin: false", "allow_margin: true")
    text = text.replace("allow_leverage: false", "allow_leverage: true")
    path.write_text(text, encoding="utf-8")

    config = load_config(root=workspace)

    assert config.allow_short is False
    assert config.allow_margin is False
    assert config.allow_leverage is False
    assert any("short selling" in warning for warning in config.warnings)


def test_unknown_tse_ticker_is_refused_not_guessed(workspace: Path):
    write_universe(workspace)
    write_tse_config(workspace, symbols=("NOT_A_TSE_TICKER",))

    with pytest.raises(ConfigError, match="not in universe_tse.json"):
        load_config(root=workspace)


def test_missing_universe_blocks_tse_instead_of_opening_it(workspace: Path):
    write_tse_config(workspace, symbols=None)

    with pytest.raises(ConfigError, match="build_tse_universe"):
        load_config(root=workspace)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_tse_routes_refuse_to_run_on_the_crypto_market(workspace: Path):
    app = api_module.create_app(root=workspace)
    client = TestClient(app)

    response = client.get(f"{BASE}/tse/market")

    assert response.status_code == 409
    assert "بورس" in response.json()["detail"]


def test_tse_api_is_read_only_and_reports_provenance(workspace: Path, monkeypatch):
    write_universe(workspace)
    write_tse_config(workspace)
    provider = fixture_provider(workspace)
    monkeypatch.setattr(api_module, "_gate_provider", lambda _config: provider)

    app = api_module.create_app(root=workspace)
    client = TestClient(app)

    market = client.get(f"{BASE}/tse/market")
    assert market.status_code == 200
    body = market.json()
    assert body["provenance"]["single_source"] is True
    assert body["allowlist"] == [FOLD_CODE]
    assert body["board_size"] == 1017
    assert body["board"]["activity_date"] == 20261021
    assert body["session"]["state"] in {"open", "closed", "pre_open"}

    instruments = client.get(f"{BASE}/tse/instruments", params={"limit": 10})
    assert instruments.status_code == 200
    listing = instruments.json()
    assert listing["count"] == 1
    assert listing["instruments"][0]["ins_code"] == FOLD_CODE
    assert listing["instruments"][0]["ticker"] == FOLD_TICKER
    assert listing["provenance"]["single_source"] is True

    instrument = client.get(f"{BASE}/tse/instrument", params={"symbol": FOLD_TICKER})
    assert instrument.status_code == 200
    detail = instrument.json()
    assert detail["display"]["insCode"] == FOLD_CODE
    assert detail["band"]["band_low"] == 4210.0
    assert detail["venue_rules"]["lot_size"] == 100.0

    book = client.get(f"{BASE}/tse/book", params={"symbol": FOLD_TICKER})
    assert book.status_code == 200
    assert book.json()["count"] == 5

    unknown = client.get(f"{BASE}/tse/instrument", params={"symbol": "NOPE"})
    assert unknown.status_code == 404


def test_tse_routes_are_get_only_and_no_path_can_go_live(workspace: Path, monkeypatch):
    write_universe(workspace)
    write_tse_config(workspace)
    provider = fixture_provider(workspace)
    monkeypatch.setattr(api_module, "_gate_provider", lambda _config: provider)

    app = api_module.create_app(root=workspace)
    paths = _walk_paths(app.routes)

    assert not any("live" in path.lower() for path in paths)
    assert any(path.startswith(f"{BASE}/tse/") for path in paths)
    assert f"{BASE}/kill-switch" in paths

    tse_routes = [route for route in _walk_routes(app.routes)
                  if str(getattr(route, "path", "")).startswith(f"{BASE}/tse/")]
    assert tse_routes
    for route in tse_routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert methods <= {"GET", "HEAD"}, (route.path, methods)

    client = TestClient(app)
    assert client.post(f"{BASE}/tse/market").status_code == 405
