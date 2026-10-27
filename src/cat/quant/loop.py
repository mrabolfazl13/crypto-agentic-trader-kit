"""Autonomous research loop with checkpoints, champion comparison and honest rejection.

The loop never changes a risk limit, the symbol universe or the capital. It only
varies strategy fields inside the audited bounds, measures each candidate on a
validation slice and on rolling walk-forward folds, and keeps a champion only
when pre-registered gates and a champion comparison both pass. The final test
slice is evaluated once per experiment and is never used to choose a winner.

Every experiment, trial, rejection reason and resource sample lands in
``research.sqlite3``; the loop resumes from its last checkpoint after a restart.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psutil

from .engine import (Window, chronological_splits, data_window, run_backtest, simulate_settings,
                     walk_forward)
from .features import STRATEGIES, QuantParams, params_from_dict
from .optimizer import optimize
from .settings import QuantSettings
from .storage import current_data_version, storage_report, universe_symbols
from .store import ResearchStore, now_iso

LOOP_PLAN: tuple[dict[str, str], ...] = (
    {"key": "ema_trend_1h", "strategy": "ema_trend", "timeframe": "1h",
     "hypothesis": "Trend following on 1h should capture sustained moves after a fee-aware stop/target."},
    {"key": "trend_pullback_1h", "strategy": "trend_pullback", "timeframe": "1h",
     "hypothesis": "Buying controlled RSI pullbacks inside an uptrend should improve entry price and PF."},
    {"key": "ema_trend_4h", "strategy": "ema_trend", "timeframe": "4h",
     "hypothesis": "A slower timeframe cuts fee churn and should raise net return per trade."},
    {"key": "momentum_4h", "strategy": "momentum", "timeframe": "4h",
     "hypothesis": "Medium-term momentum with a 4h cadence should survive realistic costs."},
    # mean_reversion_1h retired (F-015): untestable as specified (D-011 funnel) and the
    # D-015 counter-trend variant failed acceptance on both independent windows.
    {"key": "breakout_4h", "strategy": "breakout", "timeframe": "4h",
     "hypothesis": "Range breakouts with volume confirmation should produce fewer, larger winners."},
    {"key": "trend_pullback_15m", "strategy": "trend_pullback", "timeframe": "15m",
     "hypothesis": "More samples on 15m may give the loop enough trades to measure an edge."},
    {"key": "breakout_1h", "strategy": "breakout", "timeframe": "1h",
     "hypothesis": "1h breakouts trade more often than 4h and may compound small edges."},
    {"key": "ml_lgbm_1h", "kind": "ml", "strategy": "ml_lgbm", "timeframe": "1h",
     "hypothesis": "A gradient-boosted model on past-only features should beat the rule baseline after costs."},
)


def code_version(root: Path) -> str:
    """Git HEAD plus a hash of the quant sources, so a result can be tied to code."""
    parts: list[str] = []
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root),
                              capture_output=True, text=True, timeout=10, check=False)
        if head.returncode == 0:
            parts.append(head.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    digest = hashlib.sha1()
    for path in sorted((root / "src" / "cat" / "quant").glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    parts.append("quant:" + digest.hexdigest()[:12])
    return "+".join(parts)


def analyze_weakness(experiment: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the failure into named, evidence-backed observations the next plan can react to."""
    metrics = experiment.get("validation", {}) or {}
    aggregate = (experiment.get("walk_forward", {}) or {}).get("aggregate", {}) or {}
    rejections = metrics.get("rejections", {}) or {}
    diagnostics = metrics.get("signal_diagnostics", {}) or {}
    findings: list[dict[str, Any]] = []
    trades = int(aggregate.get("trades") or metrics.get("trades") or 0)
    ready_bars = int(diagnostics.get("ready_bars") or 0)
    entry_signals = int(diagnostics.get("entry_signals") or 0)
    if trades == 0:
        if ready_bars and entry_signals == 0:
            findings.append({"finding": "no_entry_signal",
                             "evidence": f"0 entry signals across {ready_bars} ready bars "
                                         f"(funnel {diagnostics.get('funnel')})",
                             "next": "relax the dominant gate named by the funnel or change family"})
        elif entry_signals:
            findings.append({"finding": "signals_rejected_before_fill",
                             "evidence": f"{entry_signals} entry signals but 0 trades; "
                                         f"rejections {rejections}",
                             "next": "inspect execution rejections, not the signal gates"})
        else:
            findings.append({"finding": "no_trades",
                             "evidence": "zero trades and no diagnostic sample",
                             "next": "loosen entry gates or move to a timeframe with more samples"})
    fees = float(metrics.get("fees") or 0.0)
    net = float(metrics.get("net_pnl") or 0.0)
    if fees > 0 and abs(net) < fees:
        findings.append({"finding": "costs_dominate", "evidence": f"fees {fees:.2f} vs net {net:.2f}",
                         "next": "fewer, longer trades: higher timeframe and larger target multiple"})
    factor = aggregate.get("profit_factor_mean")
    if isinstance(factor, (int, float)) and factor < 1.0:
        findings.append({"finding": "losing_exits", "evidence": f"mean fold PF {factor:.2f}",
                         "next": "different entry family or wider stop/longer target"})
    if int(rejections.get("max_open_positions") or 0) > 100:
        findings.append({"finding": "signal_density", "evidence": "many entries skipped for open slots",
                         "next": "stricter filters so the best signals are not diluted"})
    if float(metrics.get("effective_drawdown_pct") or 0) > 10.0:
        findings.append({"finding": "drawdown_above_limit",
                         "evidence": f"effective drawdown {metrics.get('effective_drawdown_pct')}%",
                         "next": "tighter stops or fewer concurrent positions via stricter filters"})
    if not findings:
        findings.append({"finding": "no_single_dominant_weakness",
                         "evidence": f"validation net {metrics.get('net_return_pct')}%, "
                                     f"fold PF {factor}",
                         "next": "keep the hypothesis but refit parameters on a slower cadence"})
    return findings


