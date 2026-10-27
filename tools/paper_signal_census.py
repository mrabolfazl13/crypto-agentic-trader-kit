"""Census of the regime gate that the shipped PAPER engine actually uses (read-only).

Every PAPER cycle in the deployed store has ended with zero orders, and the
recorded reason is ``regime_filter`` for ~93% of symbol-cycles. That number alone
cannot tell two very different situations apart:

  1. the market is genuinely outside an uptrend, so a fail-closed gate is correct;
  2. the gate is a conjunction of thresholds that history almost never satisfies,
     so the shipped build can never place an order and Gate B can never collect
     its 30 closed trades.

This tool measures which of the two it is. It replays the shipped classifier
(``cat.strategy.analyze`` with ``default_params()``) over stored candles, bar by
bar, using only information available at that bar, and reports the regime mix plus
which individual predicate blocks. It places no order and writes no config; the only
thing that can move a threshold here is ``--normalize-to``, which rescales it in memory
for a diagnostic run and never reaches config or the deployed engine.

Usage:
    python tools/paper_signal_census.py --symbols 30 --timeframe 4h \
        --count-from 2026-05-01 --count-to 2026-08-01
    python tools/paper_signal_census.py --timeframe 1h --params state/quant/results/params.json
    python tools/paper_signal_census.py --timeframe 15m --normalize-to 4h   # diagnostic rescale
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.data import DataQuality, Kline, MarketSnapshot, Quote, SymbolFilter  # noqa: E402
from cat.quant.engine import load_bars  # noqa: E402
from cat.quant.features import TIMEFRAME_SECONDS, resample  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import current_data_version, daily_data_version, universe_symbols  # noqa: E402
from cat.strategies import StrategyParams, default_params, params_from_dict  # noqa: E402
from cat.strategy import analyze  # noqa: E402

# The classifier reads the last N of several series; the window has to cover the
# widest of them plus EMA warm-up, or every bar would come back UNKNOWN.  The
# deployed cycle hands the classifier 300 bars (cat/engine.py fetches
# limit=300, the same default as GateProvider.klines), so a census that wants to
# speak for that engine has to use that window - a 240-bar warm-up moved the
# borderline bars and made a matched run non-reproducible.
MIN_WINDOW_BARS = 300
# Only a nominal spread is needed: the quote feeds spread reporting, never the
# regime label itself.
ASSUMED_SPREAD_PCT = 0.05
DAY_SECONDS = 86400

# The three bar-length-dependent thresholds and how each is expected to scale with
# the bar.  One-step dispersion (realized vol, ATR) grows with the square root of
# elapsed time; a slope quoted in percent-per-bar grows with the bar itself.
SQRT_SCALERS = ("vol_threshold", "atr_pct_threshold")
LINEAR_SCALERS = ("slope_threshold",)


def normalize_params(params: StrategyParams, timeframe: str, reference: str) -> tuple[StrategyParams, dict[str, Any]]:
    """Rescale the bar-dependent thresholds to a reference timeframe (diagnostic only).

    Returns the replaced params plus the factors used, so a run records exactly which
    numbers were in play.  ``label()`` comes from the tuning dict, so a normalized run
    can never be mistaken for the shipped classifier's label.
    """
    if timeframe not in TIMEFRAME_SECONDS or reference not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe: {timeframe if timeframe not in TIMEFRAME_SECONDS else reference}")
    ratio = TIMEFRAME_SECONDS[timeframe] / TIMEFRAME_SECONDS[reference]
    factors = {key: sqrt(ratio) for key in SQRT_SCALERS}
    factors.update({key: ratio for key in LINEAR_SCALERS})
    changes = {key: getattr(params, key) * factor for key, factor in factors.items()}
    scaled = params.replace(**changes, note=f"{params.note} | thresholds rescaled to {reference}")
    record = {
        "reference": reference,
        "bar_seconds": TIMEFRAME_SECONDS[timeframe],
        "reference_bar_seconds": TIMEFRAME_SECONDS[reference],
        "ratio": round(ratio, 6),
        "factors": {key: round(value, 6) for key, value in sorted(factors.items())},
        "shipped": {key: getattr(params, key) for key in sorted(factors)},
        "effective": {key: round(value, 6) for key, value in sorted(changes.items())},
    }
    return scaled, record


def _daily_frame(minutes):
    """Daily buckets in the lake's own convention (labelled by close time).

    ``cat.quant.features.resample`` stops at 4h, and the classifier's higher
    timeframe needs a confirmed daily series, so this mirrors that aggregation
    for one day instead of inventing a different one.
    """
    ts = minutes["ts"].to_numpy(dtype="int64")
    work = minutes.assign(bar_end=(ts // DAY_SECONDS + 1) * DAY_SECONDS)
    out = work.groupby("bar_end", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), bars=("ts", "count")).reset_index()
    out["complete"] = out["bars"] == DAY_SECONDS // 60
    return out[["bar_end", "open", "high", "low", "close", "volume", "bars", "complete"]]


def _klines(frame, *, step: int) -> list[Kline]:
    """Stored buckets labelled by close time -> Kline labelled by open time.

    ``quote_volume`` is filled with base volume because the lake keeps one volume
    column; the classifier only compares that series against its own average, so
    the units cancel.
    """
    return [Kline(int(row.bar_end) - step, float(row.open), float(row.high), float(row.low),
                  float(row.close), float(row.volume), float(row.volume), bool(row.complete))
            for row in frame.itertuples()]


def _snapshot(symbol: str, timeframe: str, bars: list[Kline], higher: list[Kline],
              higher_ends: list[int], index: int, window: int) -> MarketSnapshot:
    """The snapshot the engine would have held at ``bars[index]`` and nothing later.

    The cutoff is the bucket's close time, so a higher-timeframe bar counts as
    confirmed only once it finished inside the slice being classified.
    """
    start = max(0, index + 1 - window)
    seen = bars[start:index + 1]
    last = seen[-1]
    bar_end = last.open_time + TIMEFRAME_SECONDS[timeframe]
    half = ASSUMED_SPREAD_PCT / 200.0
    quote = Quote(symbol=symbol, bid=last.close * (1 - half), ask=last.close * (1 + half),
                  ts=bar_end, source="lake")
    confirmed = higher[:bisect_right(higher_ends, bar_end)][-window:]
    return MarketSnapshot(
        symbol=symbol, timeframe=timeframe, klines=seen,
        higher_klines=confirmed or None, quote=quote, cross_quote=None,
        filters=SymbolFilter(symbol=symbol, price_precision=8, qty_precision=8, min_qty=0.0,
                             min_quote=0.0, max_quote=0.0, tradable=True, fee_pct=0.1),
        quality=DataQuality(is_fresh=True), fetched_at=float(bar_end))


def predicates(analysis, params: StrategyParams) -> dict[str, bool]:
    """Which individual conditions of the TRENDING_UP branch held at this bar."""
    ind = analysis.indicators
    vol = ind.get("realized_vol_pct_per_bar")
    atr_pct = ind.get("atr_pct")
    slope = ind.get("trend_slope_pct_per_bar")
    ready = None not in (vol, atr_pct, slope)
    return {
        "indicators_ready": ready,
        "vol_ok": ready and vol <= params.vol_threshold,
        "atr_pct_ok": ready and atr_pct <= params.atr_pct_threshold,
        "slope_up": ready and slope > params.slope_threshold,
        "bullish_majority": len(analysis.bullish) > len(analysis.bearish),
    }


def months_with_extra(settings, extra: str) -> list[str]:
    """The configured months plus a month named on the command line.

    ``quant.months`` is not edited for a daily-sourced month: a run only sees October
    when it asks for it, so every earlier window keeps reading exactly what it read
    when its record was written.
    """
    months = list(settings.months)
    for month in (part.strip() for part in extra.split(",")):
        if month and month not in months:
            months.append(month)
    return sorted(months)


def census_symbol(settings, symbol: str, *, timeframe: str, params: StrategyParams,
                  window: int, count_from: int | None, count_to: int | None,
                  months: list[str] | None = None) -> dict[str, Any]:
    minutes = load_bars(settings, symbol, months=months)
    frame = resample(minutes, timeframe)
    frame = frame[frame["complete"]]
    bars = _klines(frame, step=TIMEFRAME_SECONDS[timeframe])
    daily_frame = _daily_frame(minutes)
    daily = _klines(daily_frame[daily_frame["complete"]], step=DAY_SECONDS)
    higher_ends = [bar.open_time + DAY_SECONDS for bar in daily]
    if len(bars) < window + 1:
        return {"symbol": symbol, "skipped": f"only {len(bars)} bars"}

    # Warm-up always comes from bars *before* the counted interval, so a 4h run and
    # a 1h run can be compared over the same calendar days instead of the same bar
    # count (300 bars is 50 days of 4h but only 12.5 days of 1h).
    start = window
    if count_from is not None:
        start = max(window, next((i for i, bar in enumerate(bars)
                                  if bar.open_time + TIMEFRAME_SECONDS[timeframe] >= count_from), window))
    stop = len(bars)
    if count_to is not None:
        stop = max(start, next((i for i, bar in enumerate(bars)
                                if bar.open_time >= count_to), len(bars)))
    if stop <= start:
        return {"symbol": symbol, "skipped": "no bars inside the counted interval"}

    regimes: Counter[str] = Counter()
    block: Counter[str] = Counter()
    joint: Counter[str] = Counter()
    counts = {"bars": 0, "trend_up": 0, "vol_ok": 0, "atr_ok": 0, "slope_up": 0, "bullish": 0}
    first_open_time = bars[start].open_time
    last_open_time = bars[stop - 1].open_time
    for index in range(start, stop):
        analysis = analyze(_snapshot(symbol, timeframe, bars, daily, higher_ends, index, window), params)
        counts["bars"] += 1
        regimes[analysis.regime] += 1
        flags = predicates(analysis, params)
        if flags["vol_ok"]:
            counts["vol_ok"] += 1
        if flags["atr_pct_ok"]:
            counts["atr_ok"] += 1
        if flags["slope_up"]:
            counts["slope_up"] += 1
        if flags["bullish_majority"]:
            counts["bullish"] += 1
        if analysis.regime == "TRENDING_UP":
            counts["trend_up"] += 1
            continue
        for key, label in (("indicators_ready", "indicators_not_ready"), ("vol_ok", "vol_above_threshold"),
                           ("atr_pct_ok", "atr_pct_above_threshold"), ("slope_up", "slope_below_threshold"),
                           ("bullish_majority", "not_bullish_majority")):
            if not flags[key]:
                block[label] += 1
                break
        joint[sum(1 for key in ("vol_ok", "atr_pct_ok", "slope_up", "bullish_majority") if flags[key])] += 1
    days = max(1.0, (last_open_time - first_open_time) / 86400.0)
    return {
        "symbol": symbol, "bars": counts["bars"], "regimes": dict(regimes),
        "blocking_predicate": dict(block),
        "parts_passing": {str(key): value for key, value in sorted(joint.items())},
        "shares_pct": {key: round(value / max(1, counts["bars"]) * 100, 3) for key, value in counts.items()},
        "trend_up_bars": counts["trend_up"],
        "trend_up_per_day": round(counts["trend_up"] / days, 4),
        "window_days": round(days, 2),
        "first_bar": datetime.fromtimestamp(first_open_time, tz=timezone.utc).isoformat(),
        "last_bar": datetime.fromtimestamp(last_open_time, tz=timezone.utc).isoformat(),
    }


def _epoch(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    return int(parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp())


def main() -> int:
    parser = argparse.ArgumentParser(description="replay the shipped regime classifier over stored candles")
    parser.add_argument("--symbols", type=int, default=30, help="how many universe symbols to replay")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--count-from", default="", help="first counted bar (ISO date); warm-up uses earlier bars")
    parser.add_argument("--count-to", default="", help="last counted bar, exclusive (ISO date)")
    parser.add_argument("--window-bars", type=int, default=MIN_WINDOW_BARS)
    parser.add_argument("--params", default="", help="JSON tuning dict (defaults to the shipped v1)")
    parser.add_argument("--normalize-to", default="",
                        help="diagnostic: rescale vol/atr_pct by sqrt(bar/ref) and slope by bar/ref to this "
                             "reference timeframe (e.g. 4h); off by default")
    parser.add_argument("--out", default="state/quant/results/paper_signal_census")
    parser.add_argument("--extra-months", default="",
                        help="comma separated YYYY-MM stored outside quant.months (a daily-sourced month); "
                             "off by default so earlier windows keep reading only monthly files")
    args = parser.parse_args()

    settings = load_quant_settings(load_config(root=ROOT))
    months = months_with_extra(settings, args.extra_months)
    # Name the files the run actually read: a daily-sourced month has its own manifest,
    # so current_data_version() alone would not describe it.
    lake_version = (daily_data_version(settings) if months != list(settings.months)
                    else current_data_version(settings))
    count_from = _epoch(args.count_from) if args.count_from else None
    count_to = _epoch(args.count_to) if args.count_to else None
    params = default_params()
    if args.params:
        params = params_from_dict(json.loads(Path(args.params).read_text(encoding="utf-8")), base=params)
    normalization = None
    if args.normalize_to:
        params, normalization = normalize_params(params, args.timeframe, args.normalize_to)

    symbols = universe_symbols(settings, limit=args.symbols)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    span = f"{args.count_from or 'first stored bar'}..{args.count_to or 'last stored bar'}"
    print(f"census: {len(symbols)} symbols x {args.timeframe} counted {span}, classifier {params.label()}, "
          f"warm-up={args.window_bars} bars, gate={params.regime_gate}")
    if normalization:
        print(f"normalized thresholds -> {normalization['effective']} "
              f"(factors {normalization['factors']}, ref {normalization['reference']})")
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, symbol in enumerate(symbols, start=1):
        try:
            row = census_symbol(settings, symbol, timeframe=args.timeframe, params=params,
                               window=args.window_bars, count_from=count_from, count_to=count_to,
                               months=months)
        except Exception as exc:  # a gap in one symbol must not end the census
            row = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        if "bars" not in row:
            print(f"  [{index}/{len(symbols)}] {symbol}: {row.get('error') or row.get('skipped')}")
        else:
            shares = row["shares_pct"]
            print(f"  [{index}/{len(symbols)}] {symbol}: bars={row['bars']} "
                  f"TRENDING_UP={shares['trend_up']}% ({row['trend_up_per_day']}/day) "
                  f"blocked={row['blocking_predicate']}")

    measured = [row for row in rows if "bars" in row]
    total_bars = sum(row["bars"] for row in measured)
    pooled_regimes: Counter[str] = Counter()
    pooled_block: Counter[str] = Counter()
    for row in measured:
        pooled_regimes.update(row["regimes"])
        pooled_block.update(row["blocking_predicate"])
    median_trend_up = sorted(row["shares_pct"]["trend_up"] for row in measured)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "classifier": params.as_dict(),
        "timeframe": args.timeframe,
        "counted_from": args.count_from or None,
        "counted_to": args.count_to or None,
        "counted_days": round(sum(row["window_days"] for row in measured) / max(1, len(measured)), 2),
        "window_bars": args.window_bars,
        "symbols": len(measured),
        "symbols_never_trending_up": sum(1 for row in measured if row["trend_up_bars"] == 0),
        "bars": total_bars,
        "regime_mix_pct": {key: round(value / max(1, total_bars) * 100, 3)
                           for key, value in pooled_regimes.most_common()},
        "blocking_predicate_pct": {key: round(value / max(1, total_bars) * 100, 3)
                                   for key, value in pooled_block.most_common()},
        "trend_up_bars": sum(row["trend_up_bars"] for row in measured),
        "per_symbol_trend_up_pct": {"min": median_trend_up[0] if median_trend_up else None,
                                    "median": median_trend_up[len(median_trend_up) // 2] if median_trend_up else None,
                                    "max": median_trend_up[-1] if median_trend_up else None},
        "mean_predicate_shares_pct": {key: round(sum(row["shares_pct"][key] for row in measured)
                                                 / max(1, len(measured)), 3)
                                      for key in ("vol_ok", "atr_ok", "slope_up", "bullish", "trend_up")},
        "months_read": months,
        "data_version": lake_version,
        "threshold_normalization": normalization,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "note": ("replay of the shipped classifier on stored candles; no order was placed and no "
                 "threshold in config was changed"
                 + ("" if normalization is None else " (this run rescaled thresholds in memory only)")),
    }
    suffix = f"-normalized-{args.normalize_to}" if normalization else ""
    path = out_dir / f"census-{args.timeframe}{suffix}.json"
    path.write_text(json.dumps({"summary": summary, "per_symbol": rows},
                               indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\npooled regime mix: {summary['regime_mix_pct']}")
    print(f"blocking predicate: {summary['blocking_predicate_pct']}")
    means = summary["mean_predicate_shares_pct"]
    print(f"mean predicate shares: vol_ok={means['vol_ok']}% atr_ok={means['atr_ok']}% "
          f"slope_up={means['slope_up']}% bullish={means['bullish']}% -> TRENDING_UP={means['trend_up']}%")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
