"""Typed view over the optional ``quant:`` block in config.yaml.

Defaults are explicit so the research engine can run from a fresh checkout. Every
budget is configurable; the total cap is enforced by :mod:`cat.quant.storage`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MONTHS = ("2026-06", "2026-07", "2026-08", "2026-09")
DEFAULT_TIMEFRAMES = ("15m", "1h", "4h")


@dataclass(frozen=True)
class StorageBudget:
    max_total_gb: float = 2.5
    raw_gb: float = 1.2
    features_gb: float = 0.5
    models_gb: float = 0.3
    results_gb: float = 0.3
    reserve_gb: float = 0.2

    def as_gb(self) -> dict[str, float]:
        return {
            "max_total": self.max_total_gb,
            "raw": self.raw_gb,
            "features": self.features_gb,
            "models": self.models_gb,
            "results": self.results_gb,
            "reserve": self.reserve_gb,
        }


@dataclass(frozen=True)
class BacktestSettings:
    initial_capital: float = 10_000.0
    fee_pct: float = 0.10
    slippage_pct: float = 0.05
    position_pct: float = 5.0
    max_open_positions: int = 10
    max_hold_hours: float = 24.0
    min_signal_bars: int = 30
    # Off by default so every stored experiment keeps reproducing. Turning either on
    # is a pre-registered hypothesis, not a tuning knob: the gates decide the outcome.
    min_edge_cost_ratio: float = 0.0
    risk_per_trade_pct: float = 0.0


@dataclass(frozen=True)
class WalkForwardSettings:
    folds: int = 4
    test_days: int = 7
    train_fraction: float = 0.55
    validation_fraction: float = 0.20


@dataclass(frozen=True)
class LoopSettings:
    max_experiments: int = 12
    trials_per_experiment: int = 8
    min_trades: int = 30
    min_profit_factor: float = 1.10
    max_drawdown_pct: float = 10.0
    min_positive_fold_pct: float = 60.0
    # Pre-registered for the next cycle: a candidate must also beat buy & hold on
    # both the validation slice and the walk-forward sum. A risk-adjusted positive
    # return alone is not an edge if simply holding the basket would have paid more.
    require_benchmark_edge: bool = True
    benchmark_margin_pct: float = 0.05
    max_minutes: float = 180.0


@dataclass(frozen=True)
class MLSettings:
    enabled: bool = True
    horizon_hours: int = 4
    top_k: int = 5
    probability_threshold: float = 0.55
    min_train_rows: int = 5_000


@dataclass(frozen=True)
class IngestSettings:
    concurrency: int = 6
    retries: int = 3
    timeout_seconds: int = 90


@dataclass(frozen=True)
class QuantSettings:
    root: Path
    enabled: bool = True
    data_dir: Path = Path("state/quant")
    universe_path: Path = Path("state/quant/universe.json")
    months: tuple[str, ...] = DEFAULT_MONTHS
    signal_timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    storage: StorageBudget = field(default_factory=StorageBudget)
    ingest: IngestSettings = field(default_factory=IngestSettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    walk_forward: WalkForwardSettings = field(default_factory=WalkForwardSettings)
    loop: LoopSettings = field(default_factory=LoopSettings)
    ml: MLSettings = field(default_factory=MLSettings)
    deep_enabled: bool = False
    proxy: str | None = None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw" / "1m"

    @property
    def features_dir(self) -> Path:
        return self.data_dir / "features"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def loops_dir(self) -> Path:
        return self.data_dir / "loops"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "research.sqlite3"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "research.log"


def _number(section: dict[str, Any], key: str, default: float, *, low: float | None = None,
            high: float | None = None, name: str = "") -> float:
    value = section.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name or key} must be a number, got {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name or key} must be finite, got {value!r}")
    if low is not None and number < low:
        raise ValueError(f"{name or key} must be >= {low}, got {number}")
    if high is not None and number > high:
        raise ValueError(f"{name or key} must be <= {high}, got {number}")
    return number


def _integer(section: dict[str, Any], key: str, default: int, *, low: int, high: int, name: str = "") -> int:
    return int(_number(section, key, default, low=low, high=high, name=name))


def _mapping(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def load_quant_settings(config: Any) -> QuantSettings:
    """Build settings from a loaded :class:`cat.config.Config`.

    Unknown keys are ignored on purpose; a typo in a budget cannot silently enlarge
    the cap because every consumed value is parsed and range-checked here.
    """
    raw = _mapping(getattr(config, "quant", None))
    root = Path(getattr(config, "root", ".")).resolve()
    data_dir = (root / str(raw.get("data_dir", "state/quant"))).resolve()
    universe = (root / str(raw.get("universe_path", "state/quant/universe.json"))).resolve()
    months = raw.get("months", list(DEFAULT_MONTHS))
    if not isinstance(months, list) or not months:
        raise ValueError("quant.months must be a non-empty list like ['2026-06']")
    for month in months:
        if not isinstance(month, str) or len(month) != 7 or month[4] != "-":
            raise ValueError(f"quant.months entry {month!r} must look like '2026-06'")
    timeframes = raw.get("signal_timeframes", list(DEFAULT_TIMEFRAMES))
    if not isinstance(timeframes, list) or not timeframes:
        raise ValueError("quant.signal_timeframes must be a non-empty list")
    for timeframe in timeframes:
        if timeframe not in {"5m", "15m", "30m", "1h", "2h", "4h"}:
            raise ValueError(f"unsupported quant.signal_timeframes entry {timeframe!r}")

    storage_raw = _mapping(raw.get("storage"))
    storage = StorageBudget(
        max_total_gb=_number(storage_raw, "max_total_gb", 2.5, low=0.1, high=50, name="quant.storage.max_total_gb"),
        raw_gb=_number(storage_raw, "raw_gb", 1.2, low=0.0, high=50, name="quant.storage.raw_gb"),
        features_gb=_number(storage_raw, "features_gb", 0.5, low=0.0, high=50, name="quant.storage.features_gb"),
        models_gb=_number(storage_raw, "models_gb", 0.3, low=0.0, high=50, name="quant.storage.models_gb"),
        results_gb=_number(storage_raw, "results_gb", 0.3, low=0.0, high=50, name="quant.storage.results_gb"),
        reserve_gb=_number(storage_raw, "reserve_gb", 0.2, low=0.0, high=50, name="quant.storage.reserve_gb"),
    )
    if storage.max_total_gb <= storage.reserve_gb:
        raise ValueError("quant.storage.max_total_gb must be greater than reserve_gb")

    ingest_raw = _mapping(raw.get("ingest"))
    backtest_raw = _mapping(raw.get("backtest"))
    wf_raw = _mapping(raw.get("walk_forward"))
    loop_raw = _mapping(raw.get("loop"))
    ml_raw = _mapping(raw.get("ml"))
    deep_raw = _mapping(raw.get("deep"))

    return QuantSettings(
        root=root,
        enabled=bool(raw.get("enabled", True)),
        data_dir=data_dir,
        universe_path=universe,
        months=tuple(str(month) for month in months),
        signal_timeframes=tuple(str(tf) for tf in timeframes),
        storage=storage,
        ingest=IngestSettings(
            concurrency=_integer(ingest_raw, "concurrency", 6, low=1, high=16, name="quant.ingest.concurrency"),
            retries=_integer(ingest_raw, "retries", 3, low=0, high=10, name="quant.ingest.retries"),
            timeout_seconds=_integer(ingest_raw, "timeout_seconds", 90, low=5, high=600,
                                     name="quant.ingest.timeout_seconds"),
        ),
        backtest=BacktestSettings(
            initial_capital=_number(backtest_raw, "initial_capital", 10_000, low=100, high=10_000_000,
                                    name="quant.backtest.initial_capital"),
            fee_pct=_number(backtest_raw, "fee_pct", 0.10, low=0.0, high=5.0, name="quant.backtest.fee_pct"),
            slippage_pct=_number(backtest_raw, "slippage_pct", 0.05, low=0.0, high=5.0,
                                 name="quant.backtest.slippage_pct"),
            position_pct=_number(backtest_raw, "position_pct", 5.0, low=0.1, high=100.0,
                                 name="quant.backtest.position_pct"),
            max_open_positions=_integer(backtest_raw, "max_open_positions", 10, low=1, high=100,
                                        name="quant.backtest.max_open_positions"),
            max_hold_hours=_number(backtest_raw, "max_hold_hours", 24, low=0.1, high=24 * 30,
                                   name="quant.backtest.max_hold_hours"),
            min_signal_bars=_integer(backtest_raw, "min_signal_bars", 30, low=2, high=5000,
                                     name="quant.backtest.min_signal_bars"),
            min_edge_cost_ratio=_number(backtest_raw, "min_edge_cost_ratio", 0.0, low=0.0, high=50.0,
                                        name="quant.backtest.min_edge_cost_ratio"),
            risk_per_trade_pct=_number(backtest_raw, "risk_per_trade_pct", 0.0, low=0.0, high=5.0,
                                       name="quant.backtest.risk_per_trade_pct"),
        ),
        walk_forward=WalkForwardSettings(
            folds=_integer(wf_raw, "folds", 4, low=2, high=12, name="quant.walk_forward.folds"),
            test_days=_integer(wf_raw, "test_days", 7, low=1, high=90, name="quant.walk_forward.test_days"),
            train_fraction=_number(wf_raw, "train_fraction", 0.55, low=0.2, high=0.9,
                                   name="quant.walk_forward.train_fraction"),
            validation_fraction=_number(wf_raw, "validation_fraction", 0.20, low=0.05, high=0.4,
                                        name="quant.walk_forward.validation_fraction"),
        ),
        loop=LoopSettings(
            max_experiments=_integer(loop_raw, "max_experiments", 12, low=1, high=100,
                                     name="quant.loop.max_experiments"),
            trials_per_experiment=_integer(loop_raw, "trials_per_experiment", 8, low=1, high=100,
                                           name="quant.loop.trials_per_experiment"),
            min_trades=_integer(loop_raw, "min_trades", 30, low=1, high=100_000, name="quant.loop.min_trades"),
            min_profit_factor=_number(loop_raw, "min_profit_factor", 1.10, low=0.0, high=100,
                                      name="quant.loop.min_profit_factor"),
            max_drawdown_pct=_number(loop_raw, "max_drawdown_pct", 10.0, low=0.1, high=100,
                                     name="quant.loop.max_drawdown_pct"),
            min_positive_fold_pct=_number(loop_raw, "min_positive_fold_pct", 60.0, low=0.0, high=100,
                                          name="quant.loop.min_positive_fold_pct"),
            require_benchmark_edge=bool(loop_raw.get("require_benchmark_edge", True)),
            benchmark_margin_pct=_number(loop_raw, "benchmark_margin_pct", 0.05, low=0.0, high=100,
                                         name="quant.loop.benchmark_margin_pct"),
            max_minutes=_number(loop_raw, "max_minutes", 180, low=1, high=24 * 60, name="quant.loop.max_minutes"),
        ),
        ml=MLSettings(
            enabled=bool(ml_raw.get("enabled", True)),
            horizon_hours=_integer(ml_raw, "horizon_hours", 4, low=1, high=24, name="quant.ml.horizon_hours"),
            top_k=_integer(ml_raw, "top_k", 5, low=1, high=100, name="quant.ml.top_k"),
            probability_threshold=_number(ml_raw, "probability_threshold", 0.55, low=0.5, high=0.99,
                                          name="quant.ml.probability_threshold"),
            min_train_rows=_integer(ml_raw, "min_train_rows", 5000, low=100, high=10_000_000,
                                    name="quant.ml.min_train_rows"),
        ),
        deep_enabled=bool(deep_raw.get("enabled", False)),
        proxy=getattr(config, "proxy", None),
    )