def acceptance(validation: dict[str, Any], aggregate: dict[str, Any], champion: dict[str, Any] | None,
               settings: QuantSettings) -> dict[str, Any]:
    """Pre-registered gates. A candidate must clear every one, beat buy & hold and beat the champion."""
    loop = settings.loop
    trades = int(aggregate.get("trades") or 0)
    factor = aggregate.get("profit_factor_mean")
    drawdown = max(float(aggregate.get("max_drawdown_pct") or 0.0),
                   float(aggregate.get("max_effective_drawdown_pct") or 0.0),
                   float(validation.get("effective_drawdown_pct") or 0.0))
    positive = float(aggregate.get("positive_fold_pct") or 0.0)
    net = float(validation.get("net_return_pct") or 0.0)
    champion_net = float((champion or {}).get("validation_net_pct") or 0.0) if champion else None
    margin = loop.benchmark_margin_pct

    def edge(strategy_net: float, benchmark: Any) -> tuple[float | None, bool]:
        # Fail closed: without a benchmark number there is no evidence of edge.
        if not isinstance(benchmark, (int, float)):
            return None, False
        excess = round(float(strategy_net) - float(benchmark), 4)
        return excess, excess >= margin

    validation_excess, validation_beats = edge(net, validation.get("benchmark_return_pct"))
    fold_net = float(aggregate.get("net_return_pct_sum") or 0.0)
    fold_excess, folds_beat = edge(fold_net, aggregate.get("benchmark_return_pct_sum"))
    if not loop.require_benchmark_edge:
        validation_beats = folds_beat = True

    gates = {
        "min_trades": {"required": loop.min_trades, "actual": trades, "pass": trades >= loop.min_trades},
        "profit_factor": {"required": loop.min_profit_factor,
                          "actual": round(float(factor), 4) if isinstance(factor, (int, float)) else None,
                          "pass": isinstance(factor, (int, float)) and float(factor) >= loop.min_profit_factor},
        "max_drawdown_pct": {"required": loop.max_drawdown_pct, "actual": round(drawdown, 3),
                             "pass": drawdown <= loop.max_drawdown_pct},
        "positive_folds_pct": {"required": loop.min_positive_fold_pct, "actual": positive,
                               "pass": positive >= loop.min_positive_fold_pct},
        "positive_validation_net": {"required": "> 0", "actual": round(net, 4), "pass": net > 0},
        "beats_validation_benchmark": {
            "required": f">= buy & hold + {margin}pp" if loop.require_benchmark_edge else "disabled",
            "actual": validation_excess, "pass": validation_beats},
        "beats_fold_benchmark": {
            "required": f">= buy & hold + {margin}pp" if loop.require_benchmark_edge else "disabled",
            "actual": fold_excess, "pass": folds_beat},
        "beats_champion": {"required": champion_net, "actual": round(net, 4),
                           "pass": champion is None or net > champion_net + 0.05},
    }
    failed = [name for name, gate in gates.items() if not gate["pass"]]
    if not failed:
        return {"decision": "accepted", "reason": "all pre-registered gates passed, beat buy & hold "
                                                   "and the champion",
                "gates": gates}
    details = "; ".join(f"{name} (need {gates[name]['required']}, got {gates[name]['actual']})"
                        for name in failed)
    return {"decision": "rejected", "reason": f"failed: {details}", "gates": gates}


