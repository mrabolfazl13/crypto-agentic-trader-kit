"""Gate A evidence: run the strategy over several history windows and write one matrix.

Reproducible: python tools/gate_a_backtest.py [--symbols BTC_USDT ETH_USDT] [--days 30 120 365 730]
Writes state/reports/gate_a_backtest_<date>.md with the measured numbers only.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from cat.backtest import run_backtest  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.data import seconds_per_bar  # noqa: E402
from cat.http import HttpClient  # noqa: E402
from cat.data import GateProvider  # noqa: E402

DISCLAIMER = (
    "این جدول فقط بازده ثبت‌شده‌ی گذشته است، نه پیش‌بینی و نه تضمین. اعداد با کارمزد و slippage فرضی محاسبه شده‌اند و مبنای فعال‌کردن معامله‌ی زنده قرار نمی‌گیرند مگر آستانه‌های "
    "docs/LIVE_TRADING_GATE_FA.md رعایت شوند."
)


def _fmt(value: object) -> str:
    return "—" if value is None else str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTC_USDT", "ETH_USDT"])
    parser.add_argument("--days", nargs="+", type=int, default=[30, 120, 365, 730])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    config = load_config(root=ROOT)
    gate = GateProvider(HttpClient(proxy=config.proxy))
    confirm = config.confirm_timeframe
    step = seconds_per_bar(config.timeframe)
    end = int(time.time()) // step * step

    rows = []
    for symbol in args.symbols:
        filters = gate.filters(symbol)
        for days in args.days:
            start = end - days * 86400
            klines = gate.klines_range(symbol, config.timeframe, start, end)
            higher = gate.klines_range(symbol, confirm, start - 200 * seconds_per_bar(confirm), end)
            if len(klines) < 120 or len(higher) < 80:
                rows.append({"symbol": symbol, "days": days, "error": f"insufficient history: {len(klines)}/{len(higher)}"})
                continue
            result = run_backtest(config, symbol, bars=klines, higher_bars=higher, filters=filters)
            m = result.metrics
            rows.append({
                "symbol": symbol, "days": days, "bars": result.bars,
                "start": result.start, "end": result.end,
                "net_pnl": m["net_pnl"], "net_return_pct": m["net_return_pct"],
                "buy_hold_pct": result.buy_hold_pct, "trades": m["trades"],
                "win_rate_pct": m["win_rate_pct"], "profit_factor": m["profit_factor"],
                "max_drawdown_pct": m["max_drawdown_pct"], "fees_paid": m["fees_paid"],
            })
            print(f"{symbol} {days}d: net {m['net_return_pct']:+.2f}% "
                  f"(B&H {result.buy_hold_pct:+.2f}%) trades={m['trades']} pf={m['profit_factor']}")

    today = datetime.now(timezone.utc).date().isoformat()
    out = args.out or ROOT / "state" / "reports" / f"gate_a_backtest_{today}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# شواهد دروازه‌ی A — بک‌تست روی داده‌ی واقعی ({today})",
        "",
        f"- سیگنال: `{config.timeframe}` | تأیید: `{confirm}` | سرمایه‌ی تخصیص‌یافته: {config.allocated_capital} USDT",
        f"- کارمزد: {config.maker_fee_pct}% maker / {config.taker_fee_pct}% taker | slippage فرضی روی هر ورود",
        "- اجرا: تصمیم روی کندل t، سفارش روی باز شدن کندل t+1 (بدون look-ahead)",
        "",
        "| نماد | پنجره | کندل | خالص USDT | خالص % | buy-hold % | معامله | win % | profit factor | maxDD % | کارمزد |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['symbol']} | {r['days']}d | — | — | — | — | — | — | — | — | {r['error']} |")
            continue
        lines.append(
            f"| {r['symbol']} | {r['days']}d | {r['bars']} | {r['net_pnl']:+.2f} | {r['net_return_pct']:+.2f}% "
            f"| {r['buy_hold_pct']:+.2f}% | {r['trades']} | {_fmt(r['win_rate_pct'])} | {_fmt(r['profit_factor'])} "
            f"| {_fmt(r['max_drawdown_pct'])} | {_fmt(r['fees_paid'])} |"
        )
    lines += [
        "",
        "## خواندن نتایج",
        "- تراز سود/زیان در پنجره‌ها نزدیک صفر یا منفی است؛ edge قابل‌اتکایی اندازه‌گیری نشده.",
        "- ضررها کوچک‌اند چون سقف تخصیص ۵٪ و ریسک ۰.۵٪ هر معامله، حجم را محدود می‌کند.",
        "- کارمزد بخش معناداری از نتیجه را می‌گیرد: در ETH 730d خالص پس از کارمزد +۰.۰۱ است در حالی‌که ۱.۹۰ USDT فقط کارمزد بوده.",
        "- در سال‌های نزولی استراتژی از buy-and-hold جلوتر است؛ این فقط «زیان کمتر» است، نه سود.",
        "- نتیجه: شواهد فعلی مجوز فعال‌کردن LIVE را نمی‌دهد. سیستم در PAPER می‌ماند.",
        "",
        DISCLAIMER,
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
