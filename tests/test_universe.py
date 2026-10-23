"""Offline tests for the dated universe snapshot and its config surgery."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import build_universe as bu  # noqa: E402


def test_classification_is_explicit_about_what_is_not_tradable():
    assert bu.classification("USDT", "Tether") == "stablecoin"
    assert bu.classification("USDG", "Global Dollar") == "stablecoin"
    assert bu.classification("XAUT", "Tether Gold") == "tokenised commodity (gold/silver)"
    assert bu.classification("PAXG", "PAX Gold") == "tokenised commodity (gold/silver)"
    assert bu.classification("WBTC", "Wrapped Bitcoin") == "wrapped/staked duplicate"
    assert bu.classification("BTC", "Bitcoin") is None
    assert bu.classification("ETH", "Ethereum") is None


def test_ticker_collisions_are_checked_against_the_asset_name():
    assert bu.names_match("Bitcoin", "BTC", "Bitcoin")
    assert bu.names_match("LEO Token", "LEO", "UNUS-SED-LEO")
    assert bu.names_match("Bitcoin Cash", "BCH", "BitcoinCash")
    assert bu.names_match("Artificial Superintelligence Alliance", "FET", "Artificial Superintelligence Alliance")
    assert not bu.names_match("Some Other Asset", "XYZ", "Unrelated Token")


def test_symbol_block_replacement_touches_only_trading_symbols():
    original = (
        "trading:\n"
        "  mode: PAPER\n"
        "  symbols:\n"
        "    - BTC/USDT\n"
        "    - ETH/USDT\n"
        "  timeframe: 4h\n"
        "\n"
        "risk:\n"
        "  max_daily_loss_pct: 1.5\n"
    )
    updated = bu.replace_symbols_block(
        original, ["BTC/USDT", "SOL/USDT", "ADA/USDT"], fetched_at="2026-10-24T00:00:00+00:00", count=3
    )
    assert updated.count("    - ") == 3
    assert "    - SOL/USDT" in updated and "- ETH/USDT" not in updated
    assert "  mode: PAPER" in updated
    assert updated.endswith("risk:\n  max_daily_loss_pct: 1.5\n")


def test_build_filters_real_ranking_and_keeps_selection_order(monkeypatch):
    pairs = [
        {"id": f"{ticker}_USDT", "base": ticker, "base_name": name, "quote": "USDT", "trade_status": "tradable", "type": "normal"}
        for ticker, name in (("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("ADA", "Cardano"), ("RAIN", "Rain"))
    ]
    markets = [
        {"market_cap_rank": 1, "symbol": "btc", "name": "Bitcoin"},
        {"market_cap_rank": 2, "symbol": "usdt", "name": "Tether"},
        {"market_cap_rank": 3, "symbol": "eth", "name": "Ethereum"},
        {"market_cap_rank": 4, "symbol": "xmr", "name": "Monero"},  # not on Gate
        {"market_cap_rank": 5, "symbol": "ada", "name": "Cardano"},
        {"market_cap_rank": 6, "symbol": "rain", "name": "Rain"},
    ]

    def fake_fetch(url, **_kwargs):
        return pairs if "currency_pairs" in url else markets

    monkeypatch.setattr(bu, "fetch_json", fake_fetch)
    manifest = bu.build(3, 10, None)
    assert manifest["symbols"] == ["BTC/USDT", "ETH/USDT", "ADA/USDT"]
    assert manifest["count"] == 3
    assert any(item["ticker"] == "USDT" for item in manifest["excluded"])
    assert any(item["ticker"] == "XMR" for item in manifest["skipped"])
    assert manifest["entries"][0]["gate_base_name"] == "Bitcoin"
    assert len(manifest["digest"]) == 64
