"""Command line entry point: inspect, run cycles, backtest, report, stop."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditLog
from .backtest import run_backtest
from .config import load_config, owner_confirmation_valid
from .data import seconds_per_bar, symbol_key  # noqa: F401
from .engine import Engine
from .report import backtest_markdown, build_report, write_report


def _print(result) -> None:
    print(result.summary())
    for outcome in result.outcomes:
        mark = "APPROVED" if outcome.approved else "rejected"
        print(f"  {outcome.symbol:<10} {outcome.action:<5} {mark:<9} {outcome.reasons[0] if outcome.reasons else ''}")
    for error in result.errors:
        print(f"  ERROR {error}")


def cmd_check(engine: Engine) -> int:
    config = engine.config
    print(f"mode={config.mode} live_enabled={config.live_trading_enabled} owner_confirmed={config.owner_confirmed}")
    print(f"symbols={list(config.symbols)} timeframe={config.timeframe} allocated={config.allocated_capital}")
    print(f"kill_switch={'ACTIVE' if engine.kill_switch_active() else 'off'} file={config.kill_switch_file}")
    print(f"proxy={config.proxy or 'none'}")
    for warning in config.warnings:
        print(f"  CONFIG WARNING {warning}")
    clock = engine.clock_check()
    print(f"clock {clock}")
    ok = bool(clock.get("acceptable"))
    for symbol in config.symbols:
        try:
            snap = engine.snapshot(symbol)
            print(f"  {snap.symbol}: bars={len(snap.klines)} mid={snap.quote.mid:.2f} spread={snap.quote.spread_pct:.3f}% "
                  f"fresh={snap.quality.is_fresh} warnings={snap.quality.warnings}")
            ok = ok and snap.quality.is_fresh
        except Exception as exc:  # noqa: BLE001 - the check command reports, never trades
            print(f"  {symbol}: DATA ERROR {type(exc).__name__}: {exc}")
            ok = False
    print("check:", "PASS" if ok else "FAIL (fail-closed: no orders will be sent)")
    return 0 if ok else 1


def cmd_cycle(engine: Engine, dry_run: bool) -> int:
    _print(engine.run_cycle(dry_run=dry_run))
    return 0


def cmd_loop(engine: Engine, cycles: int, interval: int, dry_run: bool) -> int:
    for index in range(1, cycles + 1):
        engine.run_cycle(dry_run=dry_run)
        now = datetime.now(timezone.utc)
        config = engine.config
        prices = dict(engine.current_prices)
        print(f"[{index}/{cycles}] {now.isoformat(timespec='seconds')} equity={engine.state.equity(prices):.2f} "
              f"realized={engine.state.realized_pnl:+.3f} open={len(engine.state.positions)} "
              f"trades_today={engine.state.trades_today}")
        if engine.kill_switch_active():
            print("kill switch active — loop stops in safe state")
            break
        if index < cycles:
            time.sleep(interval)
    return 0


def cmd_status(engine: Engine) -> int:
    state = engine.state
    prices = {}
    for symbol in engine.config.symbols:
        try:
            prices[symbol] = engine.gate.quote(symbol).mid
        except Exception as exc:  # noqa: BLE001
            print(f"quote unavailable for {symbol}: {exc}")
    print(json.dumps({
        "mode": engine.config.mode,
        "owner_confirmed": engine.config.owner_confirmed,
        "kill_switch": engine.kill_switch_active(),
        "equity": round(state.equity(prices), 4),
        "cash": round(state.cash, 4),
        "realized_pnl": round(state.realized_pnl, 6),
        "day_pnl": round(state.day_realized_pnl, 6),
        "week_pnl": round(state.week_realized_pnl, 6),
        "unrealized": round(state.unrealized(prices), 6),
        "drawdown_pct": round(state.drawdown_pct(prices), 4),
        "trades_today": state.trades_today,
        "consecutive_errors": state.consecutive_errors,
        "fees_paid": round(state.fees_paid, 6),
        "positions": [vars(p) | {"mark": prices.get(p.symbol)} for p in state.positions.values()],
        "open_orders": [vars(o) for o in engine.broker.orders.values() if o.status == "NEW"],
        "closed_trades": len(state.closed_trades),
    }, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_report(engine: Engine) -> int:
    prices = {symbol: engine.gate.quote(symbol).mid for symbol in engine.config.symbols}
    text = build_report(engine.config, engine.state, prices)
    path = write_report(engine.config, text)
    print(text)
    print(f"saved: {path}")
    return 0


def cmd_backtest(engine: Engine, symbol: str, bars: int, days: int | None, out: Path | None) -> int:
    import time as _time

    pair = symbol.upper().replace("/", "_")
    config = engine.config
    confirm = config.confirm_timeframe
    if days:
        step = seconds_per_bar(config.timeframe)
        end = int(_time.time()) // step * step
        start = end - days * 86400
        klines = engine.gate.klines_range(pair, config.timeframe, start, end)
        higher = engine.gate.klines_range(pair, confirm, start - 200 * seconds_per_bar(confirm), end)
    else:
        klines = engine.gate.klines(pair, config.timeframe, bars)
        higher = engine.gate.klines(pair, confirm, min(1000, max(200, bars // 3)))
    filters = engine.gate.filters(pair)
    if len(klines) < 120 or len(higher) < 80:
        print(f"not enough history: {len(klines)} signal bars, {len(higher)} confirmation bars")
        return 1
    result = run_backtest(config, pair, bars=klines, higher_bars=higher, filters=filters)
    text = backtest_markdown(result)
    if out is None:
        out = engine.config.root / "state" / "reports" / f"backtest_{pair}_{datetime.now(timezone.utc).date().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"saved: {out}")
    return 0


def cmd_datasets(engine: Engine) -> int:
    from .store import open_store

    rows = open_store(engine.config.root).list_datasets()
    if not rows:
        print("no datasets — run `python trader.py fetch-history` or `import-csv` first")
        return 0
    for row in rows:
        span = f"{_iso(row['start_ts'])} → {_iso(row['end_ts'])}"
        print(f"{row['id']}  {row['symbol']:<10} {row['timeframe']:<5} bars={row['bars']:<6} "
              f"{row['origin']:<9} src={row['source']:<6} {span}")
    return 0


def cmd_import_csv(engine: Engine, path: str, symbol: str, timeframe: str | None, note: str) -> int:
    from . import datasets
    from .store import open_store

    try:
        result = datasets.ingest_csv(open_store(engine.config.root), path, symbol=symbol,
                                     timeframe=timeframe, note=note)
    except datasets.DatasetError as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    quality = result["quality"]
    print(f"dataset {result['dataset_id']} — {result['bars']} closed bars "
          f"({quality.get('origin', 'csv')}/{result['timeframe']})")
    for warning in quality.get("warnings", [])[:10]:
        print(f"  warning: {warning}")
    return 0


def cmd_fetch_history(engine: Engine, symbol: str, timeframe: str, days: int) -> int:
    from . import datasets
    from .store import open_store

    try:
        result = datasets.ingest_exchange(open_store(engine.config.root), engine.gate,
                                          symbol, timeframe, days)
    except datasets.DatasetError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1
    quality = result["quality"]
    print(f"dataset {result['dataset_id']} — {result['bars']} bars, coverage {quality.get('coverage_pct')}%, "
          f"gaps {quality.get('gaps', {}).get('count', 0)}, duplicates {quality.get('duplicates', 0)}")
    print(f"source={quality.get('source')} proxy={'yes' if engine.config.proxy else 'no'} — real history, not synthetic")
    return 0


def cmd_quality(engine: Engine, dataset_id: str) -> int:
    from .store import open_store

    report = open_store(engine.config.root).dataset_quality(dataset_id)
    if not report:
        print(f"unknown dataset: {dataset_id}", file=sys.stderr)
        return 1
    for key in ("symbol", "timeframe", "origin", "source", "bars", "closed_bars", "duplicates",
                "impossible_ohlc", "non_positive_prices", "coverage_pct", "is_stale", "stale_seconds"):
        if key in report:
            print(f"{key}: {report[key]}")
    gaps = report.get("gaps") or {}
    print(f"gaps: {gaps.get('count', 0)} ranges / {gaps.get('missing_bars', 0)} missing bars")
    for warning in report.get("warnings", []):
        print(f"  warning: {warning}")
    return 0


def cmd_versions(engine: Engine) -> int:
    from .store import open_store

    rows = open_store(engine.config.root).list_strategy_versions()
    if not rows:
        print("no strategy versions recorded yet — run a research cycle first")
        return 0
    for row in rows:
        mark = "champion" if row.get("accepted") else "rejected"
        train = row.get("train") or {}
        print(f"{row['name']}:{row['version']}@{row['digest']:<12} {mark:<9} parent={row.get('parent') or '-'}")
        print(f"    train: net={train.get('net_return_pct', '—')}% trades={train.get('trades', '—')} "
              f"pf={train.get('profit_factor', '—')} dd={train.get('max_drawdown_pct', '—')}%")
        if row.get("decision"):
            print(f"    decision: {row['decision']}")
    return 0


def cmd_runs(engine: Engine, kind: str | None) -> int:
    from .store import open_store

    rows = open_store(engine.config.root).list_runs(kind=kind)
    if not rows:
        print("no runs recorded yet")
        return 0
    for row in rows:
        metrics = row.get("metrics") or {}
        print(f"{row['id']}  {row['kind']:<11} {row['symbol']:<10} {row['timeframe']:<5} "
              f"{row['status']:<9} {row['params_label']}\n"
              f"    net={metrics.get('net_return_pct', '—')}% trades={metrics.get('trades', '—')} "
              f"pf={metrics.get('profit_factor', '—')} dd={metrics.get('max_drawdown_pct', '—')}%")
    return 0


def cmd_serve(port: int) -> int:
    from .serve import main as serve_main

    return serve_main(["--port", str(port)])


def _iso(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="minutes")


def cmd_research(engine: Engine, symbol: str, timeframe: str | None, days: int, dataset: str | None,
                 grid_spec: str | None, folds: int) -> int:
    """Bounded grid + walk-forward + acceptance gate. A rejection is a normal outcome."""
    from . import datasets
    from .research import run_research
    from .store import open_store

    config = engine.config
    # Venue-canonical identity: Gate wants BTC_USDT, TSE wants its numeric insCode.
    pair = symbol_key(engine.gate, symbol)
    tf = timeframe or config.timeframe
    store = open_store(config.root)
    if dataset:
        dataset_id = dataset
        bars = datasets.load_klines(store, dataset_id)
    else:
        created = datasets.ingest_exchange(store, engine.gate, pair, tf, days)
        dataset_id = created["dataset_id"]
        bars = datasets.load_klines(store, dataset_id)
    if len(bars) < 400:
        print(f"not enough bars for a split: {len(bars)} (need >= 400 for train/validation/test)")
        return 1
    try:
        higher = datasets.resample(bars, config.confirm_timeframe)
    except datasets.DatasetError:
        higher = list(bars)
    filters = _filters_for(pair, config)
    grid = _parse_grid(grid_spec)
    if grid is None:
        return 1
    result = run_research(store, config, symbol=pair, timeframe=tf, bars=bars, higher_bars=higher,
                          filters=filters, grid=grid, folds=folds, dataset_id=dataset_id)
    print(f"dataset {dataset_id}: {len(bars)} bars, split {result['split']['train']['bars']} train / "
          f"{result['split']['validation']['bars']} validation / {result['split']['test']['bars']} test")
    print(f"candidate {result['candidate_label']} vs champion {result['champion_label']}")
    print(f"VERDICT: {result['verdict'].upper()}")
    print(f"  {result['decision']}")
    wf = result["walk_forward"]["aggregate"]
    print(f"  candidate walk-forward: net={wf.get('net_return_pct')}% trades={wf.get('trades')} "
          f"pf={wf.get('profit_factor')} dd={wf.get('max_drawdown_pct')}%")
    cwf = result["champion_walk_forward"]["aggregate"]
    print(f"  champion walk-forward:  net={cwf.get('net_return_pct')}% trades={cwf.get('trades')} "
          f"pf={cwf.get('profit_factor')} dd={cwf.get('max_drawdown_pct')}%")
    print(f"  grid evaluated {len(result['grid'])} candidates; risk checks: {result['risk_checks']}")
    print(f"  {result['disclaimer']}")
    return 0


def cmd_month_scan(engine: Engine, days: int, warmup: int, timeframe: str | None, strategies: str | None,
                   refresh: bool, improve: bool, out: Path | None, max_hold: int = 0) -> int:
    """Multi-strategy scan over the approved universe; writes a report, never an order."""
    from .data import DataUnavailable
    from .month_scan import ScanOptions, run_scan, write_report
    from .strategy_lab import LAB_STRATEGIES

    config = engine.config
    names = tuple(item.strip() for item in strategies.split(",") if item.strip()) if strategies else tuple(LAB_STRATEGIES)
    unknown = [name for name in names if name not in LAB_STRATEGIES]
    if unknown:
        print(f"unknown strategies: {', '.join(unknown)}; choose from {', '.join(LAB_STRATEGIES)}", file=sys.stderr)
        return 2
    options = ScanOptions(
        days=days, warmup_bars=warmup, timeframe=timeframe or config.timeframe,
        strategies=names, refresh=refresh, improve=improve, max_hold_bars=max_hold,
    )
    try:
        scan = run_scan(config, options)
    except (DataUnavailable, ValueError) as exc:
        print(f"month-scan failed: {exc}", file=sys.stderr)
        return 1
    markdown, payload = write_report(scan, out, config)
    print(f"report: {markdown}")
    print(f"json:   {payload}")
    for name, block in scan["strategies"].items():
        train, validation = block["train"], block["validation"]
        print(f"  {name:<14} train trades={train['trades']:>4} pf={train['profit_factor']} net={train['mean_net_return_pct']}% | "
              f"validation trades={validation['trades']:>4} pf={validation['profit_factor']} net={validation['mean_net_return_pct']}%")
    chosen = (scan.get("improvement") or {}).get("chosen")
    if chosen:
        print(f"  improvement chosen on train: {chosen['label']} -> validation pf={chosen['validation']['profit_factor']} "
              f"net={chosen['validation']['mean_net_return_pct']}% trades={chosen['validation']['trades']}")
    print(f"  {scan['meta']['disclaimer']}")
    return 0


def _filters_for(pair: str, config):
    from .data import SymbolFilter

    return SymbolFilter(symbol=pair, price_precision=2, qty_precision=6, min_qty=0.0, min_quote=0.0,
                        max_quote=0.0, tradable=True, fee_pct=config.taker_fee_pct)


def _parse_grid(spec: str | None) -> dict[str, list] | None:
    """`--grid "ema_fast=10,20;atr_stop_mult=1.5,2.5"` -> bounded dict, or None with a hint."""
    import json
    from .research import MAX_GRID_EVALS

    if not spec:
        print('give a bounded grid, e.g. --grid "ema_fast=10,20;atr_stop_mult=1.5,2.5" '
              'or --grid \'{"ema_fast":[10,20]}\'')
        return None
    grid: dict[str, list] = {}
    if spec.strip().startswith("{"):
        try:
            raw = json.loads(spec)
        except json.JSONDecodeError as exc:
            print(f"grid json invalid: {exc}", file=sys.stderr)
            return None
        if not isinstance(raw, dict):
            print("grid must be an object of param -> list of values", file=sys.stderr)
            return None
        grid = {str(k): (v if isinstance(v, list) else [v]) for k, v in raw.items()}
    else:
        for clause in spec.split(";"):
            if not clause.strip():
                continue
            key, _, values = clause.partition("=")
            if not values:
                print(f"grid clause needs key=v1,v2: {clause!r}", file=sys.stderr)
                return None
            parsed: list = []
            for token in values.split(","):
                token = token.strip()
                try:
                    parsed.append(int(token) if token.lstrip("-").isdigit() else float(token))
                except ValueError:
                    parsed.append(token)
            grid[key.strip()] = parsed
    evaluations = 1
    for values in grid.values():
        evaluations *= max(len(values), 1)
    if evaluations > MAX_GRID_EVALS:
        print(f"grid too large: {evaluations} evaluations (max {MAX_GRID_EVALS}) — "
              "wide searches overfit, narrow it", file=sys.stderr)
        return None
    return grid


def cmd_profiles(engine: Engine, days: int, warmup: int, timeframe: str | None, horizon: str,
                 top: int | None, refresh: bool, round_no: int, out_dir: Path | None) -> int:
    """Run the staged experiment ladder and store profit/loss per stage. Never trades."""
    from .data import DataUnavailable
    from .profiles import ProfileOptions, run_profiles, save_profiles

    if horizon not in ("day", "short"):
        print("horizon must be 'day' or 'short'", file=sys.stderr)
        return 2
    config = engine.config
    options = ProfileOptions(
        days=days, warmup_bars=warmup, timeframe=timeframe, horizon=horizon,
        top=top, refresh=refresh, round_no=round_no,
    )
    try:
        payload = run_profiles(config, options)
    except (DataUnavailable, ValueError) as exc:
        print(f"profiles failed: {exc}", file=sys.stderr)
        return 1
    path, report = save_profiles(config, payload, out_dir)
    meta = payload["meta"]
    print(f"run:    {meta['run_id']} ({meta['market']} {meta['timeframe']} {meta['days']}d, horizon={meta['horizon']})")
    print(f"json:   {path}")
    print(f"report: {report}")
    for stage in payload["stages"]:
        stats = stage["all"]
        print(f"  {stage['id']:<22} trades={stats['trades']:>4} net={stats['net_pnl']:>10} {meta['currency']} "
              f"pf={stats['profit_factor']} hold_med={stats['median_hold_hours']}h")
    verdict = payload["verdict"]
    print(f"  finalist={verdict['finalist']} promoted={verdict['promoted']} stable={verdict['stable_stages']}")
    print(f"  {meta['disclaimer']}")
    return 0


def cmd_stop(engine: Engine) -> int:
    Path(engine.config.kill_switch_file).write_text(f"{datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
    cancelled = engine.broker.cancel_all("kill_switch")
    engine.broker.persist()
    engine.audit.append("kill_switch_engaged", {"cancelled": len(cancelled)}, mode=engine.config.mode)
    print(f"kill switch written to {engine.config.kill_switch_file}; {len(cancelled)} resting order(s) cancelled")
    print("open positions are NOT force-closed; decide that explicitly")
    return 0


def cmd_resume(engine: Engine) -> int:
    path = Path(engine.config.kill_switch_file)
    if not path.exists():
        print("kill switch not active")
        return 0
    path.unlink()
    engine.audit.append("kill_switch_released", {"operator": "owner"}, mode=engine.config.mode)
    print(f"removed {path} — trading resumes in {engine.config.mode}")
    return 0


def cmd_enable_live(engine: Engine, owner: str, capital: float) -> int:
    """Owner-only action. The agent calls this only on explicit human instruction."""
    if engine.config.mode != "LIVE":
        print("config mode is PAPER; live stays off. Set trading.mode=LIVE and live_trading_enabled=true first, then re-run.")
    config = load_config()
    from .config import config_digest, owner_confirmation_path

    record = {
        "action": "ENABLE_LIVE_TRADING",
        "owner": owner,
        "allocated_capital": capital,
        "live_trading_enabled": True,
        "config_digest": config_digest(config.raw),
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = owner_confirmation_path(config.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    live_ready = owner_confirmation_valid(config.root, config) and config.mode == "LIVE"
    print(f"owner record written: {path}")
    print(f"live gate satisfied: {live_ready}")
    if live_ready:
        print("WARNING: this build has no live exchange adapter, so orders still cannot reach an exchange.")
    return 0


def cmd_verify_audit(engine: Engine) -> int:
    log = AuditLog(engine.config.root / "state" / "audit" / "events.jsonl")
    ok, message = log.verify()
    print(f"audit chain {'OK' if ok else 'BROKEN'}: {message}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="cat", description="Agentic crypto trader (PAPER by default)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--capital", type=float, default=None, help="paper starting capital (first run only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    cycle = sub.add_parser("cycle")
    cycle.add_argument("--dry-run", action="store_true")
    loop = sub.add_parser("loop")
    loop.add_argument("--cycles", type=int, default=24)
    loop.add_argument("--interval", type=int, default=3600)
    loop.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    sub.add_parser("report")
    bt = sub.add_parser("backtest")
    bt.add_argument("--symbol", default="BTC/USDT")
    bt.add_argument("--bars", type=int, default=900)
    bt.add_argument("--days", type=int, default=None, help="paged history length in days")
    bt.add_argument("--out", default=None)
    sub.add_parser("stop")
    sub.add_parser("resume")
    sub.add_parser("verify-audit")
    live = sub.add_parser("enable-live")
    live.add_argument("--owner", required=True)
    live.add_argument("--capital", type=float, required=True)
    serve = sub.add_parser("serve", help="run the loopback API + dashboard (never binds a public address)")
    serve.add_argument("--port", type=int, default=8177)
    fetch = sub.add_parser("fetch-history", help="store real exchange candles as a dataset")
    fetch.add_argument("--symbol", default="BTC/USDT")
    fetch.add_argument("--timeframe", default=None)
    fetch.add_argument("--days", type=int, default=365)
    imp = sub.add_parser("import-csv", help="ingest an OHLCV csv as a dataset")
    imp.add_argument("--path", required=True)
    imp.add_argument("--symbol", required=True)
    imp.add_argument("--timeframe", default=None)
    imp.add_argument("--note", default="")
    sub.add_parser("datasets", help="list stored datasets")
    qual = sub.add_parser("quality", help="validation report for one dataset")
    qual.add_argument("--dataset", required=True)
    runs = sub.add_parser("runs", help="list recorded backtests/research runs")
    runs.add_argument("--kind", default=None)
    sub.add_parser("versions", help="list strategy versions with their accept/reject decision")
    res = sub.add_parser("research", help="bounded grid + walk-forward + acceptance gate (paper only)")
    res.add_argument("--symbol", default="BTC/USDT")
    res.add_argument("--timeframe", default=None)
    res.add_argument("--days", type=int, default=1500)
    res.add_argument("--dataset", default=None, help="existing dataset id instead of a fresh fetch")
    res.add_argument("--grid", default=None, help='"ema_fast=10,20;atr_stop_mult=1.5,2.5" or a JSON object')
    res.add_argument("--folds", type=int, default=4)
    scan = sub.add_parser("month-scan", help="one-month, multi-strategy scan over the approved universe (read-only)")
    scan.add_argument("--days", type=int, default=30)
    scan.add_argument("--warmup", type=int, default=180)
    scan.add_argument("--timeframe", default=None)
    scan.add_argument("--strategies", default=None, help="comma separated: trend_v1,breakout_30,momentum_20,pullback_rsi")
    scan.add_argument("--refresh", action="store_true", help="ignore the cached candles and fetch again")
    scan.add_argument("--no-improve", action="store_true", help="skip the train-selected improvement phase")
    scan.add_argument("--max-hold", type=int, default=0, help="force-close a position after N bars (0 = off; e.g. 3 on 1h = 3 hours)")
    scan.add_argument("--out", default=None)
    prof = sub.add_parser("profiles", help="staged experiment profiles with per-stage profit/loss (read-only)")
    prof.add_argument("--days", type=int, default=30)
    prof.add_argument("--warmup", type=int, default=180)
    prof.add_argument("--timeframe", default=None)
    prof.add_argument("--horizon", choices=["day", "short"], default="day",
                      help="day = close before ~24h; short = as close to 3h as the timeframe allows")
    prof.add_argument("--top", type=int, default=None, help="only the N most liquid symbols of the universe")
    prof.add_argument("--round", type=int, choices=[1, 2, 3], default=1,
                      help="1 = entry-family ladder; 2 = fresh-cross fee-churn test; 3 = pullback sensitivity")
    prof.add_argument("--refresh", action="store_true", help="ignore the cached candles and fetch again")
    prof.add_argument("--out-dir", default=None)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        return cmd_serve(args.port)

    engine = Engine(config, allocated_capital=args.capital)

    if args.command == "check":
        return cmd_check(engine)
    if args.command == "cycle":
        return cmd_cycle(engine, args.dry_run)
    if args.command == "loop":
        return cmd_loop(engine, args.cycles, args.interval, args.dry_run)
    if args.command == "status":
        return cmd_status(engine)
    if args.command == "report":
        return cmd_report(engine)
    if args.command == "backtest":
        return cmd_backtest(engine, args.symbol, args.bars, args.days, Path(args.out) if args.out else None)
    if args.command == "fetch-history":
        return cmd_fetch_history(engine, args.symbol, args.timeframe or config.timeframe, args.days)
    if args.command == "import-csv":
        return cmd_import_csv(engine, args.path, args.symbol, args.timeframe, args.note)
    if args.command == "datasets":
        return cmd_datasets(engine)
    if args.command == "quality":
        return cmd_quality(engine, args.dataset)
    if args.command == "runs":
        return cmd_runs(engine, args.kind)
    if args.command == "versions":
        return cmd_versions(engine)
    if args.command == "research":
        return cmd_research(engine, args.symbol, args.timeframe, args.days, args.dataset, args.grid, args.folds)
    if args.command == "month-scan":
        return cmd_month_scan(engine, args.days, args.warmup, args.timeframe, args.strategies, args.refresh, not args.no_improve, Path(args.out) if args.out else None, args.max_hold)
    if args.command == "profiles":
        return cmd_profiles(engine, args.days, args.warmup, args.timeframe, args.horizon, args.top, args.refresh,
                            args.round, Path(args.out_dir) if args.out_dir else None)
    if args.command == "stop":
        return cmd_stop(engine)
    if args.command == "resume":
        return cmd_resume(engine)
    if args.command == "verify-audit":
        return cmd_verify_audit(engine)
    if args.command == "enable-live":
        return cmd_enable_live(engine, args.owner, args.capital)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
