"""Why does the deployed loop say it cannot look at some symbols at all?  (read-only)

D-027 counted the unevaluable rows in the ``decisions`` ledger; this tool asks which of them are a
fact about the market and which are a fact about our own arithmetic.  For each symbol it compares
three things over the same recent closed bars: the reason the ledger recorded, the reason the
shipped code gives right now, and the reason the same shipped code gives when the only change is
that ATR is no longer rounded to six fixed decimals.  Walking the history one decision at a time
(production cadence, not every bar) keeps the answer in the same units as the ledger.

Nothing here trades, writes engine state, or changes a threshold; the only file written is this
tool's own JSON artifact.  The cross-check source is deliberately left out: a replay cannot
reproduce a point-in-time MEXC quote, so disagreement magnitudes are read from the ledger instead.

Exit codes: 0 = measured, 3 = the evidence is not PAPER-only or nothing could be probed.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import cat.strategy as strategy_module  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.data import TIMEFRAMES, GateProvider, fetch_snapshot  # noqa: E402
from cat.http import HttpClient  # noqa: E402
from cat.indicators import atr as wilder_atr  # noqa: E402
from cat.paper_evidence import _decision_reason  # noqa: E402
from cat.strategy import analyze, propose  # noqa: E402
from paper_decisions_unarchived import classify, groups_from_universe  # noqa: E402

EXIT = {"MEASURED": 0, "FAIL": 3}
ATR_DECIMALS = 6
# ``round(x, 6)`` is exactly zero for 0 <= x < 5e-7, and that floor — not the market — is what
# ``propose()`` tests when it answers ``no_atr``.
ATR_FLOOR = 5e-7
DISAGREEMENT_RE = re.compile(r"source_price_disagreement:([0-9.]+)%")
# Gate refuses a candlesticks window wider than ~one year; 90 days is inside the limit and longer
# than the 60 bars (10 days) the shipped min_bars rule needs.
WINDOW_DAYS = 90
H1_CONFIRM_SHARE = 0.5


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def ledger_census(rows: list[dict]) -> dict:
    """Per-symbol reason counts plus the disagreement magnitudes the loop itself recorded."""
    reasons: dict[str, Counter] = defaultdict(Counter)
    divergences: dict[str, list[float]] = defaultdict(list)
    divergence_cycles: dict[str, set] = defaultdict(set)
    cycle_rows: dict[str, list[float]] = defaultdict(list)
    last_ts: dict[str, str] = {}
    for row in rows:
        symbol = str(row["symbol"])
        reason = _decision_reason(row)
        reasons[symbol][reason] += 1
        last_ts[symbol] = str(row["ts"])
        if reason == "source_price_disagreement":
            match = DISAGREEMENT_RE.search(str(row.get("rationale_json") or ""))
            if match:
                value = float(match.group(1))
                divergences[symbol].append(value)
                divergence_cycles[symbol].add(str(row["ts"]))
                cycle_rows[str(row["ts"])].append(value)
    totals: Counter[str] = Counter()
    for counter in reasons.values():
        for reason, count in counter.items():
            if classify(reason) == "not_evaluable":
                totals[reason] += count
    all_cycles = {str(row["ts"]) for row in rows}
    return {
        "symbols_with_rows": len(reasons),
        "cycles_in_ledger": len(all_cycles),
        "reason_counts_by_symbol": {symbol: dict(counter) for symbol, counter in sorted(reasons.items())},
        "not_evaluable_totals": dict(totals),
        "not_evaluable_rows_total": sum(totals.values()),
        "disagreement_spread_by_symbol": {
            symbol: {
                "rows": len(values),
                "cycles": len(divergence_cycles[symbol]),
                "min_pct": round(min(values), 3),
                "median_pct": round(statistics.median(values), 3),
                "max_pct": round(max(values), 3),
            }
            for symbol, values in sorted(divergences.items(), key=lambda item: -len(item[1]))
        },
        # Is this a cross-source outage or a few persistently diverging books?  A systemic fault
        # would light up most of the basket in one cycle; these do not.
        "disagreement_by_cycle": {
            cycle: {"symbols": len(values), "max_pct": round(max(values), 3)}
            for cycle, values in sorted(cycle_rows.items())
        },
        "cycles_with_any_disagreement": len(cycle_rows),
        "max_disagreeing_symbols_in_one_cycle": max((len(v) for v in cycle_rows.values()), default=0),
        "last_row_ts_by_symbol": last_ts,
    }


def atr_facts(atr_raw: float | None, atr_pct: float | None, atr_pct_threshold: float,
              price_tick: float | None) -> dict:
    """The separate facts one decision point carries, kept apart on purpose.

    ``killed_by_rounding`` is our arithmetic: a positive ATR that ``round(x, 6)`` deletes.
    ``atr_below_venue_tick`` is the venue's arithmetic: the exchange itself cannot express a stop
    that small, so no amount of precision at our end helps.  ``regime_would_block_after_fix`` is the
    market's answer at full precision.  Folding these into one label is how a data artifact ends up
    reading like a signal, which is the F-030 failure mode.
    """
    present = atr_raw is not None
    positive = bool(present and atr_raw > 0)
    return {
        "atr_absent": not present,
        "atr_zero_in_market": bool(present and atr_raw <= 0),
        "killed_by_rounding": bool(positive and round(atr_raw, ATR_DECIMALS) <= 0),
        "atr_below_venue_tick": bool(positive and price_tick and atr_raw < price_tick),
        "regime_would_block_after_fix": bool(positive and atr_pct is not None
                                             and atr_pct > atr_pct_threshold
                                             and round(atr_raw, ATR_DECIMALS) <= 0),
    }


def scan_history(bars: list, params, price_tick: float | None) -> dict:
    """Walk closed bars one decision at a time and classify each against the shipped rule."""
    period, min_bars = params.atr_period, params.min_bars
    series = wilder_atr(bars, period)
    counts: Counter[str] = Counter()
    points = 0
    for end in range(min_bars, len(bars)):
        index = end - period
        if index < 0 or index >= len(series):
            continue
        atr_raw = series[index]
        close = bars[end].close
        atr_pct = round(atr_raw / close * 100, 4) if close else None
        facts = atr_facts(atr_raw, atr_pct, params.atr_pct_threshold, price_tick)
        points += 1
        counts.update({key: 1 for key, value in facts.items() if value})
    killed = counts["killed_by_rounding"]
    return {
        "decision_points": points,
        "counts": dict(counts),
        "precision_block_share": round(killed / points, 4) if points else None,
        "below_venue_tick_share_of_killed": round(counts["atr_below_venue_tick"] / killed, 4) if killed else None,
        "still_regime_blocked_share_of_killed": round(
            counts["regime_would_block_after_fix"] / killed, 4) if killed else None,
    }


def counterfactual_reason(snapshot, params, kwargs: dict, atr_raw: float | None) -> str:
    """Run the shipped proposal path on a full-precision ATR pasted into the indicator dict.

    ``propose()`` calls ``analyze()`` internally, so testing the counterfactual without
    reimplementing the gate means swapping that one call for the duration of the proposal.
    """
    analysis = analyze(snapshot, params)
    patched = replace(analysis, indicators={**analysis.indicators, "atr": atr_raw,
                                            f"atr{params.atr_period}": atr_raw})
    original = strategy_module.analyze
    strategy_module.analyze = lambda *a, **k: patched
    try:
        proposal = propose(snapshot, None, **kwargs)
    finally:
        strategy_module.analyze = original
    return proposal.reason if proposal.action == "NO_TRADE" else f"executed:{proposal.action.lower()}"


def bars_at(gate: GateProvider, symbol: str, timeframe: str, at_ts: int) -> dict:
    """How much history the pair actually had at a past decision (Gate window limit applies)."""
    rows = gate.client.get(
        f"{gate.base}/api/v4/spot/candlesticks",
        {"currency_pair": symbol, "interval": TIMEFRAMES[timeframe][1],
         "from": at_ts - WINDOW_DAYS * 86400, "to": at_ts},
    )
    closed = [int(float(row[0])) for row in rows
              if isinstance(row, (list, tuple)) and len(row) > 7 and str(row[7]).lower() == "true"]
    before = [ts for ts in closed if ts <= at_ts]
    return {
        "closed_bars_at_decision": len(before),
        "oldest_bar_available": (datetime.fromtimestamp(min(before), tz=timezone.utc).isoformat(timespec="seconds")
                                 if before else None),
        "newest_closed_bar_age_seconds": (at_ts - max(before)) if before else None,
        "window_days": WINDOW_DAYS,
    }


def basket_entry(gate: GateProvider, cfg, symbol: str) -> dict:
    """One orderable pair, asked only: can the shipped sizing rule ever see its volatility?"""
    params = cfg.strategy_params
    bars = [bar for bar in gate.klines(symbol, cfg.timeframe, 300) if bar.closed]
    series = wilder_atr(bars, params.atr_period)
    if not series or not bars:
        return {"symbol": symbol, "closed_bars": len(bars), "sizable": False, "reason": "no_bars"}
    atr_raw = series[-1]
    close = bars[-1].close
    tick = 10.0 ** -gate.filters(symbol).price_precision
    return {
        "symbol": symbol,
        "closed_bars": len(bars),
        "last_close": close,
        "venue_price_tick": tick,
        "atr_raw": atr_raw,
        "atr_pct": round(atr_raw / close * 100, 4) if close else None,
        "sizable": bool(atr_raw > 0 and round(atr_raw, ATR_DECIMALS) > 0),
        "below_venue_tick": bool(0 < atr_raw < tick),
        "too_young_for_min_bars": len(bars) < params.min_bars,
    }


def _safe_entry(gate: GateProvider, cfg, symbol: str) -> dict:
    try:
        return basket_entry(gate, cfg, symbol)
    except Exception as exc:  # one unreachable pair must not void the basket census
        return {"symbol": symbol, "sizable": None, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def basket_census(gate: GateProvider, cfg, *, workers: int = 4) -> dict:
    """The size of the precision defect across the whole deployed allowlist, symbol by symbol."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="d028-basket") as pool:
        rows = list(pool.map(lambda symbol: _safe_entry(gate, cfg, symbol), cfg.symbols))
    unresolved = [row for row in rows if row.get("sizable") is None]
    measured = [row for row in rows if row.get("sizable") is not None]
    sized = [row for row in measured if row["sizable"]]
    unsizable = [row for row in measured if not row["sizable"]]
    return {
        "symbols": len(rows),
        "measured": len(measured),
        "unresolved": {row["symbol"]: row.get("error") for row in unresolved},
        "sizable_count": len(sized),
        "unsizable_by_rounding_count": len(unsizable),
        "unsizable_symbols": [
            {key: row.get(key) for key in ("symbol", "last_close", "atr_raw", "atr_pct",
                                           "venue_price_tick", "below_venue_tick", "closed_bars")}
            for row in sorted(unsizable, key=lambda item: item["symbol"])
        ],
        "too_young_count": sum(1 for row in rows if row.get("too_young_for_min_bars")),
        "per_symbol": rows,
    }


