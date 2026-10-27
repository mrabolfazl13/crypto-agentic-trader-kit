"""Gate-B evidence report for the PAPER loop (read-only).

Reads ``state/platform.sqlite3`` and answers: did 3-hour cycles keep landing,
did each process restart get followed by a cycle, and is the order/fill ledger
consistent?  It never builds an Engine, fetches a candle or sends an order.

Exit codes: 0 = PASS, 2 = INCOMPLETE (time/continuity not yet met),
3 = FAIL (integrity, restart recovery or the PAPER-only guard is violated).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import ConfigError, load_config  # noqa: E402
from cat.paper_evidence import analyze  # noqa: E402

EXIT = {"PASS": 0, "INCOMPLETE": 2, "FAIL": 3}


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(row) for row in connection.execute(sql).fetchall()]


def collect(connection: sqlite3.Connection) -> dict[str, object]:
    meta = {row["key"]: row["value"] for row in _rows(connection, "SELECT key, value FROM meta")}
    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        "snapshots": _rows(connection, "SELECT ts, equity, drawdown_pct, mode FROM portfolio_snapshots"),
        "orders": _rows(connection, "SELECT id, client_order_id, status, mode, created_ts FROM orders"),
        "fills": _rows(connection, "SELECT id, client_order_id, mode, qty, price, fee FROM fills"),
        "trades": _rows(connection, "SELECT id, net, fees, mode FROM trades"),
        "restarts": _rows(
            connection,
            "SELECT ts FROM logs WHERE source='automation' AND message LIKE 'started:%' ORDER BY ts",
        ),
        # What the loop itself recorded per symbol-cycle; absent in very old stores.
        "decisions": _rows(connection, "SELECT ts, symbol, action, rationale_json, mode FROM decisions")
        if "decisions" in tables else [],
        "meta": meta,
    }


def _persian_lines(payload: dict) -> list[str]:
    window = payload["window"]
    restarts = payload["restarts"]
    ledger = payload["ledger"]
    checks = payload["checks"]

    def show(value, unit=""):
        return "—" if value is None else f"{value}{unit}"

    lines = [
        f"دروازه‌ی B (PAPER) — وضعیت: {payload['status']}",
        f"پنجره: {window['first_cycle_at'] or '—'} تا {window['last_cycle_at'] or '—'}"
        f" ({window['span_days']} روز از {payload['min_days']:g})",
        f"چرخه‌ها: {window['cycles_observed']} مشاهده، {window['cycles_expected']} انتظار؛ "
        f"به‌موقع {show(None if window['on_time_fraction'] is None else round(window['on_time_fraction'] * 100, 1), '%')}"
        f" (حد لازم {round(payload['on_time_fraction_required'] * 100, 1)}%)",
        f"بیشترین شکاف: {show(window['max_gap_hours'], ' ساعت')}؛ "
        f"شکاف‌های بیش از حد: {window['downtime_gaps']}",
        f"بازیابی restart: {restarts['recovered']} از {restarts['in_window']} "
        f"(شکست تاریخی {restarts['historical_missed']}، در انتظار {restarts['pending']})",
        f"دفتر: سفارش {ledger['orders']}، پر شدن {ledger['fills']}، معامله‌ی بسته {ledger['trades']}، "
        f"یتم بدون سفارش {ledger['orphan_fills']}، سود خالص {show(ledger['net_pnl'])}",
        f"شمارش معامله (تشخیصی، داوری را تغییر نمی‌دهد): {payload['cadence']['closed_trades']} بسته از "
        f"{payload['cadence']['min_closed_trades']} لازم؛ نرخ ثبت‌شده "
        f"{show(payload['cadence']['observed_trades_per_day'], ' معامله/روز')}، لازم "
        f"{show(payload['cadence']['required_trades_per_day'], ' معامله/روز')}؛ پیش‌بینی تا پایان پنجره "
        f"{show(payload['cadence']['projected_at_milestone'])} ⇒ {payload['cadence']['state']}",
        f"بررسی‌ها: یکپارچگی={'ok' if checks['integrity_ok'] else 'fail'}، "
        f"بازیابی={'ok' if checks['recovery_ok'] else 'fail'}، "
        f"طول={'ok' if checks['long_enough'] else 'کم'}، "
        f"تداوم={'ok' if checks['continuous_enough'] else 'کم'}، "
        f"همین حالا={'فعال' if checks['live_now'] else 'بی‌چرخه'}",
    ]
    funnel = payload.get("decision_funnel") or {}
    if funnel.get("decisions"):
        top = "، ".join(f"{reason} {share}%" for reason, share in list(funnel["by_reason_pct"].items())[:5])
        lines.append(
            f"فانلِ تصمیمِ خودِ حلقه (تشخیصی، داوری را تغییر نمی‌دهد): "
            f"{funnel['decisions']} سطرِ نماد-چرخه در {funnel['cycles']} چرخه روی {funnel['symbols']} نماد ⇒ {top}"
        )
    if payload["reasons"]:
        lines.append("دلایل: " + "؛ ".join(payload["reasons"]))
    if window["days_remaining"]:
        lines.append(f"باقی‌مانده تا 28 روز: {window['days_remaining']} روز "
                     f"(زودترین زمان واجد شرط: {window['milestone_at'] or '—'})")
    if payload.get("safety"):
        safety = payload["safety"]
        lines.append(f"مرز ایمنی: mode={safety['mode']} live_enabled={safety['live_trading_enabled']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Read-only gate-B evidence for the PAPER loop")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=None, help="platform.sqlite3 (default: <root>/state/platform.sqlite3)")
    parser.add_argument("--interval-hours", type=float, default=None,
                        help="expected cycle cadence (default: config automation.paper_cycle_hours)")
    parser.add_argument("--min-days", type=float, default=28.0)
    parser.add_argument("--min-closed-trades", type=int, default=30,
                        help="gate-B trade count used for the cadence projection (verdict ignores it)")
    parser.add_argument("--now", default=None, help="evaluate as of this ISO timestamp (default: real UTC now)")
    parser.add_argument("--out", default=None,
                        help="JSON path (default: <root>/state/reports/gate_b_evidence.json)")
    parser.add_argument("--json", action="store_true", help="print the JSON payload instead of the summary")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    database = Path(args.db) if args.db else root / "state" / "platform.sqlite3"
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)

    safety: dict[str, object] = {"mode": None, "live_trading_enabled": None, "error": None}
    interval = args.interval_hours
    try:
        config = load_config(root=root)
        safety["mode"] = config.mode
        safety["live_trading_enabled"] = bool(config.live_trading_enabled)
        if interval is None:
            interval = float(config.auto_cycle_hours)
    except (ConfigError, OSError) as exc:
        safety["error"] = f"{type(exc).__name__}: {exc}"
    if interval is None or interval <= 0:
        interval = 3.0

    connection = _read_only(database)
    try:
        recorded = collect(connection)
    finally:
        connection.close()

    payload = analyze(
        snapshots=recorded["snapshots"],
        orders=recorded["orders"],
        fills=recorded["fills"],
        trades=recorded["trades"],
        restarts=[row["ts"] for row in recorded["restarts"]],
        decisions=recorded["decisions"],
        interval_hours=interval,
        now=now,
        min_days=args.min_days,
        min_closed_trades=args.min_closed_trades,
    )
    payload["safety"] = safety
    payload["database"] = str(database)
    payload["evaluated_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    payload["automation"] = {
        key: recorded["meta"].get(key)
        for key in (
            "automation.paper_cycle.last_attempt_at",
            "automation.paper_cycle.last_status",
            "automation.paper_cycle.last_summary",
        )
    }
    unsafe = safety["mode"] != "PAPER" or safety["live_trading_enabled"] is not False
    if unsafe:
        payload["status"] = "FAIL"
        payload["reasons"].insert(0, "PAPER-only guard failed (mode or live_trading_enabled)")

    out_path = Path(args.out) if args.out else root / "state" / "reports" / "gate_b_evidence.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in _persian_lines(payload):
            print(line)
        print(f"خروجی JSON: {out_path}")
    return EXIT[payload["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
