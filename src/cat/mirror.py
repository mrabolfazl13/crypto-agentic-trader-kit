"""Mirror one engine cycle into the SQLite Store.

Both entry points (the CLI loop and ``POST /cycles/paper``) call this, so the
dashboard is fed by the same records the JSON ledger and audit log hold. The
Store methods are idempotent on their natural keys, which means a cycle mirrored
twice — or replayed after a restart, when the in-memory lists start empty again
— cannot produce duplicate fills or trades.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .store import now_iso


def _epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (ValueError, TypeError):
        return None


def _as_dict(item: Any) -> dict[str, Any]:
    return asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)


def mirror_cycle(store, engine, config, result, *, source: str = "paper") -> None:
    """Persist this cycle's decisions, orders, fills, trades and portfolio snapshot."""
    mode = getattr(result, "mode", config.mode) or "PAPER"
    finished = getattr(result, "finished_at", now_iso())

    for outcome in getattr(result, "outcomes", []) or []:
        decision_id = getattr(outcome, "decision_id", None)
        if not decision_id:
            continue
        decision = {
            "decision_id": decision_id, "timestamp": finished,
            "symbol": getattr(outcome, "symbol", ""), "action": getattr(outcome, "action", ""),
            "confidence": None, "rationale": list(getattr(outcome, "reasons", []) or []),
            "risk": getattr(outcome, "details", {}) or {},
        }
        extras = {
            "risk_approved": bool(getattr(outcome, "approved", False)),
            "order_id": getattr(outcome, "order_id", None),
            "filled_qty": getattr(outcome, "filled_qty", 0.0),
            "fill_price": getattr(outcome, "fill_price", None),
        }
        store.record_decision(decision, extras, mode=mode, source=source, decision_id=decision_id)

    broker = getattr(engine, "broker", None)
    if broker is not None:
        for order in list((getattr(broker, "orders", {}) or {}).values()):
            data = _as_dict(order)
            store.record_order({
                "client_order_id": data.get("client_order_id"), "decision_id": data.get("decision_id"),
                "symbol": data.get("symbol"), "side": data.get("side"), "qty": data.get("qty"),
                "price": data.get("limit_price", data.get("price")), "status": data.get("status"),
                "created_ts": data.get("created_at"), "updated_ts": data.get("created_at"),
            }, mode=mode)
        for fill in list(getattr(broker, "fills", []) or []):
            data = _as_dict(fill)
            store.record_fill({
                "decision_id": data.get("decision_id"), "client_order_id": data.get("client_order_id"),
                "symbol": data.get("symbol"), "side": data.get("side"), "qty": data.get("qty"),
                "price": data.get("price"), "fee": data.get("fee"), "ts": _epoch(data.get("ts")),
            }, mode=mode)

    acct = getattr(engine, "state", None)
    if acct is None:
        return
    for trade in list(getattr(acct, "closed_trades", []) or []):
        data = _as_dict(trade)
        gross = (data.get("exit_price", 0) - data.get("entry_avg", 0)) * data.get("qty", 0)
        store.record_trade({
            "symbol": data.get("symbol"), "entry_ts": _epoch(data.get("opened_at")),
            "exit_ts": _epoch(data.get("closed_at")), "qty": data.get("qty"),
            "entry": data.get("entry_avg"), "exit": data.get("exit_price"), "gross": gross,
            "fees": data.get("fees"), "net": data.get("realized_pnl"), "reason": data.get("reason"),
            "decision_id": data.get("decision_id"),
        }, mode=mode, timeframe=config.timeframe, strategy_label=config.strategy_params.label())

    prices = dict(getattr(engine, "current_prices", {}) or {})
    for symbol, position in getattr(acct, "positions", {}).items():
        prices.setdefault(symbol, position.avg_entry)
    store.record_portfolio_snapshot({
        "ts": finished, "mode": mode,
        "equity": round(acct.equity(prices), 6), "cash": round(acct.cash, 6),
        "unrealized": round(acct.unrealized(prices), 6) if prices else None,
        "realized": round(acct.realized_pnl, 6), "day_pnl": round(acct.day_realized_pnl, 6),
        "exposure_pct": round(acct.exposure_pct(prices), 4),
        "drawdown_pct": round(acct.drawdown_pct(prices), 4),
        "trades_today": acct.trades_today, "consecutive_errors": acct.consecutive_errors,
        "fees_paid": round(acct.fees_paid, 6),
        "positions": [asdict(p) for p in getattr(acct, "positions", {}).values()],
    })