def probe_symbol(gate: GateProvider, cfg, symbol: str, kwargs: dict, *, limit: int, census: dict) -> dict:
    params = cfg.strategy_params
    snapshot = fetch_snapshot(gate, None, symbol, cfg.timeframe, higher_timeframe=None, limit=limit,
                              staleness_seconds=cfg.limits.stop_on_data_staleness_seconds)
    closed = [bar for bar in snapshot.klines if bar.closed]
    series = wilder_atr(closed, params.atr_period)
    atr_raw = series[-1] if series else None
    analysis = analyze(snapshot, params)
    proposal = propose(snapshot, None, **kwargs)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    ledger_ts = census["last_row_ts_by_symbol"].get(symbol)
    price_tick = 10.0 ** -snapshot.filters.price_precision
    return {
        "symbol": symbol,
        "bars_returned": len(snapshot.klines),
        "closed_bars": len(closed),
        "min_bars_required": params.min_bars,
        "last_close": closed[-1].close if closed else None,
        "venue_price_precision": snapshot.filters.price_precision,
        "venue_price_tick": price_tick,
        "atr_raw_last": atr_raw,
        "atr_reported_last": analysis.indicators.get("atr"),
        "atr_pct_last": analysis.indicators.get("atr_pct"),
        "regime": analysis.regime,
        "ledger_reasons": census["reason_counts_by_symbol"].get(symbol, {}),
        "shipped_reason_now": proposal.reason if proposal.action == "NO_TRADE"
        else f"executed:{proposal.action}",
        "counterfactual_reason_at_full_precision": (
            counterfactual_reason(snapshot, params, kwargs, atr_raw) if atr_raw is not None else None),
        "history_scan": scan_history(closed, params, price_tick),
        "history_at_last_ledger_row": (
            bars_at(gate, symbol, cfg.timeframe, int(datetime.fromisoformat(ledger_ts).timestamp()))
            if ledger_ts else {"note": "no ledger row for this symbol"}),
        "fetched_at_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "state/reports/server_platform_snapshot.sqlite3"))
    parser.add_argument("--universe", default=str(ROOT / "state/quant/universe_allowlist.json"))
    parser.add_argument("--symbols",
                        default="BTC_USDT,ASTER_USDT,PEPE_USDT,SHIB_USDT,BTT_USDT,HTX_USDT,PEARL_USDT,BDX_USDT")
    parser.add_argument("--limit", type=int, default=300, help="4h bars fetched per symbol (production uses 300)")
    parser.add_argument("--out", default=str(ROOT / "state/quant/results/d028_precision/unevaluability.json"))
    parser.add_argument("--direct", action="store_true", help="ignore the configured proxy (this machine's proxy is dead)")
    parser.add_argument("--basket", action="store_true",
                        help="also sweep every orderable symbol once: how many can the sizing rule see at all?")
    args = parser.parse_args(argv)

    cfg = load_config()
    connection = _read_only(Path(args.db))
    rows = [dict(row) for row in connection.execute(
        "SELECT ts, symbol, action, rationale_json, mode FROM decisions ORDER BY ts")]
    connection.close()
    modes = {str(row.get("mode") or "") for row in rows}
    if modes - {"PAPER"}:
        print(f"REFUSED: decisions carry a non-PAPER mode: {sorted(modes)}")
        return EXIT["FAIL"]

    census = ledger_census(rows)
    gate = GateProvider(HttpClient(proxy=None if args.direct else cfg.proxy))
    kwargs = dict(
        confidence_threshold=cfg.confidence_threshold, require_multiple_signals=cfg.require_multiple_signals,
        no_trade_if_signals_conflict=cfg.no_trade_if_signals_conflict,
        min_reward_risk_ratio=cfg.limits.min_reward_risk_ratio, maker_fee_pct=cfg.maker_fee_pct,
        taker_fee_pct=cfg.taker_fee_pct, est_slippage_pct=cfg.limits.max_slippage_pct,
        params=cfg.strategy_params,
    )

    probes: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for symbol in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        try:
            probes[symbol] = probe_symbol(gate, cfg, symbol, kwargs, limit=args.limit, census=census)
        except Exception as exc:  # one unreachable pair must not void the whole census
            errors[symbol] = f"{type(exc).__name__}: {str(exc)[:180]}"

    verdicts = {}
    for symbol, probe in probes.items():
        scan = probe["history_scan"]
        killed_by_rounding_now = bool(probe["atr_raw_last"] and probe["atr_raw_last"] > 0
                                     and not probe["atr_reported_last"])
        verdicts[symbol] = {
            "precision_block_share": scan["precision_block_share"],
            "killed_by_rounding_at_this_instant": killed_by_rounding_now,
            "h1_confirmed": bool(scan["precision_block_share"] is not None
                                 and scan["precision_block_share"] >= H1_CONFIRM_SHARE
                                 and killed_by_rounding_now),
            "below_venue_tick_share_of_killed": scan["below_venue_tick_share_of_killed"],
            "still_regime_blocked_share_of_killed": scan["still_regime_blocked_share_of_killed"],
        }

    universe_groups, universe_note = None, "not loaded"
    universe_path = Path(args.universe)
    if universe_path.exists():
        try:
            universe_groups = groups_from_universe(json.loads(universe_path.read_text(encoding="utf-8")))
            universe_note = "ok"
        except SystemExit as exc:
            universe_note = str(exc)

    basket = basket_census(gate, cfg) if args.basket else None
    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision_rows": len(rows),
        "ledger_window_utc": [rows[0]["ts"], rows[-1]["ts"]] if rows else None,
        "provenance": {
            "db": str(Path(args.db)),
            "universe": str(universe_path),
            "universe_note": universe_note,
            "source": "gate public spot REST, read-only; higher timeframe and MEXC cross not fetched",
            "timeframe": cfg.timeframe,
            "bars_limit": args.limit,
            "atr_period": cfg.strategy_params.atr_period,
            "atr_round_decimals": ATR_DECIMALS,
            "atr_zero_floor": ATR_FLOOR,
            "min_bars": cfg.strategy_params.min_bars,
            "atr_pct_threshold": cfg.strategy_params.atr_pct_threshold,
            "strategy_version": cfg.strategy_params.label(),
            "group_sizes": {key: len(value) for key, value in universe_groups.items()} if universe_groups else None,
        },
        "ledger_census": census,
        "probes": probes,
        "basket_census": basket,
        "verdicts": verdicts,
        "errors": errors,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"{len(rows)} سطرِ تصمیم روی {census['symbols_with_rows']} نماد؛ "
          f"ارزیابی‌ناپذیرِ کل حلقه = {census['not_evaluable_rows_total']} ⇒ {census['not_evaluable_totals']}")
    for symbol, probe in probes.items():
        scan = probe["history_scan"]
        print(f"  {symbol}: price={probe['last_close']} tick={probe['venue_price_tick']:g} "
              f"atr_raw={probe['atr_raw_last']:.6g} atr_reported={probe['atr_reported_last']} "
              f"shipped_now={probe['shipped_reason_now']} "
              f"counterfactual={probe['counterfactual_reason_at_full_precision']} "
              f"precision_share={scan['precision_block_share']}/{scan['decision_points']} "
              f"bars_at_ledger={probe['history_at_last_ledger_row'].get('closed_bars_at_decision')}")
    for symbol, message in errors.items():
        print(f"  {symbol}: ERROR {message}")
    if basket:
        print(f"سبدِ {basket['symbols']} نماد: {basket['sizable_count']} نماد با ATRِ قابل‌ممیزی، "
              f"{basket['unsizable_by_rounding_count']} نماد که ruleِ ۶ رقمِ اعشار ATRشان را صفر می‌کند، "
              f"{basket['too_young_count']} نماد زیرِ min_bars، {len(basket['unresolved'])} بی‌پاسخ")
        for row in basket["unsizable_symbols"]:
            print(f"    {row['symbol']}: price={row['last_close']} atr={row['atr_raw']} "
                  f"tick={row['venue_price_tick']:g} below_tick={row['below_venue_tick']}")
    print(f"artifact: {out_path}")
    return EXIT["MEASURED"] if probes else EXIT["FAIL"]


if __name__ == "__main__":
    raise SystemExit(main())
