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
from .data import seconds_per_bar  # noqa: F401
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
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return 2
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
