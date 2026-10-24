"""Staged experiment profiles: one named profile per development round.

Each stage is a complete, independently specified configuration (entry family,
ATR bracket, optional market-breadth gate, holding horizon). The runner measures
every stage on the same closed candles, splits trades into train/validation by
exit time, and records what each stage actually earned and lost: gross profit,
gross loss, net, fees, win rate, profit factor and holding-time distribution.

Read-only by construction: it runs the same event-driven backtest and Risk Gate
as the paper engine, never places an order, never writes config and never
changes a risk cap. The stage chosen as finalist is picked on the train half
only; the validation half is the judge, not the selector.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from .backtest import run_backtest
from .config import Config
from .data import seconds_per_bar
from .month_scan import (
    ScanOptions,
    _bar_from_row,
    _filters,
    _strategy_callable,
    _universe_volume,
    load_history,
)
from .strategies import StrategyParams, default_params

RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
TRADES_PER_STAGE = 400
CURVE_POINTS = 160


@dataclass
class Stage:
    id: str
    title: str
    change: str
    strategy: str
    params: StrategyParams
    horizon: str = "none"  # none | day | short
    extra: dict[str, Any] | None = None


@dataclass
class ProfileOptions:
    days: int = 30
    warmup_bars: int = 180
    timeframe: str | None = None
    horizon: str = "day"
    top: int | None = None
    refresh: bool = False
    assumed_spread_pct: float | None = None
    out_dir: Path | None = None


def horizon_bars(timeframe: str, horizon: str) -> int:
    """Bars that keep a trade inside the requested horizon.

    On a 4h chart the shortest possible hold is one bar (4h), so an "under a day"
    target becomes 5 bars (20h) and a "2-3h" target cannot be met at all — the
    measurement says so instead of relabelling 4h as 3h.
    """
    bar_hours = seconds_per_bar(timeframe) / 3600.0
    if horizon == "short":
        return max(1, int(3.0 // bar_hours))
    if horizon == "day":
        return max(1, int(24.0 // bar_hours) - 1)
    return 0


def stage_plan(timeframe: str, horizon: str = "day") -> list[Stage]:
    """The development ladder. Every stage changes one thing from the one before.

    The bracket stays at 1.5/3.5 ATR on purpose: tightening it to 1.2/1.8 makes
    the net reward/risk gate reject every entry once the 0.6% round-trip cost is
    subtracted, which measures nothing. The horizon and the volatility floor are
    the two levers that actually fit a short hold.
    """
    cap = horizon_bars(timeframe, horizon)
    bar_hours = seconds_per_bar(timeframe) / 3600.0
    hours = round(cap * bar_hours, 1)
    base = default_params()
    timed = base.replace(max_hold_bars=cap)
    return [
        Stage(
            "baseline-trend", "پایه‌ی تولیدی", "نسخه‌ی فعلی موتور: ورود فقط در روند صعودی تأییدشده با براکت ۱٫۵/۳٫۵ ATR",
            "trend_v1", base, horizon="none",
        ),
        Stage(
            "momentum", "مومنتوم ۲۰", "خانواده‌ی ورود عوض شد: مومنتوم ۲۰ کندلی به‌جای تأییدهای چندگانه‌ی روند",
            "momentum_20", base, horizon="none",
        ),
        Stage(
            "momentum-horizon", "مومنتوم + افق", f"تنها تغییر: بستن اجباری معامله پس از {hours} ساعت",
            "momentum_20", timed, horizon=horizon,
        ),
        Stage(
            "momentum-vol-horizon", "مومنتوم + نوسان کافی + افق",
            f"فقط نمادهایی که ATR آن‌ها ≥ ۱٫۲٪ است تا هزینه‌ی رفت‌وبرگشت در {hours} ساعت قابل جبران باشد",
            "momentum_20", timed, horizon=horizon, extra={"min_atr_pct": 1.2},
        ),
        Stage(
            "breakout-vol-horizon", "شکست سقف + نوسان + افق",
            f"ورود با شکست سقف ۳۰ کندل، فیلتر نوسان ۱٫۲٪ و افق {hours} ساعت",
            "breakout_30", timed, horizon=horizon, extra={"min_atr_pct": 1.2},
        ),
        Stage(
            "trend-horizon", "پایه + افق", f"منطق پایه‌ی تولیدی با افق {hours} ساعت — محافظه‌کارانه‌ترین گزینه",
            "trend_v1", timed, horizon=horizon,
        ),
    ]


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

def _in_segment(trade: dict[str, Any], split_ts: int, segment: str) -> bool:
    """Train = closed before the midpoint, validation = closed after it.

    Partitioning by exit time keeps every trade counted exactly once and never
    lets a validation trade influence the train statistics it is judged against.
    """
    if segment == "all":
        return True
    if segment == "train":
        return int(trade["exit_time"]) < split_ts
    return int(trade["exit_time"]) >= split_ts


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def pooled_stats(rows: list[dict[str, Any]], segment: str, capital: float) -> dict[str, Any]:
    """Profit, loss and holding-time statistics for one stage and one segment."""
    picked: list[dict[str, Any]] = []
    per_symbol: dict[str, float] = {}
    traded: set[str] = set()
    for row in rows:
        trades = [trade for trade in row["trades"] if _in_segment(trade, row["split_ts"], segment)]
        net = sum(float(trade["pnl"]) for trade in trades)
        per_symbol[row["symbol"]] = net
        if trades:
            traded.add(row["symbol"])
            picked.extend(trades)

    wins = [trade for trade in picked if float(trade["pnl"]) > 0]
    losses = [trade for trade in picked if float(trade["pnl"]) <= 0]
    gross_profit = sum(float(trade["pnl"]) for trade in wins)
    gross_loss = sum(float(trade["pnl"]) for trade in losses)
    holds = [float(trade["holding_hours"]) for trade in picked if float(trade["holding_hours"]) > 0]
    returns = [value / capital * 100.0 for value in per_symbol.values()] if capital else []
    exits = Counter(str(trade["reason"]) for trade in picked)
    drawdowns = [float(row["metrics"]["max_drawdown_pct"]) for row in rows if row["metrics"].get("max_drawdown_pct") is not None]

    return {
        "trades": len(picked),
        "symbols": len(rows),
        "symbols_with_trades": len(traded),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "net_pnl": round(gross_profit + gross_loss, 4),
        "fees_sum": round(sum(float(trade["fees"]) for trade in picked), 4),
        "win_rate_pct": round(len(wins) / len(picked) * 100.0, 2) if picked else None,
        "profit_factor": round(gross_profit / abs(gross_loss), 3) if gross_loss < 0 else None,
        "avg_win": round(gross_profit / len(wins), 4) if wins else None,
        "avg_loss": round(gross_loss / len(losses), 4) if losses else None,
        "best_trade": round(max((float(t["pnl"]) for t in picked), default=0.0), 4) if picked else None,
        "worst_trade": round(min((float(t["pnl"]) for t in picked), default=0.0), 4) if picked else None,
        "mean_net_return_pct": round(sum(returns) / len(returns), 3) if returns else None,
        "median_net_return_pct": round(median(returns), 3) if returns else None,
        "positive_symbols_pct": round(sum(1 for value in returns if value > 0) / len(returns) * 100.0, 1) if returns else None,
        "median_hold_hours": round(median(holds), 2) if holds else None,
        "mean_hold_hours": round(sum(holds) / len(holds), 2) if holds else None,
        "within_day_pct": round(sum(1 for value in holds if value <= 24.0) / len(holds) * 100.0, 1) if holds else None,
        "within_3h_pct": round(sum(1 for value in holds if value <= 3.0) / len(holds) * 100.0, 1) if holds else None,
        "exits": dict(sorted(exits.items(), key=lambda item: item[1], reverse=True)),
        "median_max_drawdown_pct": round(median(drawdowns), 3) if drawdowns and segment == "all" else None,
    }


def _symbol_rows(rows: list[dict[str, Any]], capital: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Best and worst symbols by realized net return, after at least one trade."""
    scored = []
    for row in rows:
        if not row["trades"]:
            continue
        net = sum(float(trade["pnl"]) for trade in row["trades"])
        scored.append({
            "symbol": row["symbol"],
            "trades": len(row["trades"]),
            "net_pnl": round(net, 4),
            "net_return_pct": round(net / capital * 100.0, 3) if capital else None,
            "win_rate_pct": round(sum(1 for t in row["trades"] if float(t["pnl"]) > 0) / len(row["trades"]) * 100.0, 2),
            "fees": round(sum(float(t["fees"]) for t in row["trades"]), 4),
        })
    scored.sort(key=lambda item: float(item["net_pnl"]))
    return scored[-5:][::-1], scored[:5]


