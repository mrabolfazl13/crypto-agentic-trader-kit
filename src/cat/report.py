"""Daily report generation in the shape of prompts/DAILY_REPORT_TEMPLATE_FA.md."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .portfolio import AccountState

DISCLAIMER = (
    "این گزارش سابقه‌ی ثبت‌شده است، نه پیش‌بینی. بازده گذشته تضمین‌کننده‌ی آینده نیست "
    "و ممکن است تمام سرمایه‌ی اختصاص‌یافته از دست برود."
)


def audit_day(path: Path, day: str) -> dict[str, Any]:
    """Count today's events straight from the append-only log."""
    events: Counter[str] = Counter()
    rejects: Counter[str] = Counter()
    orders = fills = cancels = decisions = approved = 0
    if not path.exists():
        return {"events": events, "rejects": rejects, "orders": orders, "fills": fills,
                "cancels": cancels, "decisions": decisions, "approved": approved}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not record.get("ts", "").startswith(day):
                continue
            event = record.get("event", "?")
            events[event] += 1
            if event == "order_placed":
                orders += 1
            elif event == "order_filled":
                fills += 1
            elif event == "order_cancelled":
                cancels += 1
            elif event == "decision_recorded":
                data = record.get("data", {})
                extras = data.get("extras", {})
                decisions += 1
                if extras.get("risk_approved"):
                    approved += 1
                    continue
                reason = extras.get("strategy_reason") or "unknown"
                rejects[str(reason)] += 1
                for gate_reason in (extras.get("risk_reasons") or [])[:1]:
                    rejects[str(gate_reason).split(":")[0].strip()] += 1
            elif event in ("exit_rejected", "decision_rejected"):
                for reason in (record.get("data", {}).get("reasons") or [record.get("data", {}).get("proposal", "unknown")]):
                    rejects[str(reason).split(":")[0]] += 1
    return {"events": events, "rejects": rejects, "orders": orders, "fills": fills,
            "cancels": cancels, "decisions": decisions, "approved": approved}


def build_report(config: Config, state: AccountState, prices: dict[str, float], *, cycle: Any = None, extra_warnings: list[str] | None = None) -> str:
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    stats = audit_day(config.root / "state" / "audit" / "events.jsonl", day)
    equity = state.equity(prices)
    unrealized = state.unrealized(prices)
    drawdown = state.drawdown_pct(prices)
    exposure = state.exposure_pct(prices)
    positions = state.positions.values()
    kill = Path(config.kill_switch_file).exists()
    limits = config.limits

    lines = [
        f"# گزارش روزانه عامل — {day}",
        "",
        f"- تاریخ/منطقه‌ی زمانی: {now.isoformat(timespec='seconds')} / UTC",
        f"- حالت: {config.mode}" + (" (PAUSED by kill switch)" if kill else ""),
        f"- منبع و تازگی داده: Gate.io public REST + MEXC cross-check؛ ساعت: "
        + (f"skew {cycle.clock.get('skew_seconds')}s" if cycle else "بررسی‌نشده"),
        f"- وضعیت سرویس‌ها: " + ("ایمن (خطای ثبت‌شده دارد)" if (cycle and cycle.errors) else "عادی"),
        f"- دارایی تخصیص‌یافته: {state.allocated_capital:.2f} USDT",
        f"- ارزش کل حساب (equity): {equity:.2f} USDT",
        f"- ارزش موقعیت‌ها: {exposure:.2f}% از حساب — {len(list(positions))} موقعیت باز",
        f"- سود/زیان تحقق‌یافته پس از هزینه: {state.realized_pnl:+.4f} USDT "
        f"(امروز {state.day_realized_pnl:+.4f} / هفته {state.week_realized_pnl:+.4f})",
        f"- سود/زیان تحقق‌نیافته: {unrealized:+.4f} USDT",
        f"- drawdown از قله: {drawdown:.3f}% (سقف {limits.max_drawdown_pct}%)",
        f"- کارمزد پرداختی: {state.fees_paid:.4f} USDT (نرخ {config.maker_fee_pct}% maker / {config.taker_fee_pct}% taker)",
        f"- تعداد سفارش / پرشدن / لغو امروز: {stats['orders']} / {stats['fills']} / {stats['cancels']}",
        f"- تصمیم‌های ثبت‌شده امروز: {stats['decisions']} (تأیید Risk Gate: {stats['approved']})",
        f"- معاملات امروز: {state.trades_today} از سقف {limits.max_trades_per_day}",
        f"- خطاها و هشدارها: {len(cycle.errors) if cycle else 0} خطای چرخه؛ "
        f"خطاهای پیاپی {state.consecutive_errors} از {limits.stop_on_consecutive_errors}",
        f"- وضعیت kill switch: {'فعال — توقف امن' if kill else 'خاموش (در دسترس)'}",
        f"- تغییرات پیکربندی: digest={cycle.config_digest[:12] if cycle else 'n/a'} — بدون تأیید مالک چیزی تغییر نمی‌کند",
        "",
        "## تصمیم‌های ردشده و علت",
    ]
    if stats["rejects"]:
        lines += [f"- {reason}: {count}" for reason, count in stats["rejects"].most_common(10)]
    else:
        lines.append("- امروز هیچ تصمیم ردشده‌ای ثبت نشده است.")

    if extra_warnings:
        lines += ["", "## هشدارهای داده", *[f"- {w}" for w in extra_warnings]]

    verdict = "ادامه‌ی PAPER"
    if kill:
        verdict = "توقف (kill switch)"
    elif state.day_realized_pnl <= -state.allocated_capital * limits.max_daily_loss_pct / 100:
        verdict = "توقف معاملات جدید — سقف زیان روزانه"
    elif drawdown >= limits.max_drawdown_pct:
        verdict = "توقف — سقف drawdown"
    lines += ["", f"- نتیجه‌ی ارزیابی: {verdict}", "", DISCLAIMER, ""]
    return "\n".join(lines)


