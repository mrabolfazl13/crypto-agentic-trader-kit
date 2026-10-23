"""SQLite persistence for the research platform: one file, explicit provenance.

Every row says where its numbers came from (`mode` / `origin` / `source`) so a
simulated backtest can never be mistaken for a paper fill or a live trade.
Stdlib only; safe for concurrent readers and a single writer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    origin TEXT NOT NULL,
    source TEXT NOT NULL,
    path TEXT,
    start_ts INTEGER,
    end_ts INTEGER,
    bars INTEGER NOT NULL,
    quality_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS bars (
    dataset_id TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    base_volume REAL NOT NULL,
    quote_volume REAL NOT NULL,
    closed INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, open_time)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    params_label TEXT,
    params_json TEXT NOT NULL,
    dataset_id TEXT,
    window_start TEXT,
    window_end TEXT,
    split_json TEXT,
    metrics_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS run_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    entry_time INTEGER,
    exit_time INTEGER,
    entry REAL,
    exit REAL,
    qty REAL,
    pnl REAL,
    fees REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS run_rejections (
    run_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (run_id, reason)
);
CREATE TABLE IF NOT EXISTS equity_points (
    run_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL,
    approved INTEGER NOT NULL,
    mode TEXT NOT NULL,
    strategy_label TEXT,
    rationale_json TEXT,
    risk_json TEXT,
    extras_json TEXT,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT,
    decision_id TEXT,
    symbol TEXT,
    side TEXT,
    qty REAL,
    price REAL,
    status TEXT,
    mode TEXT,
    created_ts TEXT,
    updated_ts TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT,
    client_order_id TEXT,
    symbol TEXT,
    side TEXT,
    qty REAL,
    price REAL,
    fee REAL,
    ts INTEGER,
    mode TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    timeframe TEXT,
    strategy_label TEXT,
    entry_ts INTEGER,
    exit_ts INTEGER,
    qty REAL,
    entry REAL,
    exit REAL,
    gross REAL,
    fees REAL,
    net REAL,
    reason TEXT,
    decision_id TEXT,
    stop REAL,
    target REAL,
    context_json TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    ts TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    mode TEXT NOT NULL,
    equity REAL,
    cash REAL,
    unrealized REAL,
    realized REAL,
    day_pnl REAL,
    exposure_pct REAL,
    drawdown_pct REAL,
    trades_today INTEGER,
    consecutive_errors INTEGER,
    fees_paid REAL,
    positions_json TEXT
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    digest TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    params_json TEXT NOT NULL,
    source TEXT,
    parent TEXT,
    created_at TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    decision TEXT,
    train_json TEXT,
    validation_json TEXT,
    test_json TEXT,
    walk_forward_json TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT
);
CREATE TABLE IF NOT EXISTS config_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    digest TEXT,
    change TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_symbol ON runs(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(date, symbol);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_equity_run ON equity_points(run_id, ts);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    # ---- datasets ---------------------------------------------------------
    def save_dataset(self, *, dataset_id: str, symbol: str, timeframe: str, origin: str, source: str,
                     bars: list[Any], quality: dict[str, Any], path: str | None = None, note: str = "") -> str:
        closed = [b for b in bars if getattr(b, "closed", True)]
        with self._lock:
            self._conn.execute("DELETE FROM bars WHERE dataset_id=?", (dataset_id,))
            self._conn.execute(
                "INSERT OR REPLACE INTO datasets(id, symbol, timeframe, origin, source, path, start_ts, end_ts,"
                " bars, quality_json, created_at, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (dataset_id, symbol, timeframe, origin, source, path,
                 closed[0].open_time if closed else None, closed[-1].open_time if closed else None,
                 len(closed), _json(quality), now_iso(), note),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO bars(dataset_id, open_time, open, high, low, close, base_volume, quote_volume, closed)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [(dataset_id, b.open_time, b.open, b.high, b.low, b.close, b.base_volume, b.quote_volume,
                  1 if getattr(b, "closed", True) else 0) for b in closed],
            )
            self._conn.commit()
        return dataset_id

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM datasets ORDER BY created_at DESC")
        for row in rows:
            row["quality"] = json.loads(row.pop("quality_json") or "{}")
        return rows

    def load_bars(self, dataset_id: str) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM bars WHERE dataset_id=? ORDER BY open_time", (dataset_id,))

    def dataset_quality(self, dataset_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT quality_json, bars, origin, source, symbol, timeframe FROM datasets WHERE id=?", (dataset_id,))
        if not rows:
            return None
        out = json.loads(rows[0]["quality_json"] or "{}")
        out.update({"bars": rows[0]["bars"], "origin": rows[0]["origin"], "source": rows[0]["source"],
                    "symbol": rows[0]["symbol"], "timeframe": rows[0]["timeframe"], "dataset_id": dataset_id})
        return out

    # ---- runs (backtests, walk-forward, grid) -----------------------------
    def record_run(self, *, kind: str, symbol: str, timeframe: str, params_label: str, params: dict[str, Any],
                    dataset_id: str | None, window: tuple[str, str] | None, metrics: dict[str, Any],
                    status: str = "completed", error: str = "", split: dict[str, Any] | None = None,
                    trades: list[Any] | None = None, rejections: dict[str, int] | None = None,
                    equity_curve: list[tuple[int, float]] | None = None, duration_ms: int | None = None) -> str:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(id, kind, status, symbol, timeframe, params_label, params_json, dataset_id,"
                " window_start, window_end, split_json, metrics_json, error, created_at, duration_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, kind, status, symbol, timeframe, params_label, _json(params), dataset_id,
                 window[0] if window else None, window[1] if window else None, _json(split or {}),
                 _json(metrics), error, now_iso(), duration_ms),
            )
            for trade in trades or []:
                self._conn.execute(
                    "INSERT INTO run_trades(run_id, symbol, entry_time, exit_time, entry, exit, qty, pnl, fees, reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run_id, getattr(trade, "symbol", symbol), getattr(trade, "entry_time", None),
                     getattr(trade, "exit_time", None), getattr(trade, "entry", None), getattr(trade, "exit", None),
                     getattr(trade, "qty", None), getattr(trade, "pnl", None), getattr(trade, "fees", None),
                     getattr(trade, "reason", "")),
                )
            for reason, count in (rejections or {}).items():
                self._conn.execute("INSERT OR REPLACE INTO run_rejections(run_id, reason, count) VALUES(?,?,?)",
                                   (run_id, str(reason), int(count)))
            for ts, equity in equity_curve or []:
                self._conn.execute("INSERT INTO equity_points(run_id, ts, equity) VALUES(?,?,?)", (run_id, int(ts), float(equity)))
            self._conn.commit()
        return run_id

    def list_runs(self, *, kind: str | None = None, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._rows(sql, tuple(params))
        for row in rows:
            row["params"] = json.loads(row.pop("params_json") or "{}")
            row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
            row["split"] = json.loads(row.pop("split_json") or "{}")
        return rows

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        run = rows[0]
        run["params"] = json.loads(run.pop("params_json") or "{}")
        run["metrics"] = json.loads(run.pop("metrics_json") or "{}")
        run["split"] = json.loads(run.pop("split_json") or "{}")
        run["trades"] = self._rows("SELECT * FROM run_trades WHERE run_id=? ORDER BY exit_time", (run_id,))
        run["rejections"] = self._rows("SELECT reason, count FROM run_rejections WHERE run_id=? ORDER BY count DESC", (run_id,))
        run["equity_curve"] = [[row["ts"], row["equity"]] for row in
                               self._rows("SELECT ts, equity FROM equity_points WHERE run_id=? ORDER BY ts", (run_id,))]
        return run

    # ---- decisions / orders / fills / trades ------------------------------
    def record_decision(self, decision: dict[str, Any], extras: dict[str, Any], *, mode: str,
                        source: str = "paper", decision_id: str | None = None) -> str:
        did = decision_id or decision.get("decision_id") or f"dec-{uuid.uuid4().hex[:12]}"
        ts = decision.get("timestamp") or now_iso()
        self._write(
            "INSERT OR IGNORE INTO decisions(id, ts, date, symbol, action, confidence, approved, mode,"
            " strategy_label, rationale_json, risk_json, extras_json, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, ts, ts[:10], decision.get("symbol", ""), decision.get("action", ""), decision.get("confidence"),
             1 if extras.get("risk_approved") else 0, mode, (extras.get("indicators") or {}).get("strategy_version", ""),
             _json(decision.get("rationale", [])), _json(decision.get("risk", {})), _json(extras), source),
        )
        return did

    def list_decisions(self, *, date: str | None = None, symbol: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, clauses, params = "SELECT * FROM decisions", [], []
        if date:
            clauses.append("date=?")
            params.append(date)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol.replace("/", "_").upper())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self._rows(sql, tuple(params))
        for row in rows:
            row["rationale"] = json.loads(row.pop("rationale_json") or "[]")
            row["risk"] = json.loads(row.pop("risk_json") or "{}")
            row["extras"] = json.loads(row.pop("extras_json") or "{}")
        return rows

    def record_order(self, order: dict[str, Any], *, mode: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO orders(id, client_order_id, decision_id, symbol, side, qty, price, status, mode,"
            " created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order.get("client_order_id") or order.get("id") or f"ord-{uuid.uuid4().hex[:12]}",
             order.get("client_order_id"), order.get("decision_id"), order.get("symbol"), order.get("side"),
             order.get("qty"), order.get("price"), order.get("status"), mode,
             order.get("created_ts") or now_iso(), order.get("updated_ts") or now_iso()),
        )

    def record_fill(self, fill: dict[str, Any], *, mode: str) -> None:
        # Natural-key dedupe: a cycle mirrored twice, or replayed after a restart
        # when the broker's in-memory list starts empty again, must not double-count.
        exists = self._rows(
            "SELECT 1 FROM fills WHERE client_order_id IS ? AND ts IS ? AND qty IS ? AND price IS ? LIMIT 1",
            (fill.get("client_order_id"), fill.get("ts"), fill.get("qty"), fill.get("price")),
        )
        if exists:
            return
        self._write(
            "INSERT INTO fills(decision_id, client_order_id, symbol, side, qty, price, fee, ts, mode) VALUES(?,?,?,?,?,?,?,?,?)",
            (fill.get("decision_id"), fill.get("client_order_id"), fill.get("symbol"), fill.get("side"),
             fill.get("qty"), fill.get("price"), fill.get("fee"), fill.get("ts"), mode),
        )

    def record_trade(self, trade: dict[str, Any], *, mode: str, timeframe: str = "", strategy_label: str = "") -> None:
        if trade.get("decision_id") and self._rows(
            "SELECT 1 FROM trades WHERE decision_id=? AND symbol=? AND exit_ts IS ? LIMIT 1",
            (trade.get("decision_id"), trade.get("symbol"), trade.get("exit_ts") or trade.get("exit_time")),
        ):
            return
        self._write(
            "INSERT INTO trades(symbol, mode, timeframe, strategy_label, entry_ts, exit_ts, qty, entry, exit,"
            " gross, fees, net, reason, decision_id, stop, target, context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade.get("symbol"), mode, timeframe, strategy_label, trade.get("entry_ts") or trade.get("entry_time"),
             trade.get("exit_ts") or trade.get("exit_time"), trade.get("qty"), trade.get("entry") or trade.get("entry_avg"),
             trade.get("exit") or trade.get("exit_price"), trade.get("gross"), trade.get("fees"),
             trade.get("net") if trade.get("net") is not None else trade.get("realized_pnl"), trade.get("reason"),
             trade.get("decision_id"), trade.get("stop"), trade.get("target"), _json(trade.get("context", {}))),
        )

    def list_trades(self, *, mode: str | None = None, symbol: str | None = None, outcome: str | None = None,
                    limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        sql, clauses, params = "SELECT * FROM trades", [], []
        if mode:
            clauses.append("mode=?")
            params.append(mode)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol.replace("/", "_").upper())
        if outcome == "win":
            clauses.append("net > 0")
        elif outcome == "loss":
            clauses.append("net <= 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY exit_ts DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        return self._rows(sql, tuple(params))

    def trade_ledger_stats(self, *, mode: str = "PAPER") -> dict[str, Any]:
        rows = self.list_trades(mode=mode, limit=10000)
        wins = [t for t in rows if (t["net"] or 0) > 0]
        losses = [t for t in rows if (t["net"] or 0) <= 0]
        gross_win = sum(t["net"] or 0 for t in wins)
        gross_loss = abs(sum(t["net"] or 0 for t in losses))
        return {
            "mode": mode,
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(rows) * 100.0, 2) if rows else None,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_win": round(gross_win / len(wins), 4) if wins else None,
            "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
            "net_pnl": round(gross_win - gross_loss, 4),
            "fees": round(sum(t["fees"] or 0 for t in rows), 4),
        }

    def record_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._write(
            "INSERT OR REPLACE INTO portfolio_snapshots(ts, date, mode, equity, cash, unrealized, realized, day_pnl,"
            " exposure_pct, drawdown_pct, trades_today, consecutive_errors, fees_paid, positions_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot.get("ts") or now_iso(), (snapshot.get("ts") or now_iso())[:10], snapshot.get("mode", "PAPER"),
             snapshot.get("equity"), snapshot.get("cash"), snapshot.get("unrealized"), snapshot.get("realized"),
             snapshot.get("day_pnl"), snapshot.get("exposure_pct"), snapshot.get("drawdown_pct"),
             snapshot.get("trades_today"), snapshot.get("consecutive_errors"), snapshot.get("fees_paid"),
             _json(snapshot.get("positions", []))),
        )

    def equity_curve(self, *, mode: str = "PAPER") -> list[list[Any]]:
        rows = self._rows("SELECT ts, equity, drawdown_pct FROM portfolio_snapshots WHERE mode=? ORDER BY ts", (mode,))
        return [[row["ts"], row["equity"], row["drawdown_pct"]] for row in rows]

    # ---- strategy lab -----------------------------------------------------
    def save_strategy_version(self, record: dict[str, Any]) -> str:
        self._write(
            "INSERT OR REPLACE INTO strategy_versions(digest, name, version, params_json, source, parent, created_at,"
            " accepted, decision, train_json, validation_json, test_json, walk_forward_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["digest"], record.get("name", ""), record.get("version", ""), _json(record.get("params", {})),
             record.get("source", "manual"), record.get("parent"), record.get("created_at") or now_iso(),
             1 if record.get("accepted") else 0, record.get("decision", ""), _json(record.get("train", {})),
             _json(record.get("validation", {})), _json(record.get("test", {})), _json(record.get("walk_forward", {}))),
        )
        return str(record["digest"])

    def list_strategy_versions(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM strategy_versions ORDER BY created_at DESC")
        for row in rows:
            for key in ("params", "train", "validation", "test", "walk_forward"):
                row[key] = json.loads(row.pop(f"{key}_json") or "{}")
        return rows

    # ---- logs / config events --------------------------------------------
    def log(self, level: str, source: str, message: str, context: dict[str, Any] | None = None) -> None:
        self._write("INSERT INTO logs(ts, level, source, message, context_json) VALUES(?,?,?,?,?)",
                    (now_iso(), level, source, message, _json(context or {})))

    def list_logs(self, *, level: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM logs"
        params: tuple = ()
        if level:
            sql += " WHERE level=?"
            params = (level,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._rows(sql, params + (int(limit),))
        for row in rows:
            row["context"] = json.loads(row.pop("context_json") or "{}")
        return rows

    def record_config_event(self, *, actor: str, digest: str, change: str, note: str = "") -> None:
        self._write("INSERT INTO config_events(ts, actor, digest, change, note) VALUES(?,?,?,?,?)",
                    (now_iso(), actor, digest, change, note))

    def list_config_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM config_events ORDER BY id DESC LIMIT ?", (int(limit),))

    def counts(self) -> dict[str, int]:
        tables = ("datasets", "bars", "runs", "decisions", "orders", "fills", "trades",
                  "portfolio_snapshots", "strategy_versions", "logs")
        return {table: int(self._rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]) for table in tables}  # noqa: S608


def open_store(root: Path) -> Store:
    return Store(Path(root) / "state" / "platform.sqlite3")
