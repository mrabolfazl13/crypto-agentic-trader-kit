"""Offline diagnostics: where do real-market proposals actually stop?

Usage: python tools/diagnose_signals.py [BTC_USDT] [bars]
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.backtest import _snapshot, select_closed_higher  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.data import seconds_per_bar  # noqa: E402
from cat.http import HttpClient  # noqa: E402
from cat.portfolio import AccountState  # noqa: E402
from cat.data import GateProvider  # noqa: E402
from cat.risk import evaluate  # noqa: E402
from cat.strategy import analyze, propose  # noqa: E402

config = load_config()
gate = GateProvider(HttpClient(proxy=config.proxy))
symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC_USDT"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 900

bars = gate.klines(symbol, config.timeframe, count)
higher_all = gate.klines(symbol, config.confirm_timeframe, 400)
filters = gate.filters(symbol)
step = seconds_per_bar(config.timeframe)
hstep = seconds_per_bar(config.confirm_timeframe)
kwargs = dict(
    confidence_threshold=config.confidence_threshold, require_multiple_signals=config.require_multiple_signals,
    no_trade_if_signals_conflict=config.no_trade_if_signals_conflict,
    min_reward_risk_ratio=config.limits.min_reward_risk_ratio, maker_fee_pct=config.maker_fee_pct,
    taker_fee_pct=config.taker_fee_pct, est_slippage_pct=config.limits.max_slippage_pct,
)

strategy_reasons: Counter[str] = Counter()
risk_reasons: Counter[str] = Counter()
regimes: Counter[str] = Counter()
approved = 0

state = AccountState.new(config.allocated_capital)
for index in range(80, len(bars) - 1):
    window = bars[: index + 1]
    decision_time = window[-1].open_time + step
    higher = select_closed_higher(higher_all, decision_time, hstep)
    if len(higher) < 60:
        continue
    snap = _snapshot(symbol, config.timeframe, window, higher[-200:], window[-1], filters, 0.05, 120.0)
    snap.higher_klines = higher[-200:] if len(higher) >= 60 else None
    proposal = propose(snap, None, **kwargs)
    analysis = analyze(snap)
    regimes[analysis.regime] += 1
    strategy_reasons[proposal.reason if proposal.action == "NO_TRADE" else proposal.action] += 1
    if proposal.action != "NO_TRADE":
        risk = evaluate(
            config, state, snap, proposal, None,
            now=datetime.fromtimestamp(decision_time, tz=timezone.utc),
            prices={symbol: window[-1].close}, kill_switch_active=False, clock_ok=True,
        )
        if risk.approved:
            approved += 1
        else:
            risk_reasons[risk.reasons[0][:70]] += 1

print(f"{symbol}: {len(bars)} bars, decisions from #80")
print("regime:", dict(regimes))
print("strategy outcome:", dict(strategy_reasons.most_common()))
print("risk-gate rejections:", dict(risk_reasons.most_common(10)))
print("fully approved entries:", approved)
