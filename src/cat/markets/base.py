"""Market adapter contract: how one venue's data, symbols and session rules plug in.

Adding a venue must not mean editing the crypto path. A spec bundles the things
the engine, risk gate and dashboard need to know about a market:

* which provider object answers ``klines``/``klines_range``/``quote``/``filters``
  (the duck-typed contract already used by :mod:`cat.data`),
* whether a second, independent price source exists for the cross-check,
* where the exchange clock comes from,
* what "fresh" means for a market that closes every day.

``freshness_reference`` is the one genuinely new idea: for a 24/7 venue it is the
wall clock, for a session venue it stops at the last session close so that
nighttime snapshots are *expected* rather than stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..http import HttpClient

__all__ = ["MarketSpec", "get_spec", "names", "no_cross", "register"]

_REGISTRY: dict[str, "MarketSpec"] = {}


def _always_on(now_epoch: float) -> float:
    """24/7 venues: the wall clock is the freshness anchor."""
    return now_epoch


def no_cross(_client: HttpClient, _config: Any) -> None:
    """Canonical "this venue has no independent second price source" adapter."""
    return None


@dataclass(frozen=True)
class MarketSpec:
    name: str
    title: str
    currency: str
    quantity_unit: str
    timeframes: tuple[str, ...]
    # (client, config) -> provider answering the data.py duck-typed contract.
    provider: Callable[[HttpClient, Any], Any]
    # (client, config) -> second source with .quote(symbol) and .name, or None.
    cross: Callable[[HttpClient, Any], Any] = no_cross
    # (client, config) -> {"server_time", "local_time", "skew_seconds", "acceptable"}
    clock: Callable[[HttpClient, Any], dict[str, Any]] | None = None
    # epoch -> newest instant at which new data could have arrived.
    freshness_reference: Callable[[float], float] = _always_on
    # (prev_open, next_open) -> True when the gap is an expected session gap.
    # None keeps the 24/7 rule untouched for venues that never close.
    gap_is_expected: Callable[[float, float], bool] | None = None
    # (spec, config) -> allowlist the risk gate enforces for this market.
    symbols: Callable[..., tuple[str, ...]] | None = None
    # Extra headers for every data request (TSETMC serves JSON only to browser agents).
    http_headers: dict[str, str] = field(default_factory=dict)
    # Venue defaults used only when config.yaml stays silent; config always wins.
    confirm_timeframe: str | None = None
    default_maker_pct: float | None = None
    default_taker_pct: float | None = None
    #: Warning codes attached to every snapshot from this venue (honesty about
    #: what the feed cannot prove, e.g. the absence of a second price source).
    quality_warnings: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def build_provider(self, client: HttpClient, config: Any) -> Any:
        return self.provider(client, config)

    def build_cross(self, client: HttpClient, config: Any) -> Any:
        return self.cross(client, config)

    def allowlist(self, config: Any) -> tuple[str, ...]:
        """Venue-specific allowlist; venues without one use ``config.symbols`` verbatim."""
        if self.symbols is not None:
            return tuple(self.symbols(self, config))
        return tuple(getattr(config, "symbols", ()) or ())

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "currency": self.currency,
            "quantity_unit": self.quantity_unit,
            "timeframes": list(self.timeframes),
            "session_gaps": self.gap_is_expected is not None,
            "cross_check": self.cross is not no_cross,
            "quality_warnings": list(self.quality_warnings),
            "notes": list(self.notes),
        }


def register(spec: MarketSpec) -> MarketSpec:
    _REGISTRY[spec.name] = spec
    return spec


def get_spec(name: str | None) -> MarketSpec:
    key = (name or "crypto").strip().lower()
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"unknown market {name!r}; known markets: {', '.join(sorted(_REGISTRY))}") from exc


def names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