def _basket_curve(curves: list[list[tuple[int, float]]], capital: float) -> list[list[float]]:
    """Equal-weight basket of independent accounts, as percent change.

    It is not a portfolio: each symbol trades its own account with the same
    starting capital, and the curve only answers "did this stage, on average,
    move in the right direction".
    """
    buckets: dict[int, list[float]] = {}
    for curve in curves:
        for ts, equity in curve:
            if capital:
                buckets.setdefault(int(ts), []).append(float(equity) / capital)
    points = sorted((ts, sum(values) / len(values)) for ts, values in buckets.items() if values)
    if not points:
        return []
    step = max(1, len(points) // CURVE_POINTS)
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    base = sampled[0][1]
    return [[ts, round((value / base - 1.0) * 100.0, 3)] for ts, value in sampled]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _resolve_symbols(config: Config, options: ProfileOptions) -> list[str]:
    symbols = [str(symbol).replace("/", "_") for symbol in config.symbols]
    volumes = _universe_volume(config)
    if options.top and volumes:
        ranked = sorted(symbols, key=lambda symbol: volumes.get(symbol, 0.0), reverse=True)
        return ranked[: options.top]
    return symbols


def _run_stage(
    config: Config,
    data: dict[str, Any],
    symbols: list[str],
    stage: Stage,
    options: ProfileOptions,
    *,
    timeframe: str,
    capital: float,
    spread_pct: float,
    progress: Callable[[str], None],
) -> dict[str, Any]:
    step = seconds_per_bar(timeframe)
    signal = _strategy_callable(stage.strategy, stage.extra or {})
    rows: list[dict[str, Any]] = []
    curves: list[list[tuple[int, float]]] = []
    skipped: dict[str, str] = {}
    started = time.time()

    for index, symbol in enumerate(symbols, 1):
        raw = data.get(symbol)
        if not raw:
            skipped[symbol] = "missing"
            continue
        bars = [_bar_from_row(row) for row in raw["bars"]]
        higher = [_bar_from_row(row) for row in raw["higher"]]
        if len(bars) < options.warmup_bars + 30 or len(higher) < 60:
            skipped[symbol] = f"insufficient_history:{len(bars)}/{len(higher)}"
            continue
        test_bars = options.days * 86400 // step
        start = max(len(bars) - test_bars, options.warmup_bars)
        if start >= len(bars) - 5:
            skipped[symbol] = "empty_window"
            continue
        try:
            result = run_backtest(
                config, symbol, bars=bars, higher_bars=higher, filters=_filters(raw["filters"]),
                timeframe=timeframe, warmup=start, assumed_spread_pct=spread_pct,
                params=stage.params, propose_fn=signal,
            )
        except Exception as exc:  # noqa: BLE001 - one symbol must not kill the stage
            skipped[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        trades = [
            {
                "symbol": trade.symbol, "pnl": round(trade.pnl, 6), "fees": round(trade.fees, 6),
                "reason": trade.reason, "entry_time": trade.entry_time, "exit_time": trade.exit_time,
                "entry": round(trade.entry, 8), "exit": round(trade.exit, 8), "qty": round(trade.qty, 8),
                "holding_hours": round(max(0.0, (trade.exit_time - trade.entry_time) / 3600.0), 2),
                "return_pct": round((trade.exit / trade.entry - 1) * 100.0, 3) if trade.entry else None,
            }
            for trade in result.trades
        ]
        window_start = bars[start].open_time + step
        window_end = bars[-1].open_time + step
        rows.append({
            "symbol": symbol,
            "metrics": result.metrics,
            "buy_hold_pct": round(result.buy_hold_pct, 3),
            "trades": trades,
            "split_ts": window_start + (window_end - window_start) // 2,
        })
        curves.append(result.equity_curve)
        if index % 25 == 0:
            progress(f"  {stage.id}: {index}/{len(symbols)} symbols, {len(rows)} measured, {time.time() - started:.0f}s")

    top, bottom = _symbol_rows(rows, capital)
    all_stats = pooled_stats(rows, "all", capital)
    ordered = sorted(
        (trade for row in rows for trade in row["trades"]),
        key=lambda trade: int(trade["exit_time"]), reverse=True,
    )
    return {
        "id": stage.id,
        "title": stage.title,
        "change": stage.change,
        "strategy": stage.strategy,
        "horizon": stage.horizon,
        "max_hold_bars": stage.params.max_hold_bars,
        "max_hold_hours": round(stage.params.max_hold_bars * step / 3600.0, 2) if stage.params.max_hold_bars else None,
        "params": stage.params.as_dict(),
        "all": all_stats,
        "train": pooled_stats(rows, "train", capital),
        "validation": pooled_stats(rows, "validation", capital),
        "top_symbols": top,
        "bottom_symbols": bottom,
        "trades": ordered[:TRADES_PER_STAGE],
        "trades_truncated": max(0, len(ordered) - TRADES_PER_STAGE),
        "skipped": skipped,
        "basket_curve": _basket_curve(curves, capital),
    }


def _verdict(stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick a finalist on train, judge it once on validation, and name stable stages."""
    eligible = [
        stage for stage in stages
        if stage["train"]["trades"] >= 20 and stage["train"]["profit_factor"] is not None
    ]
    finalist = max(
        eligible,
        key=lambda stage: (stage["train"]["profit_factor"] or 0.0, stage["train"]["mean_net_return_pct"] or -99.0),
    ) if eligible else None
    promoted = False
    if finalist is not None:
        validation = finalist["validation"]
        promoted = (
            validation["trades"] >= 15
            and (validation["profit_factor"] or 0.0) >= 1.0
            and (validation["net_pnl"] or 0.0) > 0
        )
    stable = [
        stage["id"] for stage in stages
        if stage["all"]["trades"] >= 20
        and (stage["all"]["profit_factor"] or 0.0) >= 1.2
        and (stage["all"]["net_pnl"] or 0.0) > 0
        and (stage["train"]["net_pnl"] or 0.0) > 0
        and (stage["validation"]["net_pnl"] or 0.0) > 0
    ]
    def _judged(stage: dict[str, Any]) -> str:
        validation = stage["validation"]
        pf = validation["profit_factor"]
        return (
            f"PF {'—' if pf is None else f'{pf:.2f}'}، "
            f"سود خالص {validation['net_pnl']:,.0f}"
        )

    if finalist is None:
        text = "هیچ مرحله‌ای حداقل ۲۰ معامله در نیمه‌ی train نداشت؛ داوری ممکن نیست و هیچ نسخه‌ای ترفیع نمی‌گیرد."
    elif promoted:
        text = (
            f"مرحله‌ی «{finalist['id']}» روی train انتخاب شد و روی validation هم مثبت ماند "
            f"({_judged(finalist)}). این یک نامزد است، نه مجوز معامله‌ی زنده."
        )
    else:
        text = (
            f"مرحله‌ی «{finalist['id']}» روی train بهترین بود اما روی validation تکرار نشد "
            f"({_judged(finalist)}). در این نمونه هیچ نسخه‌ای برای ترفیع تأیید نمی‌شود."
        )
    return {
        "finalist": finalist["id"] if finalist else None,
        "promoted": promoted,
        "stable_stages": stable,
        "text_fa": text,
        "selection_rule": "بیشترین profit factor روی نیمه‌ی اول با حداقل ۲۰ معامله؛ داوری یک‌باره روی نیمه‌ی دوم",
        "stable_rule": "PF کل ≥ ۱٫۲، حداقل ۲۰ معامله، سود خالص مثبت در کل و در هر دو نیمه",
    }


def _currency(config: Config) -> str:
    return "IRR" if config.market == "tse" else "USDT"


def _disclaimer(capital: float, currency: str) -> str:
    return (
        f"این اعداد بازده ثبت‌شده‌ی گذشته‌اند؛ نه پیش‌بینی، نه تضمین و نه مجوز معامله‌ی زنده. "
        f"هر نماد یک حساب مستقل با سرمایه‌ی {capital:,.0f} {currency} دارد؛ جمع آن‌ها پرتفوی واقعی نیست."
    )


def run_profiles(
    config: Config,
    options: ProfileOptions | None = None,
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    options = options or ProfileOptions()
    timeframe = options.timeframe or config.timeframe
    symbols = _resolve_symbols(config, options)
    if not symbols:
        raise ValueError("config has no symbols")
    spread = options.assumed_spread_pct
    if spread is None:
        # The Tehran board quotes a real spread; 0.05% would flatter every TSE stage.
        spread = 0.30 if config.market == "tse" else 0.05
    capital = float(config.allocated_capital or 1000.0)

    scan_options = ScanOptions(
        days=options.days, warmup_bars=options.warmup_bars, timeframe=timeframe,
        refresh=options.refresh,
    )
    data = load_history(config, symbols, scan_options, progress=progress)
    available = [symbol for symbol in symbols if symbol in data.get("symbols", {})]
    if not available:
        raise ValueError("no symbol returned usable candles")
    stages = stage_plan(timeframe, options.horizon)

    progress(f"profiles: {len(stages)} stages × {len(available)} symbols on {timeframe}, capital {capital:,.0f} {_currency(config)}")
    measured: list[dict[str, Any]] = []
    for stage in stages:
        payload = _run_stage(
            config, data["symbols"], available, stage, options,
            timeframe=timeframe, capital=capital, spread_pct=spread, progress=progress,
        )
        measured.append(payload)
        total = payload["all"]
        progress(
            f"profiles: {stage.id} trades={total['trades']} net={total['net_pnl']} "
            f"pf={total['profit_factor']} hold_med={total['median_hold_hours']}h"
        )

    coverage_end = int(data.get("coverage_end") or 0)
    generated = datetime.now(timezone.utc)
    run_id = (
        f"{config.market}-{timeframe}-{options.days}d{'-short' if options.horizon == 'short' else ''}"
        f"-{generated:%Y%m%dT%H%M%S}"
    ).lower()
    meta = {
        "run_id": run_id,
        "generated_at": generated.isoformat(timespec="seconds"),
        "market": config.market,
        "venue": config.spec.name,
        "timeframe": timeframe,
        "days": options.days,
        "warmup_bars": options.warmup_bars,
        "horizon": options.horizon,
        "horizon_note": (
            "افق «کوتاه» روی هر تایم‌فریم به نزدیک‌ترین تعداد کندل گرد می‌شود؛ روی ۴ ساعته کوتاه‌ترین نگه‌داشت ممکن ۴ ساعت است."
            if options.horizon == "short" else
            "افق «یک روز» روی هر تایم‌فریم به نزدیک‌ترین تعداد کندل زیر ۲۴ ساعت گرد می‌شود؛ روی کندل روزانه حداقل یک جلسه است."
        ),
        "symbols_requested": len(symbols),
        "symbols_with_data": len(available),
        "data_errors": data.get("errors", {}),
        "source": data.get("source"),
        "coverage_end": coverage_end,
        "coverage_end_iso": datetime.fromtimestamp(coverage_end, tz=timezone.utc).isoformat(timespec="seconds") if coverage_end else None,
        "currency": _currency(config),
        "capital_per_symbol": capital,
        "assumed_spread_pct": spread,
        "split_rule": "train = معامله‌های بسته‌شده پیش از میانه‌ی بازه، validation = پس از آن",
        "disclaimer": _disclaimer(capital, _currency(config)),
        "live_gate": "PAPER-only؛ این اجرا هیچ سفارشی نمی‌سازد و هیچ سقف ریسکی را تغییر نمی‌دهد.",
    }
    payload = {
        "meta": meta,
        "stages": measured,
        "verdict": _verdict(measured),
    }
    return payload


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def profiles_dir(config: Config, out_dir: Path | None = None) -> Path:
    base = Path(out_dir) if out_dir else Path(config.root) / "state" / "research" / "profiles"
    base.mkdir(parents=True, exist_ok=True)
    return base


def save_profiles(config: Config, payload: dict[str, Any], out_dir: Path | None = None) -> tuple[Path, Path]:
    base = profiles_dir(config, out_dir)
    run_id = str(payload["meta"]["run_id"])
    if not RUN_ID.match(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    path = base / f"{run_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    report = base / f"{run_id}.md"
    report.write_text(render_markdown(payload), encoding="utf-8")
    return path, report


def list_runs(config: Config, out_dir: Path | None = None) -> list[dict[str, Any]]:
    base = profiles_dir(config, out_dir)
    runs: list[dict[str, Any]] = []
    for path in base.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = payload.get("meta") or {}
        if not meta.get("run_id"):
            continue
        if meta.get("market") != config.market:
            continue
        stages = payload.get("stages") or []
        runs.append({
            "run_id": meta.get("run_id"),
            "market": meta.get("market"),
            "timeframe": meta.get("timeframe"),
            "days": meta.get("days"),
            "horizon": meta.get("horizon"),
            "generated_at": meta.get("generated_at"),
            "coverage_end": meta.get("coverage_end"),
            "coverage_end_iso": meta.get("coverage_end_iso"),
            "source": meta.get("source"),
            "symbols_with_data": meta.get("symbols_with_data"),
            "currency": meta.get("currency"),
            "stages": len(stages),
            "verdict": payload.get("verdict"),
        })
    runs.sort(key=lambda run: str(run.get("generated_at") or ""), reverse=True)
    return runs


def load_run(config: Config, run_id: str, out_dir: Path | None = None) -> dict[str, Any] | None:
    if not RUN_ID.match(str(run_id)):
        return None
    path = profiles_dir(config, out_dir) / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (payload.get("meta") or {}).get("market") != config.market:
        return None
    return payload


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _fmt(value: Any, digits: int = 2, signed: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:+,.{digits}f}" if signed else f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    currency = meta["currency"]
    lines = [
        f"# پروفایل‌های آزمایش — {meta['market']} · {meta['timeframe']} · {meta['days']} روز",
        "",
        f"- تولید: {meta['generated_at']} · منبع: `{meta['source']}` · آخرین کندل: `{meta.get('coverage_end_iso')}`",
        f"- نمادهای اندازه‌گیری‌شده: {meta['symbols_with_data']} از {meta['symbols_requested']} · "
        f"سرمایه‌ی هر نماد: {_fmt(meta['capital_per_symbol'], 0)} {currency} · spread فرضی: {meta['assumed_spread_pct']}%",
        f"- تقسیم: {meta['split_rule']}",
        f"- ⚠️ {meta['disclaimer']}",
        "",
        "## سود و زیان هر مرحله (کل بازه)",
        "",
        "| مرحله | معامله | سود ناخالص | زیان ناخالص | خالص | کارمزد | برد% | PF | نگه‌داشت میانه (ساعت) | زیر ۱ روز% |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for stage in payload["stages"]:
        stats = stage["all"]
        lines.append(
            f"| {stage['title']} (`{stage['id']}`) | {_fmt(stats['trades'], 0)} "
            f"| {_fmt(stats['gross_profit'], signed=False)} | {_fmt(stats['gross_loss'])} "
            f"| {_fmt(stats['net_pnl'], signed=True)} | {_fmt(stats['fees_sum'])} "
            f"| {_fmt(stats['win_rate_pct'])} | {_fmt(stats['profit_factor'], 3)} "
            f"| {_fmt(stats['median_hold_hours'])} | {_fmt(stats['within_day_pct'], 1)} |"
        )
    lines += [
        "",
        "## داوری train / validation",
        "",
        "| مرحله | معامله train | PF train | خالص train | معامله validation | PF validation | خالص validation |",
        "|---|---|---|---|---|---|---|",
    ]
    for stage in payload["stages"]:
        train, validation = stage["train"], stage["validation"]
        lines.append(
            f"| `{stage['id']}` | {_fmt(train['trades'], 0)} | {_fmt(train['profit_factor'], 3)} "
            f"| {_fmt(train['net_pnl'], signed=True)} | {_fmt(validation['trades'], 0)} "
            f"| {_fmt(validation['profit_factor'], 3)} | {_fmt(validation['net_pnl'], signed=True)} |"
        )
    verdict = payload["verdict"]
    lines += [
        "",
        "## نتیجه",
        "",
        f"- نامزد: `{verdict['finalist']}` · ترفیع: {'بله' if verdict['promoted'] else 'خیر'}",
        f"- {verdict['text_fa']}",
        f"- مرحله‌های پایدار بر اساس قاعده‌ی «{verdict['stable_rule']}»: "
        + (", ".join(f"`{item}`" for item in verdict["stable_stages"]) or "هیچ"),
        "",
        "### توزیع دلیل خروج (کل بازه)",
        "",
    ]
    for stage in payload["stages"]:
        exits = ", ".join(f"{key}={value}" for key, value in (stage["all"]["exits"] or {}).items()) or "—"
        lines.append(f"- `{stage['id']}`: {exits}")
    lines += [
        "",
        "## محدودیت‌های همین اندازه‌گیری",
        "",
        f"- {meta['horizon_note']}",
        "- این مرحله‌ها پس از اسکن قبلی طراحی شده‌اند؛ حتی نیمه‌ی validation هم کاملاً دست‌نخورده نیست.",
        "- کارمزد و اسلیپیج فرضی‌اند و از دفتر کارگزار تأیید نشده‌اند.",
        "- خروج در کندل بعدی اجرا می‌شود؛ اگر حد ضرر و هدف در یک کندل لمس شوند، حد ضرر مقدم است.",
        f"- {meta['live_gate']}",
        "",
    ]
    return "\n".join(lines)
