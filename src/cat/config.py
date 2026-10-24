"""Configuration loading and hard safety validation.

The config is the contract the risk engine enforces. Anything ambiguous is
treated as PAPER and reported, never upgraded silently.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from .http import DataError
from .markets import MarketSpec, get_spec
from .strategies import StrategyParams, params_from_dict

MODES = ("PAPER", "LIVE", "PAUSED")
OWNER_CONFIRMATION_FILE = "owner_confirmation.json"
DEFAULT_MARKET = "crypto"


class ConfigError(RuntimeError):
    pass


def _num(section: dict[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"risk.{key} must be a number, got {value!r}") from exc


@dataclass(frozen=True)
class RiskLimits:
    max_account_allocation_pct: float
    max_position_pct_of_allocated_capital: float
    max_risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_weekly_loss_pct: float
    max_drawdown_pct: float
    max_trades_per_day: int
    max_spread_pct: float
    max_slippage_pct: float
    min_reward_risk_ratio: float
    cooldown_minutes_after_loss: int
    stop_on_data_staleness_seconds: int
    stop_on_consecutive_errors: int


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    root: Path
    mode: str
    live_trading_enabled: bool
    exchange: str
    market_type: str
    symbols: tuple[str, ...]
    timeframe: str
    confirm_timeframe: str
    max_open_positions: int
    allow_short: bool
    allow_margin: bool
    allow_leverage: bool
    allow_withdrawals: bool
    limits: RiskLimits
    confidence_threshold: float
    require_multiple_signals: bool
    require_fee_and_slippage_estimate: bool
    no_trade_if_signals_conflict: bool
    order_type: str
    cancel_unfilled_after_seconds: int
    idempotency_required: bool
    kill_switch_file: Path
    allocated_capital: float
    maker_fee_pct: float
    taker_fee_pct: float
    max_clock_skew_seconds: float
    fallback_exchange: str
    proxy: str | None
    strategy_params: Any = None
    warnings: tuple[str, ...] = ()
    data_concurrency: int = 4
    filters_cache_seconds: int = 3600
    market: str = DEFAULT_MARKET
    market_options: dict[str, Any] = field(default_factory=dict)
    auto_cycle_hours: float = 0.0
    auto_review_days: float = 0.0

    @property
    def spec(self) -> MarketSpec:
        """The venue adapter this config was validated against."""
        return get_spec(self.market)

    @property
    def is_live(self) -> bool:
        return self.mode == "LIVE" and self.owner_confirmed

    @property
    def owner_confirmed(self) -> bool:
        return owner_confirmation_valid(self.root, self)


def owner_confirmation_path(root: Path) -> Path:
    return root / "state" / OWNER_CONFIRMATION_FILE


def owner_confirmation_valid(root: Path, config: "Config | None" = None) -> bool:
    """Live needs a signed-off owner record that still matches this config.

    The record must name the owner, pin the exact config digest, state a hard
    capital cap and be explicitly for live trading. Anything else -> False.
    """
    path = owner_confirmation_path(root)
    if not path.exists():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    if record.get("action") != "ENABLE_LIVE_TRADING":
        return False
    if not record.get("owner"):
        return False
    cap = record.get("allocated_capital")
    if not isinstance(cap, (int, float)) or cap <= 0:
        return False
    if record.get("live_trading_enabled") is not True:
        return False
    if config is not None and record.get("config_digest") != config_digest(config.raw):
        return False
    return True


def config_digest(raw: dict[str, Any]) -> str:
    import hashlib

    blob = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_config(path: str | os.PathLike[str] | None = None, root: Path | None = None) -> Config:
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    cfg_path = Path(path) if path else root / "config.yaml"
    if not cfg_path.exists():
        raise ConfigError(f"config not found: {cfg_path} (copy config.example.yaml to config.yaml)")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    for section in ("app", "trading", "risk", "strategy", "execution", "monitoring", "security"):
        if not isinstance(raw.get(section), dict):
            raise ConfigError(f"config missing required section: {section}")

    trading, risk = raw["trading"], raw["risk"]
    strategy, execution = raw["strategy"], raw["execution"]
    monitoring, security = raw["monitoring"], raw["security"]

    warnings: list[str] = []

    # Venue first: everything below (symbols, timeframes, fees) is validated
    # against the adapter, so a market that cannot be resolved never runs.
    market = str(trading.get("market", DEFAULT_MARKET)).strip().lower()
    try:
        spec = get_spec(market)
    except KeyError:
        warnings.append(f"unknown market {market!r}; forced to {DEFAULT_MARKET!r}")
        market, spec = DEFAULT_MARKET, get_spec(DEFAULT_MARKET)

    markets_section = raw.get("markets") if isinstance(raw.get("markets"), dict) else {}
    if raw.get("markets") is not None and not isinstance(raw.get("markets"), dict):
        warnings.append("markets section is not a mapping; ignored")
    market_options = dict(markets_section.get(market) or {}) if isinstance(markets_section.get(market), dict) else {}

    mode = str(trading.get("mode", "PAPER")).upper()
    if mode not in MODES:
        warnings.append(f"unknown mode {mode!r}; forced to PAPER")
        mode = "PAPER"
    live_enabled = bool(trading.get("live_trading_enabled", False))

    if mode == "LIVE":
        if not live_enabled:
            warnings.append("mode=LIVE but live_trading_enabled=false; forced to PAPER")
            mode = "PAPER"
        pending = Config(
            raw=raw, root=root, mode=mode, live_trading_enabled=live_enabled,
            exchange=str(trading.get("exchange", "")), market_type="spot",
            symbols=(), timeframe=str(trading.get("timeframe", "1h")), confirm_timeframe="1d",
            max_open_positions=0, allow_short=False, allow_margin=False,
            allow_leverage=False, allow_withdrawals=False,
            limits=_risk_limits(risk), confidence_threshold=0.0,
            require_multiple_signals=True, require_fee_and_slippage_estimate=True,
            no_trade_if_signals_conflict=True, order_type="limit",
            cancel_unfilled_after_seconds=0, idempotency_required=True,
            kill_switch_file=root / str(monitoring.get("kill_switch_file", "./STOP_TRADING")),
            allocated_capital=0.0, maker_fee_pct=0.0, taker_fee_pct=0.0,
            max_clock_skew_seconds=2.0, fallback_exchange="", proxy=None,
        )
        if not owner_confirmation_valid(root, pending):
            warnings.append("mode=LIVE without a valid owner confirmation record; forced to PAPER")
            mode = "PAPER"

    if bool(trading.get("allow_margin", False)) or bool(trading.get("allow_leverage", False)):
        warnings.append("margin/leverage requested; base build is spot-only and no-leverage, disabled")
    if bool(trading.get("allow_withdrawals", False)):
        raise ConfigError("allow_withdrawals must stay false in this build")
    if str(trading.get("market_type", "spot")).lower() != "spot":
        raise ConfigError("market_type must be spot in this build")
    if mode == "LIVE" and spec.name != DEFAULT_MARKET:
        warnings.append(f"mode=LIVE on market {spec.name!r}: no live order path exists for this venue; forced to PAPER")
        mode = "PAPER"
    allow_short = bool(trading.get("allow_short", False))
    if allow_short and spec.name != DEFAULT_MARKET:
        warnings.append(f"allow_short on {spec.name}: short selling is not possible on this venue; disabled")
        allow_short = False

    if spec.name == DEFAULT_MARKET:
        symbols = tuple(str(s).upper().replace("/", "_").replace("-", "_") for s in trading.get("symbols", []))
        if not symbols:
            raise ConfigError("trading.symbols is empty")
    else:
        # The venue owns its instrument identity (TSE uses numeric insCodes), so the
        # allowlist the risk gate enforces is resolved through the adapter.
        probe = SimpleNamespace(root=root, market_options=market_options, symbols=())
        try:
            symbols = tuple(spec.allowlist(probe))
        except DataError as exc:
            raise ConfigError(f"market {spec.name}: {exc}") from exc
        if not symbols:
            raise ConfigError(f"market {spec.name}: allowlist resolved to zero instruments")

    fees = raw.get("fees") if isinstance(raw.get("fees"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    automation = raw.get("automation") if isinstance(raw.get("automation"), dict) else {}

    timeframe = str(trading.get("timeframe", "1h"))
    if spec.name != DEFAULT_MARKET and timeframe not in spec.timeframes:
        warnings.append(f"timeframe {timeframe!r} is not published by {spec.name}; forced to {spec.timeframes[0]!r}")
        timeframe = spec.timeframes[0]
    venue_fees = market_options.get("fees") if isinstance(market_options.get("fees"), dict) else fees
    maker_default = 0.10 if spec.default_maker_pct is None else spec.default_maker_pct
    taker_default = 0.10 if spec.default_taker_pct is None else spec.default_taker_pct
    if spec.default_maker_pct is not None and not market_options.get("fees"):
        warnings.append(
            f"{spec.name}: کارمزد از پیش‌فرض venue خوانده شد ({maker_default}% / {taker_default}%); "
            f"نرخ دقیق را با کارگزار تأیید و در markets.{spec.name}.fees بنویسید"
        )

    limits = _risk_limits(risk)
    if security.get("reject_withdraw_permission", True) is not True:
        raise ConfigError("security.reject_withdraw_permission must be true")

    return Config(
        raw=raw,
        root=root,
        mode=mode,
        live_trading_enabled=live_enabled,
        exchange=str(trading.get("exchange", "gate")).lower(),
        market_type="spot",
        symbols=symbols,
        timeframe=timeframe,
        confirm_timeframe=str(data.get("confirm_timeframe") or spec.confirm_timeframe or _default_confirm_tf(timeframe)),
        max_open_positions=int(trading.get("max_open_positions", 2)),
        allow_short=allow_short,
        allow_margin=False,
        allow_leverage=False,
        allow_withdrawals=False,
        limits=limits,
        confidence_threshold=float(strategy.get("confidence_threshold", 0.70)),
        require_multiple_signals=bool(strategy.get("require_multiple_signals", True)),
        require_fee_and_slippage_estimate=bool(strategy.get("require_fee_and_slippage_estimate", True)),
        no_trade_if_signals_conflict=bool(strategy.get("no_trade_if_signals_conflict", True)),
        order_type=str(execution.get("order_type", "limit")).lower(),
        cancel_unfilled_after_seconds=int(execution.get("cancel_unfilled_after_seconds", 30)),
        idempotency_required=bool(execution.get("idempotency_required", True)),
        kill_switch_file=(root / str(monitoring.get("kill_switch_file", "./STOP_TRADING"))).resolve(),
        allocated_capital=float(risk.get("allocated_capital", 0) or _default_allocated(root)),
        maker_fee_pct=_num(venue_fees, "maker_pct", maker_default),
        taker_fee_pct=_num(venue_fees, "taker_pct", taker_default),
        max_clock_skew_seconds=float(risk.get("max_clock_skew_seconds", 2.0)),
        fallback_exchange=str(raw.get("data", {}).get("fallback_exchange", "mexc")) if isinstance(raw.get("data"), dict) else "mexc",
        proxy=os.environ.get("CAT_PROXY") or data.get("proxy"),
        data_concurrency=max(1, min(8, int(data.get("max_concurrency", 4) or 4))),
        filters_cache_seconds=max(0, int(data.get("filters_cache_seconds", 3600) or 0)),
        market=market,
        market_options=market_options,
        auto_cycle_hours=_interval(automation, "paper_cycle_hours"),
        auto_review_days=_interval(automation, "research_review_days"),
        strategy_params=params_from_dict(
            strategy.get("params") if isinstance(strategy.get("params"), dict) else None,
            base=StrategyParams(
                version=str(strategy.get("version", "v1")),
                name=str(strategy.get("name", "trend-pullback")),
            ),
        ),
        warnings=tuple(warnings),
    )


def _interval(section: dict[str, Any], key: str) -> float:
    value = section.get(key, 0)
    try:
        hours = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"automation.{key} must be a number, got {value!r}") from exc
    if hours < 0:
        raise ConfigError(f"automation.{key} must be >= 0 (0 disables the job)")
    return hours


NEXT_TF = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1d"}


def _default_confirm_tf(timeframe: str) -> str:
    return NEXT_TF.get(timeframe, "1d")


def _default_allocated(root: Path) -> float:
    state = root / "state" / "account.json"
    if state.exists():
        try:
            return float(json.loads(state.read_text(encoding="utf-8")).get("allocated_capital", 1000.0))
        except (OSError, ValueError, json.JSONDecodeError):
            return 1000.0
    return 1000.0


def _risk_limits(risk: dict[str, Any]) -> RiskLimits:
    return RiskLimits(
        max_account_allocation_pct=_num(risk, "max_account_allocation_pct", 5.0),
        max_position_pct_of_allocated_capital=_num(risk, "max_position_pct_of_allocated_capital", 20.0),
        max_risk_per_trade_pct=_num(risk, "max_risk_per_trade_pct", 0.5),
        max_daily_loss_pct=_num(risk, "max_daily_loss_pct", 1.5),
        max_weekly_loss_pct=_num(risk, "max_weekly_loss_pct", 4.0),
        max_drawdown_pct=_num(risk, "max_drawdown_pct", 8.0),
        max_trades_per_day=int(_num(risk, "max_trades_per_day", 4)),
        max_spread_pct=_num(risk, "max_spread_pct", 0.30),
        max_slippage_pct=_num(risk, "max_slippage_pct", 0.20),
        min_reward_risk_ratio=_num(risk, "min_reward_risk_ratio", 1.5),
        cooldown_minutes_after_loss=int(_num(risk, "cooldown_minutes_after_loss", 60)),
        stop_on_data_staleness_seconds=int(_num(risk, "stop_on_data_staleness_seconds", 120)),
        stop_on_consecutive_errors=int(_num(risk, "stop_on_consecutive_errors", 3)),
    )