class ResourceMonitor(threading.Thread):
    def __init__(self, store: ResearchStore, root: Path, interval: float = 10.0):
        super().__init__(daemon=True, name="quant-resource-monitor")
        self.store, self.root = store, Path(root)
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        process = psutil.Process(os.getpid())
        while not self._stop.wait(self.interval):
            try:
                disk = psutil.disk_usage(str(self.root))
                self.store.sample_resources(cpu_pct=psutil.cpu_percent(interval=None),
                                            ram_mb=process.memory_info().rss / (1024 * 1024),
                                            disk_free_gb=disk.free / (1024 ** 3), workers=1)
            except Exception:  # noqa: BLE001 - monitoring must never kill the loop
                break

    def stop(self) -> None:
        self._stop.set()


def resource_snapshot(root: Path) -> dict[str, Any]:
    disk = psutil.disk_usage(str(root))
    vm = psutil.virtual_memory()
    return {
        "cpu_pct": psutil.cpu_percent(interval=0.2),
        "ram_used_gb": round(vm.used / (1024 ** 3), 2),
        "ram_total_gb": round(vm.total / (1024 ** 3), 2),
        "ram_pct": vm.percent,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "process_ram_mb": round(psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2), 1),
    }


class AutonomousResearchLoop:
    """Run plan -> trials -> validation -> walk-forward -> test -> gate -> checkpoint."""

    def __init__(self, settings: QuantSettings, store: ResearchStore,
                 progress: Callable[[dict[str, Any]], None] | None = None,
                 stop_event: threading.Event | None = None,
                 search_symbols: int = 30, full_symbols: int | None = None):
        self.settings = settings
        self.store = store
        self.progress = progress or (lambda event: None)
        self.stop_event = stop_event or threading.Event()
        self.search_symbols = max(3, int(search_symbols))
        self.full_symbols = full_symbols
        self.version = code_version(settings.root)
        self.data_version = current_data_version(settings)
        self.monitor = ResourceMonitor(store, settings.root)
        self.loop_id = ""
        self.plan: list[dict[str, Any]] = []
        self.next_seq = 0
        self.champion: dict[str, Any] | None = None
        self.experiments_done = 0
        self.started = time.monotonic()
        self.ready = threading.Event()
        self._last_message = ""

    def _emit(self, message: str, **extra: Any) -> None:
        self._last_message = message
        self.progress({"phase": "loop", "message": message, "loop_id": self.loop_id,
                       "experiments": self.experiments_done, **extra})

    def _checkpoint(self, *, status: str | None = None, message: str = "") -> None:
        self.store.set_checkpoint(f"loop:{self.loop_id}", {
            "plan": self.plan, "next_seq": self.next_seq, "champion": self.champion,
            "experiments": self.experiments_done, "updated_at": now_iso(),
        })
        if status:
            self.store.update_loop(self.loop_id, status=status, message=message or self._last_message,
                                   experiments=self.experiments_done,
                                   champion_id=(self.champion or {}).get("experiment_id"))
        else:
            self.store.update_loop(self.loop_id, message=self._last_message,
                                   experiments=self.experiments_done)

    def _prepare(self, resume: bool, plan: list[dict[str, Any]] | None) -> None:
        latest = self.store.latest_loop()
        if resume and latest and latest["status"] == "running":
            self.loop_id = latest["id"]
            interrupted = self.store.mark_interrupted(self.loop_id)
            state = self.store.get_checkpoint(f"loop:{self.loop_id}", {}) or {}
            self.plan = list(state.get("plan") or plan or LOOP_PLAN)
            self.next_seq = int(state.get("next_seq") or 0)
            self.champion = state.get("champion") or self.store.get_champion()
            self.experiments_done = int(state.get("experiments") or 0)
            self._emit(f"resuming loop {self.loop_id} at experiment {self.next_seq}"
                       + (f"; {interrupted} interrupted experiment(s) will be rerun" if interrupted else ""))
            self.store.update_loop(self.loop_id, status="running",
                                   message=f"resumed at experiment {self.next_seq}")
        else:
            self.loop_id = self.store.start_loop({"loop": self.settings.loop.__dict__,
                                                  "walk_forward": self.settings.walk_forward.__dict__,
                                                  "code_version": self.version,
                                                  "data_version": self.data_version})
            self.plan = list(plan or LOOP_PLAN)
            self.next_seq = 0
            self.champion = self.store.get_champion()
            self.experiments_done = 0
            self._emit(f"new loop {self.loop_id} with {len(self.plan)} planned experiments")
        self._checkpoint()
        self.ready.set()

    def _evaluate(self, item: dict[str, Any], seq: int, experiment_id: str,
                  symbols_search: list[str], symbols_full: list[str]) -> dict[str, Any] | None:
        strategy = item["strategy"]
        kind = item.get("kind", "rules")
        if kind == "ml" and not self.settings.ml.enabled:
            raise ValueError("ml experiment requested but quant.ml.enabled is false")
        if kind == "rules" and strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r} in plan")
        timeframe = item["timeframe"]
        self._emit(f"experiment {seq + 1}/{len(self.plan)} {item.get('key')}: "
                   f"{'training ' + strategy if kind == 'ml' else 'optimizing ' + strategy} {timeframe}")
        window = data_window(self.settings, symbols_full)
        splits = chronological_splits(window, train=self.settings.walk_forward.train_fraction,
                                      validation=self.settings.walk_forward.validation_fraction)
        sim = simulate_settings(self.settings)
        record: dict[str, Any] = {
            "experiment_id": experiment_id, "seq": seq, "key": item.get("key"),
            "hypothesis": item["hypothesis"], "strategy": strategy, "timeframe": timeframe,
        }
        if kind == "ml":
            from .ml import run_ml_walk_forward

            params = QuantParams()
            trained = run_ml_walk_forward(self.settings, symbols_search, symbols_full,
                                          timeframe=timeframe, train=splits["train"],
                                          validation=splits["validation"], test=splits["test"],
                                          params=params, progress=self.progress)
            trials = 0
            record.update({
                "params": params.as_dict(),
                "optimization": {"kind": "lightgbm defaults", "threshold": trained["selected_threshold"],
                                 "model": trained["model_metrics"],
                                 "elapsed_seconds": trained["elapsed_seconds"]},
                "validation": trained["validation"]["metrics"],
                "benchmark_validation": trained["validation"]["benchmark"],
                "walk_forward": trained["walk_forward"],
                "test": trained["test"]["metrics"],
                "benchmark_test": trained["test"]["benchmark"],
            })
        else:
            search = optimize(self.settings, symbols_search, strategy=strategy, timeframe=timeframe,
                              trials=self.settings.loop.trials_per_experiment, seed=42 + seq,
                              progress=self.progress)
            params = params_from_dict(overrides=search["best"]["params"])
            validation = run_backtest(self.settings, symbols_full, strategy=strategy, timeframe=timeframe,
                                      params=params, sim=sim, start_ts=splits["validation"].start_ts,
                                      end_ts=splits["validation"].end_ts)
            folds = walk_forward(self.settings, symbols_full, strategy=strategy, timeframe=timeframe,
                                 params=params, sim=sim,
                                 window=Window(window.start_ts, splits["test"].start_ts))
            test = run_backtest(self.settings, symbols_full, strategy=strategy, timeframe=timeframe,
                                params=params, sim=sim, start_ts=splits["test"].start_ts,
                                end_ts=splits["test"].end_ts)
            trials = len(search["trials"])
            record.update({
                "params": params.as_dict(),
                "optimization": {key: search[key] for key in
                                 ("fields", "trials", "best", "stop_reason", "window", "elapsed_seconds")},
                "validation": validation["metrics"], "walk_forward": folds,
                "test": test["metrics"], "benchmark_validation": validation["benchmark"],
                "benchmark_test": test["benchmark"],
            })
        aggregate = record["walk_forward"]["aggregate"]
        decision = acceptance(record["validation"], aggregate, self.champion, self.settings)
        record["acceptance"] = decision["decision"]
        record["reason"] = decision["reason"]
        record["gates"] = decision["gates"]
        record["weaknesses"] = analyze_weakness(record)
        self.store.finish_experiment(
            experiment_id, status="completed", trials=trials,
            metrics=record["test"], validation=record["validation"],
            test={"holdout": record["test"], "benchmark": record["benchmark_test"]},
            walk_forward=record["walk_forward"], acceptance=decision["decision"], reason=decision["reason"],
            duration_s=round(time.monotonic() - self.started, 1), params=params.as_dict(),
        )
        if decision["decision"] == "accepted":
            self.champion = {
                "experiment_id": experiment_id, "strategy": strategy, "timeframe": timeframe,
                "params": params.as_dict(), "validation_net_pct": record["validation"].get("net_return_pct"),
                "validation_pf": record["validation"].get("profit_factor"),
                "walk_forward": aggregate, "created_at": now_iso(),
            }
            self.store.set_champion(self.champion)
            self._emit(f"accepted {item.get('key')} as champion: {decision['reason']}")
        else:
            self._emit(f"rejected {item.get('key')}: {decision['reason']}")
        return record

    def run(self, *, resume: bool = True, plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self._prepare(resume=resume, plan=plan)
        symbols_all = universe_symbols(self.settings)
        symbols_search = symbols_all[: self.search_symbols]
        symbols_full = symbols_all[: self.full_symbols] if self.full_symbols else symbols_all
        self.monitor.start()
        status, errors = "completed", 0
        failed = 0
        try:
            for seq, item in enumerate(self.plan):
                if seq < self.next_seq:
                    continue
                if self.stop_event.is_set():
                    status = "stopped"
                    break
                if self.settings.loop.max_minutes and \
                        (time.monotonic() - self.started) / 60.0 >= self.settings.loop.max_minutes:
                    status = "stopped"
                    self._emit("stopping: time budget reached")
                    break
                if self.experiments_done >= self.settings.loop.max_experiments:
                    status = "stopped"
                    self._emit("stopping: max_experiments reached")
                    break
                experiment_id = self.store.create_experiment(
                    loop_id=self.loop_id, seq=seq, hypothesis=item["hypothesis"],
                    strategy=item["strategy"], timeframe=item["timeframe"], params={},
                    source=item.get("source", "plan"), code_version=self.version,
                    data_version=self.data_version,
                )
                try:
                    self._evaluate(item, seq, experiment_id, symbols_search, symbols_full)
                    errors = 0
                except Exception as exc:  # noqa: BLE001 - a worker error must be visible, not fatal
                    errors += 1
                    failed += 1
                    self.store.finish_experiment(
                        experiment_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                        duration_s=round(time.monotonic() - self.started, 1),
                    )
                    self._emit(f"experiment {seq + 1} failed: {type(exc).__name__}: {exc}", error=str(exc))
                    if errors >= 3:
                        status = "failed"
                        self.store.update_loop(self.loop_id, status="failed",
                                               message="three consecutive experiment failures")
                        break
                self.experiments_done += 1
                self.next_seq = seq + 1
                self._checkpoint()
            else:
                if self.stop_event.is_set():
                    status = "stopped"
                elif failed:
                    # A run with failed experiments is never reported as a clean completion.
                    status = "partial" if self.experiments_done > failed else "failed"
        finally:
            self.monitor.stop()
            self._checkpoint(status=status, message=f"{status} after {self.experiments_done} experiments")
            self._emit(f"loop {status}")
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id, "status": (self.store.latest_loop() or {}).get("status"),
            "champion": self.champion, "experiments": self.experiments_done,
            "next_seq": self.next_seq, "plan_size": len(self.plan),
            "data_version": self.data_version, "code_version": self.version,
            "storage": storage_report(self.settings), "resources": resource_snapshot(self.settings.root),
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
        }


