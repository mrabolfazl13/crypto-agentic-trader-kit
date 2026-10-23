"""Offline fixtures and workspace builders for the TSE market tests.

Every payload comes from ``tests/fixtures/tse_*.json`` recorded from the real
TSETMC CDN; nothing here reaches the network.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from cat.markets.tse import InstrumentIndex, TseProvider  # noqa: E402

import build_tse_universe as builder  # noqa: E402

FOLD_CODE = "46348559193224090"
FOLD_TICKER = "فولاد"
FOLD_ISIN = "IRO1FOLD0001"

_ROUTES = (
    ("MarketWatch", "marketwatch", "tse_marketwatch_m1.json"),
    ("DailyList", "daily", "tse_daily_foolad.json"),
    ("BestLimits", "book", "tse_bestlimits_foolad.json"),
    ("GetMarketOverview", "overview", "tse_marketoverview_f1.json"),
    ("InstrumentSearch", "search", "tse_search_foolad.json"),
)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient:
    """Just enough of ``HttpClient``: URL routing, no sockets."""

    def __init__(self, overrides: dict[str, dict[str, Any]] | None = None,
                 date_epoch: float = 1_793_000_000.0):
        self.overrides = overrides or {}
        self.date_epoch = date_epoch
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((url, dict(params or {})))
        for marker, key, filename in _ROUTES:
            if marker in url:
                payload = self.overrides.get(key)
                return copy.deepcopy(payload if payload is not None else load_fixture(filename))
        raise AssertionError(f"unexpected TSETMC URL in test: {url}")

    def date_header_epoch(self, url: str) -> float:
        return self.date_epoch


def marketwatch_rows() -> list[dict[str, Any]]:
    return load_fixture("tse_marketwatch_m1.json")["marketwatch"]


def write_universe(workspace: Path, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Write a real-schema universe manifest built from the recorded board."""
    rows = marketwatch_rows() if rows is None else rows
    instruments, _excluded = builder.build_instruments(rows, limit=0)
    payload = {
        "generated_at": "2026-10-24T00:00:00+00:00",
        "source": {
            "endpoint": "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch",
            "origin": "fixture:tests/fixtures/tse_marketwatch_m1.json",
            "recorded_session_date": 20261021,
            "ranking": "trade_value_rial descending, then ticker",
            "board_rows": len(rows),
        },
        "count": len(instruments),
        "digest": builder.manifest_digest(instruments),
        "instruments": instruments,
    }
    (workspace / "universe_tse.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def write_tse_config(
    workspace: Path,
    *,
    symbols: tuple[str, ...] | None = (FOLD_TICKER,),
    mode: str = "PAPER",
    live: bool = False,
    max_symbols: int = 5,
) -> Path:
    raw = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    raw["trading"].update({
        "market": "tse",
        "mode": mode,
        "live_trading_enabled": live,
        "exchange": "tsetmc",
        "timeframe": "1d",
    })
    raw["trading"].pop("symbols", None)
    raw["data"]["confirm_timeframe"] = "1w"
    raw["data"]["fallback_exchange"] = ""
    raw["risk"]["stop_on_data_staleness_seconds"] = 345600
    raw["risk"]["max_spread_pct"] = 3.0
    raw["risk"]["allocated_capital"] = 1_000_000_000
    tse_options: dict[str, Any] = {
        "max_symbols": max_symbols,
        "watch_ttl_seconds": 0,
        "fees": {"maker_pct": 0.325, "taker_pct": 0.325},
    }
    if symbols is not None:
        tse_options["symbols"] = list(symbols)
    raw["markets"] = {"tse": tse_options}
    path = workspace / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def fixture_provider(workspace: Path, overrides: dict[str, dict[str, Any]] | None = None) -> TseProvider:
    index = InstrumentIndex(workspace / "universe_tse.json").load()
    assert index.rows, index.error
    return TseProvider(FixtureClient(overrides), index=index, fee_pct=0.325, watch_ttl_seconds=0)
