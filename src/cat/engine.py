"""Cycle orchestration: data -> validation -> strategy -> risk gate -> paper execution.

Every branch leaves an audit record. Any unexpected failure stops new orders for
the cycle (fail closed) instead of guessing.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .broker import LiveExecutor, PaperBroker
from .config import Config, config_digest, load_config
from .data import Quote, DataError, GateProvider, MarketSnapshot, MexcCrossCheck, estimate_slippage_pct, fetch_snapshot
from .decision import build_decision, validate_decision
from .http import HttpClient, check_clock_skew
from .portfolio import AccountState, atomic_write, load_state
from .risk import RiskResult, evaluate
from .strategy import Position, Proposal, analyze, propose


@dataclass
class SymbolOutcome:
    symbol: str
    action: str
    approved: bool
    reasons: list[str]
    order_id: str | None = None
    filled_qty: float = 0.0
    fill_price: float | None = None
    decision_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CycleResult:
    started_at: str
    finished_at: str
    mode: str
    kill_switch: bool
    clock: dict[str, Any]
    outcomes: list[SymbolOutcome]
    equity: float
    unrealized: float
    drawdown_pct: float
    errors: list[str]
    safe_state: bool
    config_digest: str

    def summary(self) -> str:
        return (
            f"[{self.started_at}] mode={self.mode} equity={self.equity:.2f} "
            f"unrealized={self.unrealized:+.2f} drawdown={self.drawdown_pct:.2f}% "
            f"approved={sum(1 for o in self.outcomes if o.approved)}/{len(self.outcomes)} errors={len(self.errors)}"
        )


class Engine:
    def __init__(self, config: Config | None = None, *, allocated_capital: float | None = None):
        self.config = config or load_config()
        root = self.config.root
        self.state_path = root / "state" / "account.json"
        self.audit = AuditLog(root / "state" / "audit" / "events.jsonl")
        self.client = HttpClient(proxy=self.config.proxy)
        self.gate = GateProvider(self.client, filters_ttl_seconds=self.config.filters_cache_seconds)
        self.cross = MexcCrossCheck(self.client) if self.config.fallback_exchange == "mexc" else None
        starting = allocated_capital if allocated_capital is not None else (self.config.allocated_capital or 1000.0)
        self.state: AccountState = load_state(self.state_path, starting)
        self.broker = PaperBroker(config=self.config, state=self.state, audit=self.audit)
        self.live = LiveExecutor(self.config, self.audit)
        self.levels: dict[str, tuple[float, float]] = {}
        self.current_prices: dict[str, float] = {}
        self.clock_fn = self.clock_check
        self._reference_quote = Quote(symbol="", bid=1.0, ask=1.0, ts=0.0, source="internal")
        self.mirror_to_store = True
        self.store = None

    # --- guards -----------------------------------------------------------
    def kill_switch_active(self) -> bool:
        return Path(self.config.kill_switch_file).exists()

    def clock_check(self) -> dict[str, Any]:
        try:
            return check_clock_skew(
                self.client, "https://api.gateio.ws/api/v4/spot/time", ("server_time",), self.config.max_clock_skew_seconds
            )
        except DataError as exc:
            return {"acceptable": False, "error": str(exc)}

    def snapshot(self, symbol: str) -> MarketSnapshot:
        return fetch_snapshot(
            self.gate, self.cross, symbol, self.config.timeframe,
            higher_timeframe=self.config.confirm_timeframe, limit=300,
            staleness_seconds=self.config.limits.stop_on_data_staleness_seconds,
        )

    def _safe_snapshot(self, symbol: str) -> tuple[MarketSnapshot | None, str | None]:
        try:
            return self.snapshot(symbol), None
        except Exception as exc:  # one bad pair must not abort the scan
            return None, f"{type(exc).__name__}: {exc}"

    def _fetch_snapshots(self) -> tuple[dict[str, MarketSnapshot], list[tuple[str, str]]]:
        """Scan the allowlist with bounded concurrency, preserving config order.

        A 100-symbol universe cannot be fetched sequentially inside a cycle, and
        unbounded parallelism would trip exchange rate limits. Results are
        collected in the configured order so entry priority stays deterministic.
        """
        symbols = self.config.symbols
        workers = max(1, min(self.config.data_concurrency, len(symbols)))
        snapshots: dict[str, MarketSnapshot] = {}
        failures: list[tuple[str, str]] = []
        if workers == 1:
            results = [(symbol, self._safe_snapshot(symbol)) for symbol in symbols]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cat-data") as pool:
                futures = {symbol: pool.submit(self._safe_snapshot, symbol) for symbol in symbols}
                results = [(symbol, futures[symbol].result()) for symbol in symbols]
        for symbol, (snap, error) in results:
            if snap is None:
                failures.append((symbol, error or "unknown data error"))
            else:
                snapshots[symbol] = snap
        return snapshots, failures

    # --- one cycle --------------------------------------------------------
    def run_cycle(self, *, now: datetime | None = None, dry_run: bool = False, snapshots: dict[str, MarketSnapshot] | None = None) -> CycleResult:
        now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        started = now.isoformat(timespec="seconds")
        outcomes: list[SymbolOutcome] = []
        errors: list[str] = []
        self.state.roll_periods(now)
        self.levels.clear()

        kill = self.kill_switch_active()
        clock = self.clock_fn()

        if kill:
            cancelled = self.broker.cancel_all("kill_switch")
            self.audit.append("cycle_halted_kill_switch", {"file": str(self.config.kill_switch_file), "cancelled": len(cancelled)}, mode=self.config.mode)
            outcomes.extend(
                SymbolOutcome(o.symbol, "CANCEL", False, ["kill switch active"], o.client_order_id) for o in cancelled
            )
            return self._finish(started, now, kill, clock, outcomes, errors, snapshots or {}, safe_state=True, mirror=not dry_run)

        if not clock.get("acceptable"):
            errors.append(f"clock check failed: {clock}")
            self.state.note_error()
            self.audit.append("cycle_halted_clock", {"clock": clock}, mode=self.config.mode)
            return self._finish(started, now, kill, clock, outcomes, errors, snapshots or {}, safe_state=True, mirror=not dry_run)

        self.audit.append(
            "cycle_start",
            {"mode": self.config.mode, "config_digest": config_digest(self.config.raw),
             "symbols": list(self.config.symbols), "config_warnings": list(self.config.warnings)},
            mode=self.config.mode,
        )

        if snapshots is None:
            snapshots, failures = self._fetch_snapshots()
            for symbol, message in failures:
                errors.append(f"{symbol}: {message}")
                self.audit.append("data_error", {"symbol": symbol, "error": message}, mode=self.config.mode)

        for order in self.broker.cancel_unfilled(now):
            outcomes.append(SymbolOutcome(order.symbol, "CANCEL", False, ["unfilled timeout"], order.client_order_id))

        self.current_prices = {symbol: snap.quote.mid for symbol, snap in snapshots.items()}
        if snapshots:
            self._reference_quote = max(snapshots.values(), key=lambda s: s.quote.ts).quote
        settlement_errors = 0
        for symbol, snap in snapshots.items():
            try:
                self.settle(symbol, snap, now)
            except Exception as exc:  # one bad symbol must not corrupt the cycle
                settlement_errors += 1
                errors.append(f"{symbol}: settlement error {type(exc).__name__}: {exc}")
                self.audit.append("settlement_error", {"symbol": symbol, "error": str(exc)}, mode=self.config.mode)

        strategy_errors = 0
        for symbol, position in list(self.state.positions.items()):
            snap = snapshots.get(symbol)
            if snap is None:
                outcomes.append(SymbolOutcome(symbol, "HOLD", False, ["no fresh data for an open position"]))
                continue
            try:
                self._handle_exit(snap, position, now, outcomes, dry_run)
            except Exception as exc:  # fail closed: the position keeps its stop, entries stop
                strategy_errors += 1
                errors.append(f"{symbol}: exit error {type(exc).__name__}: {exc}")
                self.audit.append("strategy_error", {"symbol": symbol, "phase": "exit", "error": str(exc)}, mode=self.config.mode)
                break

        # After an unexpected strategy failure no new order is placed for this cycle.
        if strategy_errors == 0:
            for symbol, snap in snapshots.items():
                if snap.symbol in self.state.positions or self.broker.open_orders_for(snap.symbol):
                    continue
                try:
                    self._handle_entry(snap, now, outcomes, dry_run)
                except Exception as exc:
                    strategy_errors += 1
                    errors.append(f"{symbol}: entry error {type(exc).__name__}: {exc}")
                    self.audit.append("strategy_error", {"symbol": symbol, "phase": "entry", "error": str(exc)}, mode=self.config.mode)
                    break

        # With a wide universe, a single dead pair is quarantined for the cycle;
        # only a systemic fault (no data at all, or an internal strategy/settlement error)
        # consumes the account's consecutive-error budget.
        systemic = strategy_errors > 0 or settlement_errors > 0 or (not snapshots and bool(errors))
        if systemic:
            self.state.note_error()
        elif snapshots:
            self.state.note_success()
        return self._finish(started, now, kill, clock, outcomes, errors, snapshots, safe_state=systemic, mirror=not dry_run)

    def settle(self, symbol: str, snap: MarketSnapshot, now: datetime) -> None:
        """Match resting orders against the current book and book the results."""
        for fill in self.broker.try_fill(snap.quote, now):
            if fill.symbol != symbol:
                continue
            self.book(fill, self.levels.get(fill.client_order_id), at=fill_epoch(fill))

    def book(self, fill, levels: tuple[float, float, float] | None, at: float | None = None) -> None:
        if levels is None:
            stop = target = fill.price
        else:
            stop, target, planned = levels
            # Shift the bracket by the gap between signal price and actual fill.
            shift = fill.price - planned
            stop, target = stop + shift, target + shift
        if fill.side == "BUY":
            self.state.record_fill(
                cost=fill.qty * fill.price, fee=fill.fee, qty=fill.qty, price=fill.price,
                symbol=fill.symbol, stop=stop, target=target, decision_id=fill.decision_id, at=at,
            )
            position = self.state.positions.get(fill.symbol)
            if position:
                position.stop, position.target = stop, target
        else:
            self.state.record_close(fill.symbol, fill.qty, fill.price, fill.fee, reason=fill.reason, decision_id=fill.decision_id, at=at)
        self.audit.append(
            "fill_booked",
            {"symbol": fill.symbol, "side": fill.side, "qty": fill.qty, "price": fill.price,
             "fee": round(fill.fee, 6), "slippage_pct": fill.slippage_pct, "stop": stop, "target": target},
            mode=self.config.mode,
        )

    # --- decision paths ---------------------------------------------------
    def _proposal_kwargs(self) -> dict[str, Any]:
        return dict(
            confidence_threshold=self.config.confidence_threshold,
            require_multiple_signals=self.config.require_multiple_signals,
            no_trade_if_signals_conflict=self.config.no_trade_if_signals_conflict,
            min_reward_risk_ratio=self.config.limits.min_reward_risk_ratio,
            maker_fee_pct=self.config.maker_fee_pct,
            taker_fee_pct=self.config.taker_fee_pct,
            est_slippage_pct=estimate_slippage_pct(self._reference_quote, self.config.limits.max_slippage_pct),
            params=self.config.strategy_params,
        )

    def _risk_for(self, snap: MarketSnapshot, proposal: Proposal, position: Position | None, now: datetime) -> RiskResult:
        return evaluate(
            self.config, self.state, snap, proposal, position, now=now,
            prices=self.current_prices,
            kill_switch_active=self.kill_switch_active(), clock_ok=True,
        )

    def _handle_exit(self, snap: MarketSnapshot, position: Position, now: datetime, outcomes: list[SymbolOutcome], dry_run: bool) -> None:
        if not snap.quality.is_fresh:
            outcomes.append(SymbolOutcome(snap.symbol, "HOLD", False, ["data untrusted; stop left in place"]))
            return
        proposal = propose(snap, position, **self._proposal_kwargs())
        risk = self._risk_for(snap, proposal, position, now)
        record = self._record_decision(snap, proposal, risk, now)
        if not risk.approved:
            outcomes.append(SymbolOutcome(snap.symbol, proposal.action, False, risk.reasons, decision_id=record))
            return
        if dry_run:
            outcomes.append(SymbolOutcome(snap.symbol, "SELL", True, ["dry-run: exit approved"], decision_id=record, details=risk.as_dict()))
            return
        order = self.broker.place(
            decision_id=record, symbol=snap.symbol, side="SELL", qty=risk.qty,
            price=snap.filters.round_price(snap.quote.bid), now=now, reason=proposal.reason,
        )
        self.levels[order.client_order_id] = (risk.stop, risk.target, risk.entry)
        fills = self.broker.try_fill(snap.quote, now)
        booked = [f for f in fills if f.client_order_id == order.client_order_id]
        for fill in booked:
            self.book(fill, self.levels.get(fill.client_order_id), at=fill_epoch(fill))
            self.audit.append("exit_executed", {"symbol": snap.symbol, "reason": proposal.reason, "pnl_after_fees": None}, mode=self.config.mode)
        outcomes.append(
            SymbolOutcome(snap.symbol, "SELL", True, [proposal.reason], order.client_order_id,
                          sum(f.qty for f in booked), booked[0].price if booked else None, record,
                          {"open_orders": len(self.broker.open_orders_for(snap.symbol))})
        )

    def _handle_entry(self, snap: MarketSnapshot, now: datetime, outcomes: list[SymbolOutcome], dry_run: bool) -> None:
        proposal = propose(snap, None, **self._proposal_kwargs())
        risk = self._risk_for(snap, proposal, None, now)
        record_id = self._record_decision(snap, proposal, risk, now)
        if not risk.approved:
            outcomes.append(SymbolOutcome(snap.symbol, proposal.action, False, risk.reasons, decision_id=record_id))
            return
        if dry_run:
            outcomes.append(SymbolOutcome(snap.symbol, "BUY", True, ["dry-run: approved by risk gate"], decision_id=record_id, details=risk.as_dict()))
            return
        order = self.broker.place(
            decision_id=record_id, symbol=snap.symbol, side="BUY", qty=snap.filters.round_qty(risk.qty),
            price=snap.filters.round_price_up(risk.entry), now=now, reason=proposal.reason,
        )
        self.levels[order.client_order_id] = (risk.stop, risk.target, risk.entry)
        fills = self.broker.try_fill(snap.quote, now)
        booked = [f for f in fills if f.client_order_id == order.client_order_id]
        for fill in booked:
            self.book(fill, self.levels.get(fill.client_order_id), at=fill_epoch(fill))
        if not booked:
            order.status = "CANCELLED"
            self.audit.append("order_cancelled", {"client_order_id": order.client_order_id, "reason": "not marketable"}, mode=self.config.mode)
        outcomes.append(
            SymbolOutcome(snap.symbol, "BUY", True, risk.reasons[:2], order.client_order_id,
                          sum(f.qty for f in booked), booked[0].price if booked else None, record_id,
                          {"notional": risk.notional, "net_reward_risk": risk.reward_risk_ratio, "spread_pct": snap.quote.spread_pct})
        )

    def _record_decision(self, snap: MarketSnapshot, proposal: Proposal, risk: RiskResult, now: datetime) -> str:
        decision_id = f"dec-{uuid.uuid4().hex[:12]}"
        payload = build_decision(
            decision_id=decision_id, now=now, snapshot=snap, proposal=proposal, risk=risk,
            analysis=analyze(snap, self.config.strategy_params),
        )
        schema_errors = validate_decision(self.config.root, payload["decision"])
        if schema_errors:
            risk.approved = False
            risk.reasons = [f"decision failed schema: {e}" for e in schema_errors]
            self.audit.append("decision_rejected", {"decision_id": decision_id, "errors": schema_errors}, mode=self.config.mode)
            return decision_id
        self.audit.append("decision_recorded", payload, mode=self.config.mode)
        return decision_id

    def _finish(self, started: str, now: datetime, kill: bool, clock: dict[str, Any], outcomes: list[SymbolOutcome],
                errors: list[str], snapshots: dict[str, MarketSnapshot], *, safe_state: bool,
                mirror: bool = True) -> CycleResult:
        price_map = {symbol: snap.quote.mid for symbol, snap in snapshots.items()}
        for symbol, position in self.state.positions.items():
            price_map.setdefault(symbol, position.avg_entry)
        equity = self.state.equity(price_map)
        self.audit.append(
            "cycle_end",
            {"outcomes": [o.__dict__ for o in outcomes], "errors": errors, "equity": round(equity, 6),
             "realized_pnl": round(self.state.realized_pnl, 6), "fees": round(self.state.fees_paid, 6),
             "trades_today": self.state.trades_today, "consecutive_errors": self.state.consecutive_errors},
            mode=self.config.mode,
        )
        self._persist()
        cycle = CycleResult(
            started_at=started, finished_at=now.isoformat(timespec="seconds"), mode=self.config.mode,
            kill_switch=kill, clock=clock, outcomes=outcomes, equity=equity,
            unrealized=self.state.unrealized(price_map), drawdown_pct=self.state.drawdown_pct(price_map),
            errors=errors, safe_state=safe_state, config_digest=config_digest(self.config.raw),
        )
        if mirror:
            self._mirror(cycle)
        return cycle

    def _mirror(self, cycle: CycleResult) -> None:
        """Best-effort SQLite projection of the ledger for the dashboard.

        The JSON ledger and the hash-chained audit log stay authoritative: a
        broken dashboard database must never stop a paper cycle or a risk check.
        """
        if not self.mirror_to_store:
            return
        try:
            from .mirror import mirror_cycle
            from .store import open_store

            if self.store is None:
                self.store = open_store(self.config.root)
            mirror_cycle(self.store, self, self.config, cycle, source="paper")
        except Exception as exc:  # noqa: BLE001
            print(f"store mirror skipped: {type(exc).__name__}: {exc}")

    def _persist(self) -> None:
        atomic_write(self.state_path, self.state.to_dict())
        self.broker.persist()




def fill_epoch(fill) -> float:
    """Fill timestamps are ISO strings; account math needs epoch seconds."""
    try:
        return datetime.fromisoformat(fill.ts).timestamp()
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).timestamp()
