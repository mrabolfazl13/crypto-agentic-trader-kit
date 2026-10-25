"""Service layer between the PAPER API and the research engine.

Everything heavy is imported inside the function that needs it, so the base
install (FastAPI + pandas, no pyarrow/optuna/lightgbm) can still serve
``/api/v1/quant/status`` and explain exactly what is missing. The autonomous
loop runs in its own process because its dependencies live in the quant
virtualenv; a killed process resumes from the SQLite checkpoint.

PAPER only: nothing here can place an order or read exchange credentials.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

BACKTEST_MODULES = ("numpy", "pandas", "pyarrow")
LOOP_MODULES = BACKTEST_MODULES + ("optuna", "psutil")
ML_MODULES = LOOP_MODULES + ("lightgbm", "sklearn")
ALL_MODULES = LOOP_MODULES + ("lightgbm", "sklearn")

PYTHON_ENV_VAR = "CAT_QUANT_PYTHON"
PID_FILE_NAME = "loop.pid"
LOOP_LOG_NAME = "loop_stdout.log"

MAX_BACKTEST_SYMBOLS = 25
DEFAULT_BACKTEST_SYMBOLS = 5

_BACKTEST_LOCK = threading.Lock()
_PROCESS_LOCK = threading.Lock()
_PROCESS: subprocess.Popen | None = None
_PROBE_CACHE: dict[str, dict[str, Any]] = {}


class QuantUnavailable(RuntimeError):
    """Optional research dependencies are not installed in this interpreter."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("research dependencies missing: " + ", ".join(missing))


class QuantDisabled(RuntimeError):
    pass


class QuantBusy(RuntimeError):
    pass


def dependency_report() -> dict[str, bool]:
    return {name: importlib.util.find_spec(name) is not None for name in ALL_MODULES}


def _require(modules: tuple[str, ...]) -> None:
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        raise QuantUnavailable(missing)


def _settings(root: Path):
    from ..config import load_config
    from .settings import load_quant_settings

    settings = load_quant_settings(load_config(root=Path(root).resolve()))
    if not settings.enabled:
        raise QuantDisabled("quant.enabled is false in config.yaml")
    if settings.proxy is None:
        proxy = os.environ.get("CAT_PROXY")
        if proxy:
            from dataclasses import replace
            settings = replace(settings, proxy=proxy)
    return settings


def _store(settings):
    from .store import ResearchStore

    return ResearchStore(settings.database_path)


# ---------------------------------------------------------------------------
# Discovery / status
# ---------------------------------------------------------------------------

def _manifest_summary(settings) -> dict[str, Any]:
    from .storage import StorageError, coverage_report, current_data_version, load_manifest

    manifest = load_manifest(settings)
    coverage = coverage_report(settings, manifest)
    try:
        version = current_data_version(settings)
    except StorageError:
        # A fresh workspace has no universe yet; report an empty lake, not a 500.
        version = {}
    data_version = version.get("manifest") if isinstance(version, dict) else version
    months_stored = sorted({str(month) for record in (manifest.get("symbols") or {}).values()
                            for month in (record.get("months") or {})})
    return {
        "data_version": data_version,
        "months_expected": list(settings.months),
        "months_stored": months_stored,
        "symbols_stored": coverage["symbols_stored"],
        "symbols_complete": coverage["symbols_complete"],
        "rows": coverage["rows"],
        "bytes": coverage["bytes"],
        "coverage_pct": coverage["coverage_pct"],
        "missing_minutes": coverage["missing_minutes"],
        "gaps": coverage["gaps"],
        "manifest_generated_at": manifest.get("generated_at"),
        "errors": list(manifest.get("errors") or []),
    }