_ACTIVE: dict[str, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()


def start_loop_background(settings: QuantSettings, store: ResearchStore, *,
                          resume: bool = True, search_symbols: int = 30,
                          full_symbols: int | None = None) -> dict[str, Any]:
    with _ACTIVE_LOCK:
        if _ACTIVE.get("thread") and _ACTIVE["thread"].is_alive():
            return {"started": False, "reason": "a loop is already running",
                    "loop_id": _ACTIVE.get("loop_id")}
        stop_event = threading.Event()
        loop = AutonomousResearchLoop(settings, store, stop_event=stop_event,
                                      search_symbols=search_symbols, full_symbols=full_symbols)
        thread = threading.Thread(target=loop.run, kwargs={"resume": resume},
                                  name="quant-autonomous-loop", daemon=True)
        _ACTIVE.update({"thread": thread, "stop": stop_event, "loop": loop})
        thread.start()
        loop.ready.wait(timeout=5.0)
        return {"started": True, "loop_id": loop.loop_id}


def stop_loop_background() -> dict[str, Any]:
    with _ACTIVE_LOCK:
        stop = _ACTIVE.get("stop")
        if stop is None:
            return {"stopped": False, "reason": "no loop is running"}
        stop.set()
        return {"stopped": True}


def loop_running() -> bool:
    thread = _ACTIVE.get("thread")
    return bool(thread and thread.is_alive())
