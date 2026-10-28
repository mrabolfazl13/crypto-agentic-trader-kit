"""A breadth comparison is only honest while the protocol behind both rows is identical.

The tool reads replay artifacts already on disk, so the tests below are about two failure
modes seen in this project's history: silently comparing two runs that differ in something
other than the basket (D-020's window/decision-clock mistakes), and reading a trade count as
an improvement without putting profit factor next to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from paper_breadth_compare import compare_rows, main, summarize_run  # noqa: E402


def _payload(**overrides):
    summary = {
        "generated_at": "2026-10-28T00:00:00+00:00",
        "timeframe": "4h",
        "cadence": "production",
        "window_bars": 300,
        "counted_from": "2026-05-01",
        "counted_to": "2026-08-01",
        "counted_days_per_symbol": 92.0,
        "production_cycle_hours": 3.0,
        "classifier": {"name": "trend-pullback", "version": "v1", "vol_threshold": 1.0},
        "symbols_replayed": 66,
        "symbols_skipped": [],
        "closed_trades_total": 42,
        "pooled_trades_per_day_upper_bound": 0.4565,
        "single_account_schedule": {"taken": 17, "trades_per_day": 0.1848, "profit_factor": 1.173,
                                    "net_pnl_per_1000": 1.9721, "win_rate_pct": 35.29,
                                    "days_to_30_trades": 162.35,
                                    "skipped": {"overlapping_position": 24}},
        "data_version": {"manifest": "abc", "symbols": 103, "rows": 31725150},
    }
    summary.update(overrides)
    return {"summary": summary, "per_symbol": []}


def test_summarize_flattens_the_deciding_fields():
    row = summarize_run(_payload())
    assert row["serial_trades_per_day"] == 0.1848
    assert row["profit_factor"] == 1.173
    assert row["lake_manifest"] == "abc"
    # the classifier identity has to survive flattening: it is what makes two rows comparable
    assert "trend-pullback" in row["classifier_key"] and "1.0" in row["classifier_key"]


def test_rows_with_different_protocol_are_not_compared():
    base = summarize_run(_payload())
    wider = summarize_run(_payload(window_bars=240))
    verdict = compare_rows(base, wider)
    assert verdict["comparable"] is False
    assert verdict["protocol_mismatch"]["window_bars"] == [300, 240]
    # no ratio is emitted at all, so a stale artifact cannot be quoted as a breadth effect
    assert "serial_trades_per_day_ratio" not in verdict


def test_different_classifier_params_block_the_comparison_even_when_dates_match():
    base = summarize_run(_payload())
    other = summarize_run(_payload(classifier={"name": "trend-pullback", "version": "v1",
                                               "vol_threshold": 1.5}))
    mismatch = compare_rows(base, other)["protocol_mismatch"]
    assert list(mismatch) == ["classifier_key"]
    assert mismatch["classifier_key"][0] != mismatch["classifier_key"][1]


def test_locked_rows_report_cadence_ratio_next_to_quality_delta():
    base = summarize_run(_payload())
    other = summarize_run(_payload(symbols_replayed=69, closed_trades_total=50,
                                   pooled_trades_per_day_upper_bound=0.5435,
                                   single_account_schedule={
                                       "taken": 21, "trades_per_day": 0.2283, "profit_factor": 0.9,
                                       "net_pnl_per_1000": -4.0, "win_rate_pct": 30.0,
                                       "days_to_30_trades": 131.4,
                                       "skipped": {"overlapping_position": 30}}))
    verdict = compare_rows(base, other)
    assert verdict["comparable"] is True
    assert verdict["breadth_ratio"] == round(69 / 66, 4)
    assert verdict["serial_trades_per_day_ratio"] == round(0.2283 / 0.1848, 4)
    # buying cadence while expectancy falls has to be visible in the same object
    assert verdict["profit_factor_delta"] == round(0.9 - 1.173, 4)
    assert verdict["net_pnl_per_1000_delta"] == round(-4.0 - 1.9721, 4)


def test_main_writes_rows_and_pairs_from_paths(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    for handle, payload in ((a, _payload()), (b, _payload(symbols_replayed=69))):
        handle.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "cmp" / "compare.json"
    code = main(["--cell", f"research={a}", "--cell", f"allowlist={b}",
                 "--pair", "research", "allowlist", "--out", str(out)])
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert set(report["rows"]) == {"research", "allowlist"}
    assert report["pairs"][0]["comparable"] is True


def test_main_refuses_a_pair_naming_an_unknown_label(tmp_path, capsys):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(_payload()), encoding="utf-8")
    out = tmp_path / "cmp" / "compare.json"
    code = main(["--cell", f"research={a}", "--pair", "research", "missing", "--out", str(out)])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["pairs"] == []
    assert "missing label" in capsys.readouterr().err
