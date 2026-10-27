"""Pure-function tests for the frequency replay's symbol normalizer and its bounds.

The replay speaks to `cat.risk.evaluate`, which rejects any symbol outside the
allowlist. Stored ids are `BTCUSDT`, the allowlist is `BTC_USDT`, so a normalizer
that silently returns the stored id turns "the gate refused" into "the allowlist
refused" — and the cadence this tool exists to measure comes out too low.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from paper_frequency_replay import MIN_WINDOW_BARS, canonical_symbol  # noqa: E402


@pytest.mark.parametrize("stored, expected", [
    ("BTCUSDT", "BTC_USDT"),
    ("LINKUSDT", "LINK_USDT"),
    ("AAVEUSDT", "AAVE_USDT"),
    ("ASTERUSDT", "ASTER_USDT"),
    ("1000PEPEUSDT", "1000PEPE_USDT"),
    ("ETHFDUSD", "ETH_FDUSD"),
    ("BTCUSDC", "BTC_USDC"),
    ("SOLUSD", "SOL_USD"),
])
def test_every_quote_suffix_is_stripped_regardless_of_base_length(stored: str, expected: str):
    assert canonical_symbol(stored) == expected


def test_a_base_longer_than_three_letters_is_never_left_unchanged():
    """The first two runs of the tool dropped LINK/AAVE/DOGE/ASTER by a length heuristic."""
    for stored in ("LINKUSDT", "DOGEUSDT", "AAVEUSDT", "ASTERUSDT", "PUMPUSDT", "NEARUSDT"):
        assert canonical_symbol(stored) != stored
        assert canonical_symbol(stored).endswith("_USDT")


def test_an_id_without_a_known_quote_passes_through_unchanged():
    assert canonical_symbol("BTC") == "BTC"


def test_the_replay_refuses_a_window_smaller_than_production_uses():
    assert MIN_WINDOW_BARS == 300


def test_every_replayed_lake_symbol_exists_in_the_deployed_allowlist():
    """The failure this guards is silent: an unmapped id is rejected by risk.py:95 and
    counted as 'the build refused to trade', which lowers the measured cadence.

    Skipped where the candle lake is not installed.
    """
    from pathlib import Path

    from cat.config import load_config
    from cat.quant.settings import load_quant_settings
    from cat.quant.storage import universe_symbols

    try:
        config = load_config(root=Path(__file__).resolve().parents[1])
        settings = load_quant_settings(config)
        symbols = universe_symbols(settings, limit=30)
    except Exception as exc:  # no lake on this machine
        pytest.skip(f"lake unavailable: {type(exc).__name__}: {exc}")
    if not symbols:
        pytest.skip("lake has no universe")
    unmapped = [stored for stored in symbols if canonical_symbol(stored) not in config.symbols]
    assert unmapped == [], f"would be rejected by the allowlist: {unmapped}"
