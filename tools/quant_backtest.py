"""Run one PAPER research backtest (or a walk-forward) over stored symbols.

    python tools/quant_backtest.py --strategy ema_trend --timeframe 1h --limit 5
    python tools/quant_backtest.py --strategy trend_pullback --timeframe 4h --walk-forward
    python tools/quant_backtest.py --strategy buy_hold --limit 100 --json state/quant/results/bh.json
    python tools/quant_backtest.py --strategy trend_pullback --timeframe 4h --symbols 30 \
        --extra-months 2026-10 --start 2026-10-01 --end 2026-10-27 --json ...

Reads only the local data lake; never touches an exchange endpoint. ``--extra-months`` is the only
way a run sees a daily-sourced month, so the windows already recorded keep reproducing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from cat.config import load_config  # noqa: E402
from cat.quant.engine import Window, run_backtest, walk_forward  # noqa: E402
from cat.quant.features import STRATEGIES, params_from_dict  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, daily_data_version, universe_symbols  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="ema_trend", choices=STRATEGIES)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--params", default=None, help="JSON overrides, e.g. '{\"ema_fast\": 8}'")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--folds", type=int, default=None)
    parser.add_argument("--test-days", type=int, default=None)
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--position-pct", type=float, default=None)
    parser.add_argument("--max-positions", type=int, default=None)
    parser.add_argument("--max-hold-hours", type=float, default=None)
    parser.add_argument("--fee-pct", type=float, default=None)
    parser.add_argument("--slippage-pct", type=float, default=None)
    parser.add_argument("--min-edge-cost-ratio", type=float, default=None,
                        help="drop entries whose target cannot pay the round trip this many times")
    parser.add_argument("--risk-per-trade-pct", type=float, default=None,
                        help="size each position so a stop-out costs this share of equity")
    parser.add_argument("--extra-months", default=None,
                        help="comma separated YYYY-MM stored outside quant.months (a daily-sourced "
                             "month); off by default so an earlier window keeps reading only "
                             "monthly files")
    parser.add_argument("--start", default=None, help="ISO date; entries only at or after this")
    parser.add_argument("--end", default=None, help="ISO date; entries strictly before this")
    parser.add_argument("--regime-symbol", default=None,
                        help="override quant.backtest.regime_symbol for this run only; '' turns the "
                             "entry gate off in memory — config.yaml is never written")
    parser.add_argument("--json", default=None, help="write the full result JSON here")
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    months_read = list(settings.months)
    if args.extra_months:
        from dataclasses import replace
        from paper_signal_census import months_with_extra
        months_read = months_with_extra(settings, args.extra_months)
        settings = replace(settings, months=tuple(months_read))
    requested = {"start": args.start, "end": args.end}
    start_ts = None if args.start is None else int(datetime.fromisoformat(args.start)
                                                   .replace(tzinfo=timezone.utc).timestamp())
    end_ts = None if args.end is None else int(datetime.fromisoformat(args.end)
                                               .replace(tzinfo=timezone.utc).timestamp())
    if (start_ts is not None and end_ts is not None and end_ts <= start_ts):
        parser.error(f"empty window {args.start}..{args.end}")
    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
               else universe_symbols(settings, limit=args.limit))
    params = params_from_dict(overrides=json.loads(args.params) if args.params else None)
    timeframe = args.timeframe or settings.signal_timeframes[0]
    from cat.quant.engine import simulate_settings
    sim = simulate_settings(settings, **{key: value for key, value in {
        "initial_capital": args.capital,
        "position_pct": args.position_pct,
        "max_open_positions": args.max_positions,
        "max_hold_hours": args.max_hold_hours,
        "fee_pct": args.fee_pct,
        "slippage_pct": args.slippage_pct,
        "min_edge_cost_ratio": args.min_edge_cost_ratio,
        "risk_per_trade_pct": args.risk_per_trade_pct,
        "regime_symbol": args.regime_symbol,
    }.items() if value is not None})

    started = time.monotonic()
    progress = lambda event: print(event.get("message") or f"{event.get('done')}/{event.get('total')} "
                                  f"{event.get('symbol', '')}", flush=True)
    if args.walk_forward:
        if start_ts is not None or end_ts is not None:
            if start_ts is None or end_ts is None:
                parser.error("--walk-forward needs both --start and --end")
        result = walk_forward(settings, symbols, strategy=args.strategy, timeframe=timeframe,
                              params=params, folds=args.folds, test_days=args.test_days, sim=sim,
                              window=None if start_ts is None else Window(start_ts, end_ts),
                              progress=progress)
        aggregate = result["aggregate"]
        print(f"\nwalk-forward {args.strategy} {timeframe}: {aggregate['folds']} folds, "
              f"{aggregate['trades']} trades, sum return {aggregate['net_return_pct_sum']}%, "
              f"positive folds {aggregate['positive_fold_pct']}%, PF mean {aggregate['profit_factor_mean']}, "
              f"max DD {aggregate['max_drawdown_pct']}%")
        for fold in result["folds"]:
            m = fold["metrics"]
            print(f"  fold {fold['fold']} {fold['test']['start'][:10]}→{fold['test']['end'][:10]}: "
                  f"{m['trades']} trades, {m['net_return_pct']}% vs BH {fold['benchmark'].get('return_pct')}%, "
                  f"PF {m['profit_factor']}, DD {m['max_drawdown_pct']}%")
    else:
        result = run_backtest(settings, symbols, strategy=args.strategy, timeframe=timeframe,
                              params=params, sim=sim, start_ts=start_ts, end_ts=end_ts,
                              progress=progress)
        m = result["metrics"]
        print(f"\n{args.strategy} {timeframe} on {result['symbols_evaluated']} symbols: "
              f"{m['trades']} trades, net {m['net_return_pct']}% vs BH {m.get('benchmark_return_pct')}%, "
              f"PF {m['profit_factor']}, DD {m['max_drawdown_pct']}%, win {m['win_rate_pct']}%, "
              f"fees {m['fees']}, exposure {m['exposure_pct']}%")
        print(f"rejections: {m['rejections']}")
        if result["errors"]:
            print(f"errors: {list(result['errors'].items())[:5]}")

    result["months_read"] = months_read
    result["requested_window"] = requested
    result["lake_version"] = (daily_data_version(settings) if args.extra_months
                              else current_data_version(settings))
    if args.extra_months or args.start or args.end:
        print(f"months read: {months_read} | counted window: {args.start}..{args.end} "
              f"| gate: {sim.regime_symbol or 'off'} EMA{sim.regime_ema}")

    if args.json:
        path = Path(args.json)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {path}")
    print(f"elapsed {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