def status(root: Path) -> dict[str, Any]:
    """One payload the panel can poll: data, storage, loop process, counts."""
    root = Path(root).resolve()
    payload: dict[str, Any] = {
        "mode": "PAPER",
        "live_trading_enabled": False,
        "dependencies": dependency_report(),
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        settings = _settings(root)
    except QuantDisabled as exc:
        return {**payload, "enabled": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - config errors must be readable, not a 500
        return {**payload, "enabled": False, "reason": f"{type(exc).__name__}: {exc}"}

    from .storage import storage_report

    payload["enabled"] = True
    payload["research_db"] = str(settings.database_path)
    python = quant_python(root)
    payload["interpreter"] = {"python": python, "probe": _PROBE_CACHE.get(python)}
    payload["database_bytes"] = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    payload["data"] = _manifest_summary(settings)
    payload["storage"] = storage_report(settings)
    payload["gates"] = {
        "min_trades": settings.loop.min_trades,
        "min_profit_factor": settings.loop.min_profit_factor,
        "max_drawdown_pct": settings.loop.max_drawdown_pct,
        "min_positive_fold_pct": settings.loop.min_positive_fold_pct,
        "max_experiments": settings.loop.max_experiments,
        "trials_per_experiment": settings.loop.trials_per_experiment,
        "max_minutes": settings.loop.max_minutes,
    }
    payload["loop"] = loop_status(root, settings)
    store = _store(settings)
    try:
        payload["counts"] = store.counts()
        payload["champion"] = store.get_champion()
    finally:
        store.close()
    return payload


def data(root: Path, limit: int = 200) -> dict[str, Any]:
    root = Path(root).resolve()
    settings = _settings(root)
    from .storage import coverage_report, storage_report

    summary = _manifest_summary(settings)
    symbols = coverage_report(settings)["symbols"]
    symbols = sorted(symbols, key=lambda row: row.get("rows") or 0, reverse=True)[: max(1, int(limit))]
    return {
        "summary": summary,
        "storage": storage_report(settings),
        "symbols": [{
            "symbol": row.get("symbol"),
            "rows": row.get("rows"),
            "months": len(row.get("months") or {}),
            "coverage_pct": row.get("coverage_pct"),
            "bytes": row.get("bytes"),
            "first_iso": row.get("first_iso"),
            "last_iso": row.get("last_iso"),
        } for row in symbols],
        "note": "Binance Vision spot 1m archives; universe pinned by current market cap (see universe.json)",
    }


# ---------------------------------------------------------------------------
# Research database views
# ---------------------------------------------------------------------------

def experiments(root: Path, *, limit: int = 50, loop_id: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    settings = _settings(root)
    store = _store(settings)
    try:
        limit = max(1, min(int(limit), 200))
        items = store.list_experiments(loop_id=loop_id, limit=limit)
        return {"count": len(items), "loop_id": loop_id, "limit": limit,
                "items": [_compact_experiment(item) for item in items]}
    finally:
        store.close()


def loops(root: Path, *, limit: int = 20) -> dict[str, Any]:
    root = Path(root).resolve()
    settings = _settings(root)
    store = _store(settings)
    try:
        items = store.list_loops(limit=max(1, min(int(limit), 100)))
        return {"count": len(items), "items": items}
    finally:
        store.close()


def _compact_experiment(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item.get("metrics") or {}
    validation = item.get("validation") or {}
    walk = item.get("walk_forward") or {}
    return {
        "id": item.get("id"),
        "loop_id": item.get("loop_id"),
        "seq": item.get("seq"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "strategy": item.get("strategy"),
        "timeframe": item.get("timeframe"),
        "status": item.get("status"),
        "acceptance": item.get("acceptance"),
        "reason": item.get("reason"),
        "error": item.get("error"),
        "trials": item.get("trials"),
        "duration_s": item.get("duration_s"),
        "hypothesis": item.get("hypothesis"),
        "params": item.get("params") or {},
        "metrics": {key: metrics.get(key) for key in
                    ("trades", "net_return_pct", "profit_factor", "max_drawdown_pct",
                     "effective_drawdown_pct", "win_rate_pct", "fees", "exposure_pct",
                     "benchmark_return_pct", "excess_return_pct", "rejections") if key in metrics},
        "validation": {key: validation.get(key) for key in
                       ("trades", "net_return_pct", "profit_factor", "max_drawdown_pct",
                        "effective_drawdown_pct", "win_rate_pct") if key in validation},
        "signal_diagnostics": validation.get("signal_diagnostics") or {},
        "walk_forward": walk.get("aggregate") if isinstance(walk, dict) else None,
        "code_version": item.get("code_version"),
        "data_version": item.get("data_version"),
    }


# ---------------------------------------------------------------------------
# Manual backtest (research only)
# ---------------------------------------------------------------------------

def _number(payload: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = payload.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not (low <= number <= high):
        raise ValueError(f"{key} must be between {low} and {high}")
    return number


def _stored_symbols(settings) -> list[str]:
    from .storage import load_manifest, universe_symbols

    manifest = load_manifest(settings)
    records = manifest.get("symbols") if isinstance(manifest.get("symbols"), dict) else {}
    return [symbol for symbol in universe_symbols(settings) if records.get(symbol, {}).get("months")]


def _select_symbols(settings, payload: dict[str, Any]) -> list[str]:
    stored = _stored_symbols(settings)
    requested = payload.get("symbols")
    if requested:
        if not isinstance(requested, (list, tuple)):
            raise ValueError("symbols must be a list")
        symbols = [str(symbol).upper() for symbol in requested]
        unknown = [symbol for symbol in symbols if symbol not in stored]
        if unknown:
            raise ValueError("symbols without stored data: " + ", ".join(unknown[:10]))
        return symbols[:MAX_BACKTEST_SYMBOLS]
    limit = int(_number(payload, "limit", DEFAULT_BACKTEST_SYMBOLS, 1, MAX_BACKTEST_SYMBOLS))
    return stored[:limit]


def manual_backtest(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Bounded, cost-aware backtest on stored parquet. Never touches an exchange."""
    root = Path(root).resolve()
    _require(BACKTEST_MODULES)
    from .engine import run_backtest, simulate_settings, walk_forward
    from .features import STRATEGIES, TIMEFRAME_SECONDS, params_from_dict

    strategy = str(payload.get("strategy") or "ema_trend")
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; choose from {', '.join(STRATEGIES)}")
    timeframe = str(payload.get("timeframe") or "1h")
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    params = params_from_dict(overrides=payload.get("params") or {})
    settings = _settings(root)
    symbols = _select_symbols(settings, payload)
    sim = simulate_settings(
        settings,
        position_pct=_number(payload, "position_pct", settings.backtest.position_pct, 0.5, 25.0),
        max_open_positions=int(_number(payload, "max_open_positions",
                                       settings.backtest.max_open_positions, 1, 20)),
        max_hold_hours=_number(payload, "max_hold_hours", settings.backtest.max_hold_hours, 1.0, 168.0),
        fee_pct=_number(payload, "fee_pct", settings.backtest.fee_pct, 0.0, 1.0),
        slippage_pct=_number(payload, "slippage_pct", settings.backtest.slippage_pct, 0.0, 1.0),
    )
    if not _BACKTEST_LOCK.acquire(blocking=False):
        raise QuantBusy("another backtest is running in this process")
    try:
        started = time.monotonic()
        if payload.get("walk_forward"):
            result = walk_forward(settings, symbols, strategy=strategy, timeframe=timeframe,
                                  params=params, sim=sim,
                                  folds=int(_number(payload, "folds", settings.walk_forward.folds, 2, 8)),
                                  test_days=int(_number(payload, "test_days",
                                                       settings.walk_forward.test_days, 1, 30)))
            result["kind"] = "walk_forward"
        else:
            result = run_backtest(settings, symbols, strategy=strategy, timeframe=timeframe,
                                  params=params, sim=sim)
            result["kind"] = "backtest"
        result["requested_seconds"] = round(time.monotonic() - started, 2)
    finally:
        _BACKTEST_LOCK.release()
    return _trim_result(result)


def _trim_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep API payloads small; the full record is still reproducible via the CLI."""
    if "trades" in result:
        trades = result["trades"]
        result["trades_total"] = len(trades)
        result["trades"] = trades[-100:]
    if "equity_curve" in result and len(result["equity_curve"]) > 500:
        curve = result["equity_curve"]
        step = max(1, len(curve) // 500)
        sampled = curve[::step]
        if sampled[-1] != curve[-1]:
            sampled.append(curve[-1])
        result["equity_curve"] = sampled
        result["equity_curve_sampled"] = step
    return result


# ---------------------------------------------------------------------------
# Autonomous loop process control
# ---------------------------------------------------------------------------

def quant_python(root: Path) -> str:
    """Interpreter that can actually import pyarrow/optuna — never guessed silently."""
    root = Path(root).resolve()
    candidates = [os.environ.get(PYTHON_ENV_VAR)]
    candidates.append(str(Path.home() / ".venvs" / "crypto-quant" / ("Scripts/python.exe" if os.name == "nt"
                                                                    else "bin/python")))
    candidates.append(str(Path(root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    candidates.append(sys.executable)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def interpreter_probe(python: str) -> dict[str, Any]:
    """Ask the target interpreter, once, which optional modules it can import."""
    if python in _PROBE_CACHE:
        return _PROBE_CACHE[python]
    code = ("import importlib.util as u, json, sys; "
            f"print(json.dumps({{m: u.find_spec(m) is not None for m in {list(LOOP_MODULES)!r}}}))")
    try:
        probe = subprocess.run([python, "-c", code], capture_output=True, text=True,
                               timeout=120, check=False)
        modules = json.loads(probe.stdout) if probe.returncode == 0 else {}
        missing = [name for name in LOOP_MODULES if not modules.get(name)]
        record = {"python": python, "ok": not missing, "missing": missing,
                  "error": None if not missing else (probe.stderr or "").strip()[-400:] or "modules missing"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        record = {"python": python, "ok": False, "missing": list(LOOP_MODULES),
                  "error": f"{type(exc).__name__}: {exc}"}
    _PROBE_CACHE[python] = record
    return record


def _pid_path(root: Path) -> Path:
    return Path(root) / "state" / "quant" / PID_FILE_NAME


def _read_pid(root: Path) -> dict[str, Any] | None:
    path = _pid_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10, check=False)
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _loop_log_path(root: Path) -> Path:
    return Path(root) / "state" / "quant" / LOOP_LOG_NAME


def _log_tail(root: Path, lines: int = 12) -> list[str]:
    path = _loop_log_path(root)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return text[-lines:]


def _live_resources(root: Path) -> dict[str, Any] | None:
    try:
        import psutil
    except ModuleNotFoundError:
        return None
    try:
        disk = psutil.disk_usage(str(root))
        memory = psutil.virtual_memory()
        return {
            "cpu_pct": psutil.cpu_percent(interval=0.2),
            "ram_used_gb": round(memory.used / (1024 ** 3), 2),
            "ram_total_gb": round(memory.total / (1024 ** 3), 2),
            "ram_pct": memory.percent,
            "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            "process_ram_mb": round(psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2), 1),
        }
    except Exception:  # noqa: BLE001 - monitoring is best-effort
        return None


def loop_status(root: Path, settings=None) -> dict[str, Any]:
    root = Path(root).resolve()
    info = _read_pid(root)
    alive = bool(info and _pid_alive(int(info.get("pid") or 0)))
    payload: dict[str, Any] = {
        "process": {"alive": alive, **(info or {})},
        "running": alive,
        "log_tail": _log_tail(root),
        "log_path": str(_loop_log_path(root)),
    }
    try:
        settings = settings or _settings(root)
    except Exception as exc:  # noqa: BLE001 - status must still answer
        return {**payload, "error": f"{type(exc).__name__}: {exc}"}
    store = _store(settings)
    try:
        latest = store.latest_loop()
        payload["latest"] = latest
        if latest and latest.get("status") == "running" and not alive:
            payload["latest"]["resumable"] = True
        payload["counts"] = store.counts()
        payload["last_resources"] = store.latest_resources()
        payload["champion"] = store.get_champion()
        payload["recent"] = store.list_experiments(limit=5)
    finally:
        store.close()
    payload["resources"] = _live_resources(Path(root))
    return payload


def start_loop(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Start the loop as a separate process; returns only after it really exists."""
    root = Path(root).resolve()
    with _PROCESS_LOCK:
        global _PROCESS
        info = _read_pid(root)
        if info and _pid_alive(int(info.get("pid") or 0)):
            raise QuantBusy(f"loop already running with pid {info.get('pid')}")

        python = quant_python(root)
        probe = interpreter_probe(python)
        if not probe["ok"]:
            raise QuantUnavailable([f"{python} ({', '.join(probe['missing'])})"])

        search_symbols = int(_number(payload, "search_symbols", 30, 5, 60))
        full_symbols = payload.get("full_symbols")
        command = [python, str(root / "tools" / "quant_loop.py"),
                   "--search-symbols", str(search_symbols)]
        if full_symbols not in (None, "", 0):
            command += ["--full-symbols", str(int(_number(payload, "full_symbols", 100, 5, 100)))]
        if payload.get("max_experiments") is not None:
            command += ["--max-experiments", str(int(_number(payload, "max_experiments", 12, 1, 50)))]
        if payload.get("max_minutes") is not None:
            command += ["--max-minutes", str(_number(payload, "max_minutes", 180, 1, 720))]
        if payload.get("plan_key"):
            command += ["--plan-key", str(payload["plan_key"])]
        if payload.get("resume") is False:
            command.append("--no-resume")

        log_path = _loop_log_path(root)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        offset = log_path.stat().st_size if log_path.exists() else 0
        log_file = log_path.open("a", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        process = subprocess.Popen(command, cwd=str(root), stdout=log_file, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, creationflags=creationflags)
        _PROCESS = process
        record = {"pid": process.pid, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "python": python, "command": command, "log": str(log_path)}
        _pid_path(root).write_text(json.dumps(record, indent=1), encoding="utf-8")

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = _log_tail(root)
                _pid_path(root).unlink(missing_ok=True)
                raise RuntimeError("loop process exited during startup: " + " | ".join(tail[-4:]))
            time.sleep(0.25)
            if _loop_started(root, offset):
                break
        return {"started": True, **record, "log_tail": _log_tail(root, 6)}


def _loop_started(root: Path, offset: int) -> bool:
    try:
        with _loop_log_path(root).open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            text = handle.read()
    except OSError:
        return False
    return "new loop" in text or "resuming loop" in text


def stop_loop(root: Path) -> dict[str, Any]:
    """Terminate the loop process. The checkpoint makes the stop resumable."""
    root = Path(root).resolve()
    with _PROCESS_LOCK:
        info = _read_pid(root)
        pid = int((info or {}).get("pid") or 0)
        if not pid or not _pid_alive(pid):
            _pid_path(root).unlink(missing_ok=True)
            return {"stopped": False, "reason": "no loop process is running"}
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=30, check=False)
            else:
                os.kill(pid, 15)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"stopped": False, "reason": f"{type(exc).__name__}: {exc}"}
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.25)
        alive = _pid_alive(pid)
        if not alive:
            _pid_path(root).unlink(missing_ok=True)
        return {"stopped": not alive, "pid": pid, "still_alive": alive,
                "note": "the next start resumes from the last checkpoint"}
