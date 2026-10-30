"""The allowlist universe must be provable about what it selected and what it dropped.

D-026 measures cadence on the symbols the deployed engine may actually order. That only means
something if the basket is exactly `config.yaml -> trading.symbols`, the availability rule is the
same one the ranked research universe used, and the existing universe file cannot be clobbered.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from build_allowlist_universe import (  # noqa: E402
    build_universe, deployed_allowlist, main, probe_months)


def test_the_basket_is_the_deployed_allowlist_in_vision_spelling():
    config = SimpleNamespace(symbols=("BTC_USDT", "ASTER_USDT", "m_usdt", "BTC_USDT"))
    assert deployed_allowlist(config) == ["ASTERUSDT", "BTCUSDT", "MUSDT"]


def test_cached_probes_are_used_and_an_unproven_month_excludes_the_symbol():
    cache = {"AAAUSDT": {"2026-05": 10, "2026-06": 20}, "BBBUSDT": {"2026-05": 5}}
    sizes, failures = probe_months(["AAAUSDT", "BBBUSDT"], ["2026-05", "2026-06"],
                                   proxy=None, cache=dict(cache), offline=True)
    assert sizes == {"AAAUSDT": {"2026-05": 10, "2026-06": 20}}
    assert failures == {"BBBUSDT": "offline and no cached probe for 2026-06"}


def test_a_probed_absent_month_is_reported_as_missing_not_unproven(monkeypatch):
    monkeypatch.setattr("build_allowlist_universe._probe",
                        lambda symbol, month, proxy, retries=3: None)
    sizes, failures = probe_months(["AAAUSDT"], ["2026-05", "2026-06"], proxy=None,
                                   cache={"AAAUSDT": {"2026-05": 10}}, offline=False)
    assert sizes == {} and failures == {"AAAUSDT": "no monthly archive for 2026-06"}

    def empty_archive(symbol, month, proxy, retries=3):
        return 0 if month == "2026-06" else 10

    monkeypatch.setattr("build_allowlist_universe._probe", empty_archive)
    sizes, failures = probe_months(["AAAUSDT"], ["2026-05", "2026-06"], proxy=None,
                                   cache={"AAAUSDT": {"2026-05": 10}}, offline=False)
    assert sizes == {"AAAUSDT": {"2026-05": 10, "2026-06": 0}} and failures == {}


def test_offline_refuses_to_invent_a_probe_rather_than_guessing_coverage():
    sizes, failures = probe_months(["CCCUSDT"], ["2026-05"], proxy=None, cache={}, offline=True)
    assert sizes == {} and failures["CCCUSDT"].startswith("offline")


def _payload(**kwargs):
    return build_universe(symbols=kwargs.pop("symbols"), months=["2026-05"],
                          research_universe=["BTCUSDT", "ETHUSDT"], config_path="config.yaml", **kwargs)


def test_the_payload_keeps_the_same_schema_and_names_every_exclusion():
    payload = _payload(symbols=["BTCUSDT", "NEWUSDT", "GONEUSDT"],
                       sizes={"BTCUSDT": {"2026-05": 11}, "NEWUSDT": {"2026-05": 12}},
                       failures={"GONEUSDT": "no monthly archive for 2026-05"})
    assert payload["schema_version"] == "quant-universe-v1"
    assert payload["symbols"] == ["BTCUSDT", "NEWUSDT"]
    assert payload["selection"] == {**payload["selection"], "requested": 3, "count": 2,
                                    "months": ["2026-05"]}
    assert [e["symbol"] for e in payload["probe_errors"]] == ["GONEUSDT"]
    assert payload["source"]["ranking"].startswith("none")
    assert payload["entries"][1]["binance_symbol"] == "NEWUSDT"
    assert any("deployed allowlist" in text for text in payload["limitations"])
    assert any("ranked research universe" in text for text in payload["limitations"])


def test_the_builder_refuses_to_overwrite_the_cited_research_universe(capsys):
    # A clean checkout has no ignored runtime state. Seed a disposable sentinel so this
    # safety test proves the refusal path without requiring or modifying a real universe.
    target = ROOT / "state" / "quant" / "universe.json"
    existed = target.exists()
    if not existed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"fixture":"must remain untouched"}\\n')
    before = target.read_bytes()
    try:
        exit_code = main(["--out", str(target)])
        assert exit_code == 3
        assert "refusing to overwrite" in capsys.readouterr().out
        assert target.read_bytes() == before
    finally:
        if not existed:
            target.unlink(missing_ok=True)
            for parent in (target.parent, target.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break
