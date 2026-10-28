"""The breadth audit has to keep two claims apart.

It exists because the research lake and the deployed allowlist are separately curated 100-symbol
lists: one claim is "the replayed cadence overstates the account", the other is "the account may
order symbols nobody ever backtested". The first is only true if unapproved symbols ever reach a
trade, so the split must be made on the symbol carried by each recorded trade, not on the row it
sits in. A test that cannot tell those two claims apart would pass either way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from paper_breadth_audit import (  # noqa: E402
    audit_artifact, canonical_allowlist, split_candidates, universe_overlap)

LIMITS = SimpleNamespace(max_trades_per_day=4, cooldown_minutes_after_loss=60)
ALLOW = {"BTC_USDT", "ETH_USDT", "LINK_USDT"}


def _trade(symbol: str, index: int, pnl: float) -> dict:
    return {"symbol": symbol, "entry_time": index * 7200, "exit_time": index * 7200 + 3600,
            "pnl": pnl, "fees": 0.05, "reason": "target" if pnl > 0 else "stop"}


def _artifact(tmp_path: Path, rows: list[dict], *, refusals: dict[str, int],
              timeframe: str = "1h", days: float = 4.0) -> Path:
    doc = {"summary": {
        "timeframe": timeframe, "cadence": "production", "counted_days_per_symbol": days,
        "symbols_replayed": len(rows), "decision_bars_total": 1000,
        "rejections_pooled": {f"{symbol} not in approved symbol list": count
                              for symbol, count in refusals.items()},
    }, "per_symbol": rows}
    path = tmp_path / f"frequency-{timeframe}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_the_allowlist_is_compared_in_the_same_shape_the_replay_uses():
    config = SimpleNamespace(symbols=["BTC/USDT", "link/usdt"])
    assert canonical_allowlist(config) == {"BTC_USDT", "LINK_USDT"}


def test_the_overlap_reports_both_directions_of_the_gap_separately():
    row = universe_overlap(allow={"BTC_USDT", "ETH_USDT", "CRO_USDT"},
                           lake={"BTC_USDT", "LINK_USDT"})
    assert row["both"] == 1 and row["allowlist"] == 3 and row["lake"] == 2
    assert row["approved_without_lake_data"] == ["CRO_USDT", "ETH_USDT"]
    assert row["in_lake_not_approved"] == ["LINK_USDT"]
    assert row["coverage_pct"] == round(100 / 3, 2)


def test_candidates_are_split_by_the_symbol_on_the_trade_not_the_row():
    rows = [{"symbol": "BTCUSDT", "closed_trades": 1,
             "trade_timeline": [_trade("LINKUSDT", 0, 1.0)]}]
    approved, blocked = split_candidates(rows, ALLOW)
    assert [t["symbol"] for t in approved] == ["LINKUSDT"] and blocked == []

    rows = [{"symbol": "BTCUSDT", "closed_trades": 1,
             "trade_timeline": [_trade("SANDUSDT", 0, 1.0)]}]
    approved, blocked = split_candidates(rows, ALLOW)
    assert approved == [] and [t["symbol"] for t in blocked] == ["SANDUSDT"]


def test_a_cell_whose_trades_are_all_approved_reproduces_its_own_schedule(tmp_path):
    trades = [_trade("BTCUSDT", i, 1.0 if i % 3 else -1.0) for i in range(10)]
    path = _artifact(tmp_path, [{"symbol": "BTCUSDT", "closed_trades": 10,
                                 "trade_timeline": trades},
                                {"symbol": "SANDUSDT", "closed_trades": 0, "trade_timeline": []}],
                    refusals={"SAND_USDT": 6})
    row = audit_artifact(path, allow=ALLOW, limits=LIMITS)
    assert row["candidates"]["not_approved"]["trades"] == 0
    assert row["candidates"]["approved"]["trades"] == 10
    assert row["schedules"]["all_1"] == row["schedules"]["approved_1"]
    assert row["symbols_replayed_not_approved"] == 1
    assert row["allowlist_refusal_ticks"] == 6
    assert row["risk_layer_rejections_are_unapproved_replayed_symbols"] is True


def test_a_refusal_for_an_approved_symbol_is_flagged_not_tolerated(tmp_path):
    """If the allowlist refuses a symbol it approves, one of the two lists is stale."""
    path = _artifact(tmp_path, [{"symbol": "BTCUSDT", "closed_trades": 1,
                                 "trade_timeline": [_trade("BTCUSDT", 0, 1.0)]}],
                    refusals={"BTC_USDT": 4})
    assert audit_artifact(path, allow=ALLOW, limits=LIMITS)[
        "risk_layer_rejections_are_unapproved_replayed_symbols"] is False


def test_candidate_quality_reports_a_profit_factor_of_none_instead_of_dividing_by_zero(tmp_path):
    trades = [_trade("BTCUSDT", i, 2.0) for i in range(3)]
    path = _artifact(tmp_path, [{"symbol": "BTCUSDT", "closed_trades": 3,
                                 "trade_timeline": trades}], refusals={})
    row = audit_artifact(path, allow=ALLOW, limits=LIMITS)
    assert row["candidates"]["all"]["profit_factor"] is None
    assert row["candidates"]["all"]["net_pnl_per_1000"] == 6.0
    assert row["candidates"]["all"]["fees"] == 0.15
    assert row["symbols_replayed_not_approved"] == 0
