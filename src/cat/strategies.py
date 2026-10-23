"""Versioned strategy parameters: one place that turns tuning into a named, hashed version.

The base build (`v1`) is the behaviour the safety tests were written against; every
other version is an explicit parameter set that must earn acceptance through
out-of-sample evaluation in the research loop.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any

BASE_VERSION = "v1"


@dataclass(frozen=True)
class StrategyParams:
    name: str = "trend-pullback"
    version: str = BASE_VERSION
    note: str = "base build: only enters confirmed uptrends, ATR bracket, cost-aware reward/risk"

    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    atr_target_mult: float = 3.5
    min_bars: int = 60
    vol_lookback: int = 30
    slope_lookback: int = 30
    volume_avg_bars: int = 20

    regime_gate: str = "TRENDING_UP"
    require_multiple_signals: bool = True
    no_trade_if_signals_conflict: bool = True
    rsi_bullish_low: float = 45.0
    rsi_bullish_high: float = 68.0
    rsi_stretched: float = 78.0
    rsi_weak: float = 40.0
    volume_collapse_ratio: float = 0.6
    volume_weak_ratio: float = 0.9
    slope_threshold: float = 0.06
    vol_threshold: float = 1.0
    atr_pct_threshold: float = 1.5

    def tuning(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k not in {"name", "version", "note"}}

    def digest(self) -> str:
        blob = json.dumps(self.tuning(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def label(self) -> str:
        return f"{self.name}:{self.version}@{self.digest()}"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["digest"] = self.digest()
        out["label"] = self.label()
        return out

    def replace(self, **changes: Any) -> "StrategyParams":
        return replace(self, **changes)


@dataclass
class StrategyVersion:
    """A stored, comparable entry in the strategy lab."""

    params: StrategyParams
    created_at: str
    source: str = "manual"
    metrics: dict[str, Any] = field(default_factory=dict)
    validation_metrics: dict[str, Any] = field(default_factory=dict)
    test_metrics: dict[str, Any] = field(default_factory=dict)
    accepted: bool = False
    decision: str = ""
    parent: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "params": self.params.as_dict(),
            "created_at": self.created_at,
            "source": self.source,
            "metrics": self.metrics,
            "validation_metrics": self.validation_metrics,
            "test_metrics": self.test_metrics,
            "accepted": self.accepted,
            "decision": self.decision,
            "parent": self.parent,
        }


def default_params() -> StrategyParams:
    return StrategyParams()


def params_from_dict(raw: dict[str, Any] | None, *, base: StrategyParams | None = None) -> StrategyParams:
    """Build a variant from a tuning dict, ignoring unknown keys and type mismatches."""
    base = base or default_params()
    if not raw:
        return base
    allowed = {k: v for k, v in asdict(base).items()}
    changes: dict[str, Any] = {}
    for key, value in raw.items():
        # name/version/note are identity fields taken from the top-level keys below;
        # letting them through here would pass `version=` to replace() twice.
        if key not in allowed or key in {"name", "version", "note"}:
            continue
        target = allowed[key]
        try:
            if isinstance(target, bool):
                changes[key] = bool(value)
            elif isinstance(target, int):
                changes[key] = int(value)
            elif isinstance(target, float):
                number = float(value)
                if number != number or number in (float("inf"), float("-inf")):
                    continue
                changes[key] = number
            else:
                changes[key] = str(value)
        except (TypeError, ValueError):
            continue
    version = str(raw.get("version") or base.version)
    name = str(raw.get("name") or base.name)
    return base.replace(version=version, name=name, **changes)
