"""In-process automation for the PAPER research loop.

Two jobs, both OFF by default and both PAPER-only:

  * a paper cycle every ``automation.paper_cycle_hours`` hours;
  * a research review every ``automation.research_review_days`` days — one
    trade-analysis report, then one optimizer loop with at most three
    hypotheses that have not been claimed on that dataset yet.

The attempt timestamp is written to the ``meta`` table *before* a job starts, so
a restart cannot replay work that already ran. Failures are logged and recorded,
never raised: a scheduled job must not be able to take the API process down, and
it deliberately keeps every guard (mode, kill switch, risk limits, allowlist)
authoritative.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config, ConfigError, load_config
from .data import DataError, DataUnavailable
from .engine import Engine
from .mirror import mirror_cycle
from .store import Store

# Shared with the manual /cycles/paper endpoint: a scheduled cycle can never
# overlap a manual one (both build an Engine against the same account state).
PAPER_CYCLE_LOCK = threading.Lock()

CYCLE_LAST = "automation.paper_cycle.last_attempt_at"
CYCLE_STATUS = "automation.paper_cycle.last_status"
CYCLE_SUMMARY = "automation.paper_cycle.last_summary"
REVIEW_LAST = "automation.review.last_attempt_at"
REVIEW_STATUS = "automation.review.last_status"
REVIEW_SUMMARY = "automation.review.last_summary"
REVIEW_REPORT = "automation.review.last_report_id"
REVIEW_LOOP = "automation.review.last_loop_id"
REVIEW_MATURITY = "automation.review.last_maturity"

MATURITY_ACTIVATION = date(2026, 10, 25)
MAX_REVIEW_HYPOTHESES = 3


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


class Automation:
    """Two daemon jobs over the same store; no external scheduler is required."""

    def __init__(
        self,
        root: Path,
        store: Store,
        *,
        cycle_hours: float | None = None,
        review_days: float | None = None,
        tick_seconds: float = 60.0,
        initial_delay_seconds: float = 20.0,
        config_loader: Callable[[], Config] | None = None,
    ):
        self.root = Path(root)
        self.store = store
        # None means "read the interval from config on each check", so editing
        # config.yaml takes effect without a code change.
        self._cycle_hours = None if cycle_hours is None else max(0.0, float(cycle_hours))
        self._review_days = None if review_days is None else max(0.0, float(review_days))
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._config_loader = config_loader or (lambda: load_config(root=self.root))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_result: dict[str, Any] = {}

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        try:
            config = self._config_loader()
            hours, days = self._intervals(config)
        except (ConfigError, OSError) as exc:
            self.store.log("warning", "automation", f"not started: config unreadable ({type(exc).__name__})")
            return False
        if config.mode != "PAPER":
            self.store.log("warning", "automation", f"not started: mode={config.mode}; automation is PAPER-only")
            return False
        if hours <= 0 and days <= 0:
            self.store.log("info", "automation", "idle: both jobs disabled (automation.* = 0)")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cat-automation", daemon=True)
        self._thread.start()
        self.store.log("info", "automation",
                       f"started: paper_cycle_hours={hours} research_review_days={days}")
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def _loop(self) -> None:
        if self.initial_delay_seconds and self._stop.wait(self.initial_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception as exc:  # noqa: BLE001 - a tick must never kill the thread
                try:
                    self.store.log("error", "automation", f"tick failed: {type(exc).__name__}")
                except Exception:  # noqa: BLE001 - logging must not mask the loop
                    pass
            if self._stop.wait(self.tick_seconds):
                break

    # ---- scheduling -------------------------------------------------------
    def _intervals(self, config: Config | None = None) -> tuple[float, float]:
        hours, days = self._cycle_hours, self._review_days
        if hours is not None and days is not None:
            return hours, days
        try:
            cfg = config or self._config_loader()
        except (ConfigError, OSError):
            return (hours or 0.0, days or 0.0)
        return (hours if hours is not None else max(0.0, float(cfg.auto_cycle_hours)),
                days if days is not None else max(0.0, float(cfg.auto_review_days)))

    def _is_due(self, key: str, interval_seconds: float, now: datetime) -> bool:
        last = _parse_ts(self.store.get_meta(key))
        if last is None:
            return True
        return now >= last + timedelta(seconds=interval_seconds)

    def _next_due(self, key: str, interval_seconds: float) -> str | None:
        last = _parse_ts(self.store.get_meta(key))
        if last is None:
            return None
        return _iso(last + timedelta(seconds=interval_seconds))

    def run_due(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        hours, days = self._intervals()
        results: dict[str, Any] = {}
        if hours > 0 and self._is_due(CYCLE_LAST, hours * 3600.0, now):
            # At-most-once: stamp first so a crash mid-run cannot replay it.
            self._stamp(CYCLE_LAST, CYCLE_STATUS, now)
            results["cycle"] = self.run_cycle(now)
        if days > 0 and self._is_due(REVIEW_LAST, days * 86400.0, now):
            self._stamp(REVIEW_LAST, REVIEW_STATUS, now)
            results["review"] = self.run_review(now)
        return results

    def _stamp(self, last_key: str, status_key: str, now: datetime) -> None:
        self.store.set_meta(last_key, _iso(now))
        self.store.set_meta(status_key, "running")

    def _record(self, job: str, status_key: str, summary_key: str, status: str,
                detail: str, now: datetime, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        detail = str(detail)[:500]
        self.store.set_meta(status_key, status)
        self.store.set_meta(summary_key, detail)
        result = {"job": job, "status": status, "at": _iso(now), "detail": detail}
        if extra:
            result.update(extra)
        with self._lock:
            self._last_result[job] = result
        return result

    def status(self) -> dict[str, Any]:
        hours, days = self._intervals()
        with self._lock:
            last = dict(self._last_result)
        return {
            "enabled": bool(self._thread is not None and self._thread.is_alive()),
            "guard": "PAPER-only؛ mode، kill switch، allowlist و محدودیت ریسک همچنان حاکم‌اند",
            "paper_cycle": {
                "interval_hours": hours,
                "last_attempt_at": self.store.get_meta(CYCLE_LAST),
                "last_status": self.store.get_meta(CYCLE_STATUS),
                "last_summary": self.store.get_meta(CYCLE_SUMMARY),
                "next_due_at": self._next_due(CYCLE_LAST, hours * 3600.0) if hours > 0 else None,
            },
            "research_review": {
                "interval_days": days,
                "last_attempt_at": self.store.get_meta(REVIEW_LAST),
                "last_status": self.store.get_meta(REVIEW_STATUS),
                "last_summary": self.store.get_meta(REVIEW_SUMMARY),
                "last_report_id": self.store.get_meta(REVIEW_REPORT),
                "last_loop_id": self.store.get_meta(REVIEW_LOOP),
                "next_due_at": self._next_due(REVIEW_LAST, days * 86400.0) if days > 0 else None,
            },
            "last_result": last,
        }

    # ---- job: paper cycle -------------------------------------------------
    def run_cycle(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        try:
            config = self._config_loader()
            if config.mode != "PAPER":
                self.store.log("warning", "automation",
                               f"scheduled paper cycle skipped: mode={config.mode}")
                return self._record("paper_cycle", CYCLE_STATUS, CYCLE_SUMMARY, "skipped",
                                    f"mode={config.mode}; automation is PAPER-only", now)
            if Path(config.kill_switch_file).exists():
                self.store.log("warning", "automation",
                               "scheduled paper cycle skipped: kill switch active")
                return self._record("paper_cycle", CYCLE_STATUS, CYCLE_SUMMARY, "skipped",
                                    "kill switch active; nothing was run", now)
            with PAPER_CYCLE_LOCK:
                engine = Engine(config)
                result = engine.run_cycle(now=now, dry_run=False)
                outcomes = list(getattr(result, "outcomes", []) or [])
                errors = list(getattr(result, "errors", []) or [])
                kill = bool(getattr(result, "kill_switch", False))
                if errors:
                    # A count alone cannot explain an outage: keep the first few
                    # symbol errors (truncated) so the next cycle is diagnosable.
                    detail = {"error_count": len(errors),
                              "errors": [str(item)[:200] for item in errors[:5]]}
                    self.store.log("error" if (not outcomes and not kill) else "warning",
                                   "automation",
                                   f"scheduled paper cycle symbol errors: {len(errors)}", detail)
                if not outcomes and errors and not kill:
                    self.store.log("error", "automation",
                                   f"scheduled paper cycle unreachable: {errors[:3]}")
                    return self._record("paper_cycle", CYCLE_STATUS, CYCLE_SUMMARY, "failed",
                                        "data unreachable; fail-closed, no new order", now)
                mirror_cycle(self.store, engine, config, result, source="paper")
                summary = str(result.summary())
                self.store.log("info", "automation", f"paper cycle (scheduled): {summary}")
                return self._record("paper_cycle", CYCLE_STATUS, CYCLE_SUMMARY, "ok", summary, now)
        except (DataUnavailable, DataError, ConnectionError, OSError) as exc:
            self.store.log("error", "automation",
                           f"scheduled paper cycle data error: {type(exc).__name__}")
            return self._record("paper_cycle", CYCLE_STATUS, CYCLE_SUMMARY, "failed",
                                f"data error {type(exc).__name__}; fail-closed", now)
        except Exception as exc:  # noqa: BLE001 - a job failure is data, not a crash
            self.store.log("error", "automation",
                           f"scheduled paper cycle failed: {type(exc).__name__}")
            return self._record("paper_cycle", CYCLE_STATUS, CYCLE_SUMMARY, "failed",
                                f"{type(exc).__name__}; fail-closed", now)

    # ---- job: weekly research review --------------------------------------
    def run_review(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        try:
            config = self._config_loader()
            if config.mode != "PAPER":
                self.store.log("warning", "automation",
                               f"scheduled review skipped: mode={config.mode}")
                return self._record("research_review", REVIEW_STATUS, REVIEW_SUMMARY, "skipped",
                                    f"mode={config.mode}; automation is PAPER-only", now)
            from . import trade_analysis

            try:
                payload = trade_analysis.build_report(self.store, limit_runs=10)
            except ValueError as exc:
                self.store.log("info", "automation", f"review skipped: {exc}")
                return self._record("research_review", REVIEW_STATUS, REVIEW_SUMMARY, "skipped",
                                    str(exc), now)
            payload["config_snapshot"] = {
                "market": config.market, "mode": config.mode,
                "fees": {"maker_pct": config.maker_fee_pct, "taker_pct": config.taker_fee_pct},
            }
            path = trade_analysis.save_report(self.root, payload)
            self.store.set_meta(REVIEW_REPORT, payload["report_id"])
            self.store.log("info", "automation",
                           f"review report {payload['report_id']} built from "
                           f"{payload['trade_count']} trades", {"path": str(path)})

            maturity = self._maturity(now)
            self.store.set_meta(REVIEW_MATURITY, json.dumps(maturity, ensure_ascii=False))
            hypotheses = list((payload.get("discovery") or {}).get("hypotheses") or [])
            target = self._review_target(payload)
            loop_id, loop_reason = self._start_review_loop(config, hypotheses, target)
            detail = (f"report={payload['report_id']} trades={payload['trade_count']} "
                      f"hypotheses={len(hypotheses)} loop={loop_id or 'none'} "
                      f"dataset={target[0] if target else 'none'}")
            if loop_reason:
                detail += f" ({loop_reason})"
            self.store.log("info" if loop_id else "warning", "automation",
                           f"research review: {detail}")
            return self._record("research_review", REVIEW_STATUS, REVIEW_SUMMARY, "ok", detail,
                                now, extra={"report_id": payload["report_id"],
                                            "loop_id": loop_id,
                                            "maturity": maturity})
        except Exception as exc:  # noqa: BLE001 - a job failure is data, not a crash
            self.store.log("error", "automation",
                           f"scheduled review failed: {type(exc).__name__}")
            return self._record("research_review", REVIEW_STATUS, REVIEW_SUMMARY, "failed",
                                f"{type(exc).__name__}; see logs", now)

    def _review_target(self, payload: dict[str, Any]) -> tuple[str, str, str] | None:
        """Pick the dataset that carries the most analysed trades.

        A hypothesis discovered from trades should be tested where those trades
        actually happened, not on whichever run happens to be newest. Ties go to
        the most recent run; a report without trade counts falls back to how
        often each dataset appears.
        """
        labels = [item for item in payload.get("run_labels") or [] if item.get("dataset_id")]
        if not labels:
            return None
        scores: dict[str, int] = {}
        for item in labels:
            key = str(item["dataset_id"])
            scores[key] = scores.get(key, 0) + int(item.get("trades") or 0)
        if not any(scores.values()):
            scores = {}
            for item in labels:
                key = str(item["dataset_id"])
                scores[key] = scores.get(key, 0) + 1
        best = max(scores, key=lambda key: scores[key])
        label = next(item for item in labels if str(item["dataset_id"]) == best)
        if not label.get("symbol") or not label.get("timeframe"):
            return None
        return best, str(label["symbol"]), str(label["timeframe"])

    def _claimed_hypotheses(self) -> set[tuple[str, str]]:
        from . import optimizer

        claimed: set[tuple[str, str]] = set()
        directory = optimizer.state_dir(self.root)
        if not directory.exists():
            return claimed
        for path in directory.glob("opt-*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            dataset_id = state.get("dataset_id")
            if not dataset_id:
                continue
            ids = {item.get("hypothesis_id") for item in state.get("experiments") or []}
            ids.update(state.get("queue") or [])
            if state.get("current"):
                ids.add(state["current"])
            for h_id in ids:
                if h_id:
                    claimed.add((str(h_id), str(dataset_id)))
        return claimed

    def _start_review_loop(self, config: Config, hypotheses: list[dict[str, Any]],
                           target: tuple[str, str, str] | None) -> tuple[str | None, str | None]:
        if target is None:
            return None, "no dataset attached to the analysed runs"
        if not hypotheses:
            return None, "not enough trades for a hypothesis"
        dataset_id, symbol, timeframe = target
        claimed = self._claimed_hypotheses()
        fresh = [item for item in hypotheses if (item.get("id"), dataset_id) not in claimed]
        if not fresh:
            return None, "every discovered hypothesis was already claimed on this dataset"
        from . import optimizer

        try:
            state = optimizer.start(
                config, self.store, symbol=symbol, timeframe=timeframe,
                dataset_id=dataset_id, max_experiments=min(MAX_REVIEW_HYPOTHESES, len(fresh)),
                folds=3, warmup=80, spread_pct=0.05, hypotheses=fresh[:MAX_REVIEW_HYPOTHESES],
            )
        except ValueError as exc:
            return None, f"loop not started: {exc}"
        self.store.set_meta(REVIEW_LOOP, state["run_id"])
        self.store.log("info", "automation",
                       f"review loop started {state['run_id']} on {symbol} {timeframe}",
                       {"hypotheses": [item["id"] for item in fresh[:MAX_REVIEW_HYPOTHESES]]})
        return state["run_id"], None

    def _maturity(self, now: datetime) -> dict[str, Any]:
        stats = self.store.trade_ledger_stats(mode="PAPER")
        curve = self.store.equity_curve(mode="PAPER")
        drawdowns = [row[2] for row in curve if isinstance(row[2], (int, float))]
        return {
            "activation_date": MATURITY_ACTIVATION.isoformat(),
            "days_since_activation": max(0, (now.date() - MATURITY_ACTIVATION).days),
            "paper_trades": stats["trades"],
            "paper_profit_factor": stats["profit_factor"],
            "paper_net_pnl": stats["net_pnl"],
            "paper_max_drawdown_pct": max(drawdowns) if drawdowns else None,
            "cycles_recorded": len(curve),
            "first_cycle_at": curve[0][0] if curve else None,
            "note": "شمارش واقعی از ledger و snapshot‌های PAPER؛ به‌تنهایی مجوز live نیست.",
        }