def write_report(config: Config, text: str) -> Path:
    path = config.root / "state" / "reports" / f"{datetime.now(timezone.utc).date().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def backtest_markdown(result: Any) -> str:
    m = result.metrics
    lines = [
        f"# بک‌تست {result.symbol} ({result.timeframe})",
        "",
        f"- بازه: {result.start} تا {result.end} — {result.bars} کندل",
        f"- equity پایانی: {result.final_equity:.2f} از سرمایه‌ی اولیه — خالص {m['net_pnl']:+.2f} ({m['net_return_pct']:+.2f}%)",
        f"- buy-and-hold در همان بازه: {result.buy_hold_pct:+.2f}%",
        f"- تعداد معاملات: {m['trades']} | win rate: {m['win_rate_pct']}% | profit factor: {m['profit_factor']}",
        f"- میانگین سود: {m['avg_win']} | میانگین زیان: {m['avg_loss']}",
        f"- max drawdown: {m['max_drawdown_pct']}% | کارمزد: {m['fees_paid']}",
        f"- Sharpe خام سالانه: {m['sharpe_annualised_crude']} — {m['sharpe_note']}",
        "",
        "## علل رد شدن سیگنال",
    ]
    if result.rejected:
        lines += [f"- {reason}: {count}" for reason, count in sorted(result.rejected.items(), key=lambda kv: -kv[1])[:12]]
    else:
        lines.append("- موردی ثبت نشد.")
    if result.trades:
        lines += ["", "| ورود | خروج | حجم | قیمت ورود | قیمت خروج | خالص | علت |", "|---|---|---|---|---|---|---|"]
        for trade in result.trades:
            lines.append(
                f"| {datetime.fromtimestamp(trade.entry_time, tz=timezone.utc).strftime('%m-%d %H:%M')} "
                f"| {datetime.fromtimestamp(trade.exit_time, tz=timezone.utc).strftime('%m-%d %H:%M')} "
                f"| {trade.qty:.6f} | {trade.entry:.2f} | {trade.exit:.2f} | {trade.pnl:+.2f} | {trade.reason} |"
            )
    lines += ["", DISCLAIMER, ""]
    return "\n".join(lines)
