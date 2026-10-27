"""How many closed trades can the shipped build actually make? (read-only replay)

Gate B is judged on *trades per day* (30 closed trades in 28 days = 1.1322/day), but
every measurement so far counted bars: D-018/D-019 said the regime gate is open on
4.467% of 4h bars. A bar share is not a trade. This tool converts the same shipped
code path — `cat.strategy` propose + `cat.risk` sizing + `cat.backtest.run_backtest`
fills — into the unit the gate is actually judged in, and reports the holding times
that sit between an entry and the next entry.

Two locks are measured, not assumed:

  * the regime gate (how often an entry is proposed at all), and
  * the account caps (`max_account_allocation_pct` sizes every later entry to zero
    until a position closes, `max_trades_per_day` and `cooldown_minutes_after_loss`
    bound the rest) — so cadence is a function of holding time, not only of the gate.

Each symbol is replayed in its own allocated-capital account, which is what
`run_backtest` does, so the pooled number is an **upper bound**: one shared account
can never exceed one position at a time plus the daily caps. Nothing here changes a
threshold, a timeframe, an allocation cap or the config, and no order is placed.

Usage:
    python tools/paper_frequency_replay.py --timeframe 4h --symbols 30 \
        --from 2026-05-01 --to 2026-08-01 --out state/quant/results/freq_4h
    python tools/paper_frequency_replay.py --timeframe 1h ... --window-bars 300
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.backtest import run_backtest  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.data import SymbolFilter  # noqa: E402
from cat.quant.engine import load_bars  # noqa: E402
from cat.quant.features import TIMEFRAME_SECONDS, resample  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, universe_symbols  # noqa: E402
from cat.strategies import default_params, params_from_dict  # noqa: E402

from paper_signal_census import DAY_SECONDS, _daily_frame, _epoch, _klines  # noqa: E402

# Same protocol as the deployed cycle and as the census (F-020): the classifier gets
# a fixed trailing window of this many bars, never an unbounded history.
MIN_WINDOW_BARS = 300
ASSUMED_SPREAD_PCT = 0.05


def canonical_symbol(lake_symbol: str) -> str:
    """The lake ids candles as `BTCUSDT`; the deployed allowlist is `BTC_USDT`.

    Replayed through `cat.risk.evaluate` unchanged, that mismatch shows up as
    `symbol not in approved symbol list` (risk.py:95) and is mistaken for the build
    refusing to trade. The quote is stripped by longest suffix first (FDUSD before
    USD) and any non-empty base is accepted: a length heuristic dropped LINK, AAVE,
    DOGE and ASTER, which is what made the first two runs of this tool under-report.
    """
    for quote in ("FDUSD", "USDT", "USDC", "TUSD", "USD"):
        if lake_symbol.endswith(quote) and len(lake_symbol) > len(quote):
            return f"{lake_symbol[: -len(quote)]}_{quote}"
    return lake_symbol


def _permissive_filter(symbol: str) -> SymbolFilter:
    """The lake stores no exchange filters, so none are enforced here.

    That makes the cadence an upper bound: a real min_qty/min_quote could only reject
    more entries, never create them.
    """
    return SymbolFilter(symbol=symbol, price_precision=8, qty_precision=8, min_qty=0.0,
                        min_quote=0.0, max_quote=0.0, tradable=True, fee_pct=0.1)


def replay_symbol(config, settings, symbol: str, *, timeframe: str, params,
                  window_bars: int, count_from: int | None, count_to: int | None) -> dict[str, Any]:
    minutes = load_bars(settings, symbol)
    frame = resample(minutes, timeframe)
    frame = frame[frame["complete"]]
    step = TIMEFRAME_SECONDS[timeframe]
    bars = _klines(frame, step=step)
    daily_frame = _daily_frame(minutes)
    daily = _klines(daily_frame[daily_frame["complete"]], step=DAY_SECONDS)
    if len(bars) < window_bars + 2:
        return {"symbol": symbol, "skipped": f"only {len(bars)} bars"}

    # Warm-up comes from bars *before* the counted interval, so 4h and 1h runs cover
    # the same calendar days rather than the same bar count.
    if count_from is None:
        first = window_bars
    else:
        first = next((i for i, bar in enumerate(bars) if bar.open_time >= count_from), len(bars))
        if first < window_bars:
            return {"symbol": symbol, "skipped": f"no warm-up before {count_from}"}
    last = len(bars) if count_to is None else bisect_left_ts(bars, count_to)
    if last - first < 2:
        return {"symbol": symbol, "skipped": "fewer than two counted bars"}

    slice_bars = bars[first - window_bars:last]
    started = time.monotonic()
    result = run_backtest(config, canonical_symbol(symbol), bars=slice_bars, higher_bars=daily,
                          filters=_permissive_filter(symbol), timeframe=timeframe,
                          warmup=window_bars, assumed_spread_pct=ASSUMED_SPREAD_PCT,
                          params=params, window_bars=window_bars)
    days = (slice_bars[-1].open_time + step - slice_bars[window_bars].open_time) / 86400.0
    holdings = [(t.exit_time - t.entry_time) / 3600.0 for t in result.trades]
    wins = [t for t in result.trades if t.pnl > 0]
    return {
        "symbol": symbol,
        "counted_days": round(days, 3),
        "bars": len(slice_bars) - window_bars,
        "closed_trades": len(result.trades),
        "trades_per_day": round(len(result.trades) / days, 4) if days > 0 else None,
        "holding_hours_median": round(median(holdings), 2) if holdings else None,
        "holding_hours_mean": round(mean(holdings), 2) if holdings else None,
        "holding_hours_max": round(max(holdings), 2) if holdings else None,
        "win_rate_pct": round(len(wins) / len(result.trades) * 100.0, 2) if result.trades else None,
        "profit_factor": result.metrics.get("profit_factor"),
        "net_return_pct": result.metrics.get("net_return_pct"),
        "buy_hold_pct": round(result.buy_hold_pct, 3),
        "max_drawdown_pct": round(result.max_drawdown_pct, 3),
        "fees_paid": round(result.fees_paid, 4),
        "rejections": dict(sorted(result.rejected.items(), key=lambda kv: -kv[1])),
        "exit_reasons": {reason: sum(1 for t in result.trades if t.reason == reason)
                         for reason in {t.reason for t in result.trades}},
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def bisect_left_ts(bars, ts: int) -> int:
    from bisect import bisect_left
    return bisect_left([bar.open_time for bar in bars], ts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--symbols", type=int, default=30)
    parser.add_argument("--from", dest="count_from", default="2026-05-01")
    parser.add_argument("--to", dest="count_to", default="2026-08-01")
    parser.add_argument("--window-bars", type=int, default=MIN_WINDOW_BARS)
    parser.add_argument("--params", help="optional StrategyParams JSON to replay instead of shipped")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.window_bars < MIN_WINDOW_BARS:
        # Refuse rather than measure a protocol the deployed engine never runs (F-020).
        print(f"window-bars must be at least {MIN_WINDOW_BARS} (production limit); got {args.window_bars}")
        return 3
    if args.timeframe not in TIMEFRAME_SECONDS:
        print(f"unknown timeframe: {args.timeframe}")
        return 3

    config = load_config(root=ROOT)
    settings = load_quant_settings(config)
    params = default_params()
    if args.params:
        params = params_from_dict(json.loads(Path(args.params).read_text(encoding="utf-8")), base=params)

    symbols = universe_symbols(settings, limit=args.symbols)
    count_from = _epoch(args.count_from) if args.count_from else None
    count_to = _epoch(args.count_to) if args.count_to else None
    limits = config.limits
    print(f"frequency replay: {len(symbols)} symbols x {args.timeframe} counted "
          f"{args.count_from}..{args.count_to}, classifier {params.label()}, warm-up={args.window_bars} bars")
    print(f"account caps in force: allocation {limits.max_account_allocation_pct}% of "
          f"{config.allocated_capital} capital, {limits.max_trades_per_day} trades/day, "
          f"cooldown {limits.cooldown_minutes_after_loss} min after a loss")

    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, symbol in enumerate(symbols, start=1):
        try:
            row = replay_symbol(config, settings, symbol, timeframe=args.timeframe, params=params,
                                window_bars=args.window_bars, count_from=count_from, count_to=count_to)
        except Exception as exc:  # one bad symbol must not end the replay
            row = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        if "closed_trades" in row:
            print(f"  [{index}/{len(symbols)}] {symbol}: trades={row['closed_trades']} "
                  f"({row['trades_per_day']}/day) hold_med={row['holding_hours_median']}h "
                  f"pf={row['profit_factor']} net={row['net_return_pct']}% vs BH {row['buy_hold_pct']}%")
        else:
            print(f"  [{index}/{len(symbols)}] {symbol}: {row.get('error') or row.get('skipped')}")

    measured = [row for row in rows if "closed_trades" in row]
    total_trades = sum(row["closed_trades"] for row in measured)
    days = mean([row["counted_days"] for row in measured]) if measured else 0.0
    pooled_rejections: dict[str, int] = {}
    for row in measured:
        for reason, count in row["rejections"].items():
            pooled_rejections[reason] = pooled_rejections.get(reason, 0) + count
    holding_means = [row["holding_hours_mean"] for row in measured if row["holding_hours_mean"]]
    per_day = sorted((row["trades_per_day"] or 0.0) for row in measured)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeframe": args.timeframe,
        "counted_from": args.count_from,
        "counted_to": args.count_to,
        "counted_days_per_symbol": round(days, 3),
        "window_bars": args.window_bars,
        "classifier": params.as_dict(),
        "symbols_replayed": len(measured),
        "symbols_skipped": [row["symbol"] for row in rows if "closed_trades" not in row],
        "closed_trades_total": total_trades,
        "trades_per_day_per_symbol": {"min": per_day[0] if per_day else None,
                                      "median": per_day[len(per_day) // 2] if per_day else None,
                                      "max": per_day[-1] if per_day else None},
        "pooled_trades_per_day_upper_bound": round(total_trades / days, 4) if days else None,
        "single_account_bound": {
            "serial_position_max_trades_per_day": round(24.0 / mean(holding_means), 4) if holding_means else None,
            "max_trades_per_day_cap": limits.max_trades_per_day,
            "note": ("one shared account holds at most one position: the allocation cap sizes every later "
                     "entry to zero until it closes, so the real rate is min(1/mean holding, per-day cap)"),
        },
        "holding_hours": {"median_of_medians": round(median([row["holding_hours_median"] for row in measured
                                                             if row["holding_hours_median"]]), 2)
                          if any(row["holding_hours_median"] for row in measured) else None,
                          "mean_of_means": round(mean(holding_means), 2) if holding_means else None},
        "profit_factor_by_symbol": {row["symbol"]: row["profit_factor"] for row in measured
                                    if row["profit_factor"] is not None},
        "net_vs_buy_hold_pct_by_symbol": {row["symbol"]: [row["net_return_pct"], row["buy_hold_pct"]]
                                          for row in measured if row["net_return_pct"] is not None},
        "rejections_pooled": dict(sorted(pooled_rejections.items(), key=lambda kv: -kv[1])),
        "data_version": current_data_version(settings),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "note": ("read-only replay of the deployed propose/risk/backtest path; each symbol gets its own "
                 "allocated-capital account so the pooled rate is an upper bound; no order placed, no "
                 "threshold, timeframe or allocation cap changed"),
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"frequency-{args.timeframe}.json").write_text(
        json.dumps({"summary": summary, "per_symbol": rows}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"\npooled (upper bound): {total_trades} closed trades over {round(days, 2)} days "
          f"= {summary['pooled_trades_per_day_upper_bound']}/day across {len(measured)} symbols")
    if holding_means:
        print(f"mean holding {round(mean(holding_means), 2)}h ⇒ a single shared account could make at most "
              f"{summary['single_account_bound']['serial_position_max_trades_per_day']}/day "
              f"(cap {limits.max_trades_per_day}/day)")
    top = ", ".join(f"{reason} {count}" for reason, count in list(summary["rejections_pooled"].items())[:6])
    print(f"rejections pooled: {top or 'none'}")
    print(f"written: {out_dir / f'frequency-{args.timeframe}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
