"""SQLite persistence for the research loop.

Kept separate from ``state/platform.sqlite3`` on purpose: research runs must not
contend with the PAPER broker's audit tables. Every experiment stores its code
version, data version, hypothesis, parameters, metrics and the acceptance
decision, so a run can be audited and resumed after a restart.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    message TEXT DEFAULT '',
    experiments INTEGER DEFAULT 0,
    champion_id TEXT,
    settings_json TEXT
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    loop_id TEXT,
    seq INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    hypothesis TEXT,
    strategy TEXT,
    timeframe TEXT,
    params_json TEXT,
    source TEXT,
    status TEXT NOT NULL,
    trials INTEGER DEFAULT 0,
    metrics_json TEXT,
    validation_json TEXT,
    test_json TEXT,
    walk_forward_json TEXT,
    acceptance TEXT,
    reason TEXT,
    error TEXT,
    duration_s REAL,
    code_version TEXT,
    data_version_json TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    key TEXT PRIMARY KEY,
    value_json TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS resource_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cpu_pct REAL,
    ram_mb REAL,
    disk_free_gb REAL,
    workers INTEGER
);
CREATE INDEX IF NOT EXISTS idx_experiments_loop ON experiments(loop_id, seq);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _load(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class ResearchStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
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
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # ---- loops ------------------------------------------------------------
    def start_loop(self, settings_json: dict[str, Any]) -> str:
        loop_id = f"loop-{uuid.uuid4().hex[:10]}"
        self._write("INSERT INTO loop_runs(id, started_at, status, settings_json) VALUES(?,?,?,?)",
                    (loop_id, now_iso(), "running", _dump(settings_json)))
        return loop_id

    def update_loop(self, loop_id: str, *, status: str | None = None, message: str | None = None,
                    experiments: int | None = None, champion_id: str | None = None) -> None:
        fields, params = [], []
        if status is not None:
            fields.append("status=?")
            params.append(status)
        if message is not None:
            fields.append("message=?")
            params.append(message)
        if experiments is not None:
            fields.append("experiments=?")
            params.append(int(experiments))
        if champion_id is not None:
            fields.append("champion_id=?")
            params.append(champion_id)
        if status in {"completed", "failed", "stopped"}:
            fields.append("ended_at=?")
            params.append(now_iso())
        if not fields:
            return
        params.append(loop_id)
        self._write(f"UPDATE loop_runs SET {', '.join(fields)} WHERE id=?", tuple(params))

    def latest_loop(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM loop_runs ORDER BY started_at DESC LIMIT 1")
        return rows[0] if rows else None

    def list_loops(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM loop_runs ORDER BY started_at DESC LIMIT ?", (int(limit),))

    # ---- experiments ------------------------------------------------------
    def create_experiment(self, *, loop_id: str, seq: int, hypothesis: str, strategy: str,
                          timeframe: str, params: dict[str, Any], source: str = "manual",
                          code_version: str = "", data_version: dict[str, Any] | None = None) -> str:
        experiment_id = f"exp-{uuid.uuid4().hex[:10]}"
        self._write(
            "INSERT INTO experiments(id, loop_id, seq, created_at, updated_at, hypothesis, strategy, timeframe,"
            " params_json, source, status, code_version, data_version_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (experiment_id, loop_id, int(seq), now_iso(), now_iso(), hypothesis, strategy, timeframe,
             _dump(params), source, "running", code_version, _dump(data_version or {})),
        )
        return experiment_id

    def finish_experiment(self, experiment_id: str, *, status: str, trials: int = 0,
                          metrics: dict[str, Any] | None = None, validation: dict[str, Any] | None = None,
                          test: dict[str, Any] | None = None, walk_forward: dict[str, Any] | None = None,
                          acceptance: str = "", reason: str = "", error: str = "",
                          duration_s: float | None = None, params: dict[str, Any] | None = None) -> None:
        fields = ["updated_at=?", "status=?", "trials=?", "metrics_json=?", "validation_json=?",
                  "test_json=?", "walk_forward_json=?", "acceptance=?", "reason=?", "error=?",
                  "duration_s=?"]
        params_values: list[Any] = [now_iso(), status, int(trials), _dump(metrics or {}),
                                    _dump(validation or {}), _dump(test or {}), _dump(walk_forward or {}),
                                    acceptance, reason, error, duration_s]
        if params is not None:
            fields.append("params_json=?")
            params_values.append(_dump(params))
        params_values.append(experiment_id)
        self._write(f"UPDATE experiments SET {', '.join(fields)} WHERE id=?", tuple(params_values))

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM experiments WHERE id=?", (experiment_id,))
        return self._decode(rows[0]) if rows else None

    def list_experiments(self, *, loop_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM experiments", ()
        if loop_id:
            sql += " WHERE loop_id=?"
            params = (loop_id,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [self._decode(row) for row in self._rows(sql, params + (int(limit),))]

    def mark_interrupted(self, loop_id: str) -> int:
        rows = self._rows("SELECT id FROM experiments WHERE status='running' AND loop_id=?", (loop_id,))
        for row in rows:
            self._write("UPDATE experiments SET status='interrupted', updated_at=? WHERE id=?",
                        (now_iso(), row["id"]))
        return len(rows)

    def completed_keys(self, loop_id: str) -> set[str]:
        rows = self._rows("SELECT hypothesis, strategy, timeframe, params_json FROM experiments"
                          " WHERE loop_id=? AND status='completed'", (loop_id,))
        return {f"{row['hypothesis']}|{row['strategy']}|{row['timeframe']}|{row['params_json']}" for row in rows}

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("params", "metrics", "validation", "test", "walk_forward", "data_version"):
            source = f"{key}_json"
            if source in row:
                row[key] = _load(row.pop(source), {})
        return row

    # ---- checkpoints / champion ------------------------------------------
    def set_checkpoint(self, key: str, value: Any) -> None:
        self._write("INSERT OR REPLACE INTO checkpoints(key, value_json, updated_at) VALUES(?,?,?)",
                    (key, _dump(value), now_iso()))

    def get_checkpoint(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value_json FROM checkpoints WHERE key=?", (key,))
        return _load(rows[0]["value_json"], default) if rows else default

    def get_champion(self) -> dict[str, Any] | None:
        return self.get_checkpoint("champion")

    def set_champion(self, record: dict[str, Any]) -> None:
        self.set_checkpoint("champion", record)

    # ---- resources --------------------------------------------------------
    def sample_resources(self, *, cpu_pct: float, ram_mb: float, disk_free_gb: float,
                         workers: int = 0) -> None:
        self._write("INSERT INTO resource_samples(ts, cpu_pct, ram_mb, disk_free_gb, workers)"
                    " VALUES(?,?,?,?,?)", (now_iso(), float(cpu_pct), float(ram_mb),
                                           float(disk_free_gb), int(workers)))

    def latest_resources(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM resource_samples ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def counts(self) -> dict[str, int]:
        return {table: int(self._rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])  # noqa: S608
                for table in ("loop_runs", "experiments", "checkpoints", "resource_samples")}
