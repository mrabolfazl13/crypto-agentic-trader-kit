"""Portfolio/account state with atomic persistence and period rollovers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .strategy import Position


def _stamp(at: float | None) -> str:
    moment = datetime.fromtimestamp(at, tz=timezone.utc) if at is not None else datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ClosedTrade:
    symbol: str
    qty: float
    entry_avg: float
    exit_price: float
    realized_pnl: float
    fees: float
    opened_at: str
    closed_at: str
    reason: str
    decision_id: str


@dataclass
class AccountState:
    allocated_capital: float = 1000.0
    cash: float = 1000.0
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    day: str = ""
    week: str = ""
    day_realized_pnl: float = 0.0
    week_realized_pnl: float = 0.0
    trades_today: int = 0
    consecutive_errors: int = 0
    last_loss_at: float | None = None
    peak_equity: float = 1000.0
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    updated_at: str = ""

    @classmethod
    def new(cls, allocated: float) -> "AccountState":
        now = utc_now()
        return cls(
            allocated_capital=allocated,
            cash=allocated,
            day=now.date().isoformat(),
            week=now.strftime("%G-W%V"),
            peak_equity=allocated,
            updated_at=now.isoformat(timespec="seconds"),
        )

    def roll_periods(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        week = now.strftime("%G-W%V")
        changed = False
        if now.date().isoformat() != self.day:
            self.day = now.date().isoformat()
            self.day_realized_pnl = 0.0
            self.trades_today = 0
            changed = True
        if week != self.week:
            self.week = week
            self.week_realized_pnl = 0.0
            changed = True
        return changed

    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash
        for symbol, position in self.positions.items():
            price = prices.get(symbol)
            if price is None:
                # Unknown exposure is treated conservatively at cost basis.
                price = position.avg_entry
            total += position.qty * price
        return total

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for symbol, position in self.positions.items():
            price = prices.get(symbol, position.avg_entry)
            total += position.qty * (price - position.avg_entry)
        return total

    def exposure_pct(self, prices: dict[str, float]) -> float:
        equity = self.equity(prices)
        if equity <= 0:
            return 100.0
        value = sum(prices.get(s, p.avg_entry) * p.qty for s, p in self.positions.items())
        return value / equity * 100.0

    def drawdown_pct(self, prices: dict[str, float]) -> float:
        equity = self.equity(prices)
        self.peak_equity = max(self.peak_equity, equity)
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity * 100.0

    def record_fill(self, cost: float, fee: float, qty: float, price: float, symbol: str, stop: float, target: float, decision_id: str, at: float | None = None) -> None:
        self.cash -= cost + fee
        self.fees_paid += fee
        existing = self.positions.get(symbol)
        if existing:
            total_qty = existing.qty + qty
            blended = (existing.qty * existing.avg_entry + qty * price) / total_qty
            existing.qty = total_qty
            existing.avg_entry = blended
            existing.stop = min(existing.stop, stop)
            existing.target = max(existing.target, target)
        else:
            self.positions[symbol] = Position(
                symbol=symbol, qty=qty, avg_entry=price, stop=stop, target=target,
                decision_id=decision_id, opened_at=_stamp(at),
            )

    def record_close(self, symbol: str, qty: float, price: float, fee: float, reason: str, decision_id: str, at: float | None = None) -> ClosedTrade | None:
        position = self.positions.get(symbol)
        if not position:
            return None
        settle_qty = min(qty, position.qty)
        gross = settle_qty * (price - position.avg_entry)
        net = gross - fee
        self.cash += settle_qty * price - fee
        self.fees_paid += fee
        self.realized_pnl += net
        self.day_realized_pnl += net
        self.week_realized_pnl += net
        self.trades_today += 1
        if net < 0:
            self.last_loss_at = at if at is not None else utc_now().timestamp()
        position.qty -= settle_qty
        trade = ClosedTrade(
            symbol=symbol, qty=settle_qty, entry_avg=position.avg_entry, exit_price=price,
            realized_pnl=round(net, 8), fees=round(fee, 8), opened_at=position.opened_at,
            closed_at=_stamp(at), reason=reason, decision_id=decision_id,
        )
        if position.qty <= 1e-12:
            self.positions.pop(symbol, None)
        self.closed_trades.append(trade)
        return trade

    def note_error(self) -> None:
        self.consecutive_errors += 1

    def note_success(self) -> None:
        self.consecutive_errors = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["positions"] = {symbol: asdict(position) for symbol, position in self.positions.items()}
        payload["closed_trades"] = [asdict(trade) for trade in self.closed_trades[-200:]]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AccountState":
        positions = {
            symbol: Position(**{k: v for k, v in data.items() if k in Position.__dataclass_fields__})
            for symbol, data in (payload.get("positions") or {}).items()
        }
        trades = [ClosedTrade(**data) for data in (payload.get("closed_trades") or [])]
        known = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__ and k not in ("positions", "closed_trades")}
        return cls(positions=positions, closed_trades=trades, **known)


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_state(path: Path, allocated: float) -> AccountState:
    path = Path(path)
    if not path.exists():
        return AccountState.new(allocated)
    try:
        state = AccountState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"account state unreadable at {path}: {exc}") from exc
    if allocated and allocated != state.allocated_capital:
        # The agent may never grow its own budget; keep the recorded budget.
        state.allocated_capital = state.allocated_capital or allocated
    return state
