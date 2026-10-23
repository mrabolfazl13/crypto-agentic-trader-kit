"""Execution layer: idempotent paper broker plus a live guard that stays closed.

The paper broker is the only executor wired up in this build. `LiveExecutor`
exists so the safety tests can prove that live orders cannot be produced
without an owner-confirmed config *and* an explicitly injected adapter.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Config
from .data import Quote
from .portfolio import AccountState, atomic_write


class LiveTradingBlocked(RuntimeError):
    pass


@dataclass
class Order:
    client_order_id: str
    decision_id: str
    symbol: str
    side: str
    qty: float
    limit_price: float
    created_at: str
    expires_at: float
    status: str = "NEW"
    filled_qty: float = 0.0
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.limit_price


@dataclass
class Fill:
    client_order_id: str
    decision_id: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    slippage_pct: float
    ts: str
    liquidity: str
    reason: str = ""


@dataclass
class PaperBroker:
    config: Config
    state: AccountState
    audit: AuditLog
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    seen_decision_ids: set[str] = field(default_factory=set)
    state_path: Path | None = None

    def __post_init__(self) -> None:
        self.state_path = self.state_path or (self.config.root / "state" / "paper_broker.json")
        self._restore()

    def _restore(self) -> None:
        path = Path(self.state_path)
        if not path.exists():
            return
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.orders = {oid: Order(**data) for oid, data in (payload.get("orders") or {}).items()}
        self.fills = [Fill(**data) for data in payload.get("fills", [])]
        self.seen_decision_ids = set(payload.get("seen_decision_ids", []))

    def persist(self) -> None:
        atomic_write(
            Path(self.state_path),
            {
                "orders": {oid: asdict(order) for oid, order in self.orders.items()},
                "fills": [asdict(fill) for fill in self.fills][-500:],
                "seen_decision_ids": sorted(self.seen_decision_ids),
            },
        )

    def open_orders_for(self, symbol: str) -> list[Order]:
        return [order for order in self.orders.values() if order.symbol == symbol and order.status == "NEW"]

    def place(self, *, decision_id: str, symbol: str, side: str, qty: float, price: float, now: datetime, reason: str) -> Order:
        if decision_id in self.seen_decision_ids and self.config.idempotency_required:
            raise ValueError(f"decision {decision_id} already executed; refusing duplicate order")
        if self.open_orders_for(symbol):
            raise ValueError(f"{symbol} already has a resting order; symbol lock held")
        order = Order(
            client_order_id=f"paper-{uuid.uuid4().hex[:16]}",
            decision_id=decision_id,
            symbol=symbol,
            side=side,
            qty=qty,
            limit_price=price,
            created_at=now.isoformat(timespec="seconds"),
            expires_at=now.timestamp() + self.config.cancel_unfilled_after_seconds,
            reason=reason,
        )
        self.orders[order.client_order_id] = order
        self.seen_decision_ids.add(decision_id)
        self.audit.append("order_placed", order.__dict__, mode="PAPER")
        return order

    def cancel_unfilled(self, now: datetime) -> list[Order]:
        cancelled: list[Order] = []
        for order in list(self.orders.values()):
            if order.status == "NEW" and now.timestamp() >= order.expires_at:
                order.status = "CANCELLED"
                cancelled.append(order)
                self.audit.append("order_cancelled", {"client_order_id": order.client_order_id, "symbol": order.symbol, "reason": "unfilled_timeout"}, mode="PAPER")
        return cancelled

    def cancel_all(self, reason: str) -> list[Order]:
        cancelled = []
        for order in list(self.orders.values()):
            if order.status == "NEW":
                order.status = "CANCELLED"
                order.reason = reason
                cancelled.append(order)
                self.audit.append("order_cancelled", {"client_order_id": order.client_order_id, "symbol": order.symbol, "reason": reason}, mode="PAPER")
        return cancelled

    def try_fill(self, quote: Quote, now: datetime) -> list[Fill]:
        """Match resting paper orders against the live top of book."""
        executed: list[Fill] = []
        for order in list(self.orders.values()):
            if order.status != "NEW" or order.symbol != quote.symbol:
                continue
            remaining = order.qty - order.filled_qty
            if remaining <= 0:
                order.status = "FILLED"
                continue
            if order.side == "BUY":
                marketable = quote.ask <= order.limit_price
                fill_price = quote.ask
                available = quote.ask_size
                liquidity = "taker"
            else:
                marketable = quote.bid >= order.limit_price
                fill_price = quote.bid
                available = quote.bid_size
                liquidity = "taker"
            if not marketable:
                continue

            # Cap the slice by displayed top-of-book size when known (partial fills).
            if available and available > 0:
                slice_qty = min(remaining, available)
            else:
                slice_qty = remaining
            slice_qty = min(slice_qty, remaining)
            if slice_qty <= 0:
                continue

            fee_pct = self.config.taker_fee_pct if liquidity == "taker" else self.config.maker_fee_pct
            slipped = fill_price * (1 - fee_pct / 200.0) if order.side == "SELL" else fill_price * (1 + fee_pct / 200.0)
            half = (slipped - fill_price) / fill_price * 100.0
            fee = slice_qty * fill_price * (fee_pct / 100.0)
            fill = Fill(
                client_order_id=order.client_order_id,
                decision_id=order.decision_id,
                symbol=order.symbol,
                side=order.side,
                qty=slice_qty,
                price=fill_price,
                fee=fee,
                slippage_pct=round(abs(half), 6),
                ts=now.isoformat(timespec="seconds"),
                liquidity=liquidity,
                reason=order.reason,
            )
            order.filled_qty += slice_qty
            if order.filled_qty >= order.qty - 1e-12:
                order.status = "FILLED"
            else:
                order.status = "NEW"
            self.fills.append(fill)
            executed.append(fill)
            self.audit.append("order_filled", asdict(fill), mode="PAPER")
        return executed

class LiveExecutor:
    """Refuses everything unless the config is genuinely live-approved."""

    def __init__(self, config: Config, audit: AuditLog, adapter: Any = None):
        self.config = config
        self.audit = audit
        self.adapter = adapter

    def place(self, order: Order) -> None:
        if not (self.config.mode == "LIVE" and self.config.live_trading_enabled and self.config.owner_confirmed):
            self.audit.append(
                "live_blocked",
                {"client_order_id": order.client_order_id, "symbol": order.symbol, "mode": self.config.mode},
                mode=self.config.mode,
            )
            raise LiveTradingBlocked(
                "live trading is not approved by an owner confirmation record; order discarded"
            )
        if self.adapter is None:
            raise LiveTradingBlocked("no live exchange adapter is wired into this build")
        raise LiveTradingBlocked("live execution is intentionally unimplemented in this toolkit")
