"""Market adapters: one registry, two venues, zero forks of the crypto path.

``crypto`` wraps the existing Gate/MEXC providers; ``tse`` adds the Tehran Stock
Exchange over public TSETMC data. Everything downstream (risk gate, paper
broker, engine, dashboard) reads a :class:`MarketSpec` instead of assuming a
24/7 crypto venue.
"""

from __future__ import annotations

from .base import MarketSpec, get_spec, names, register
from .crypto import SPEC as CRYPTO_SPEC
from .tse import SPEC as TSE_SPEC

__all__ = ["MarketSpec", "get_spec", "names", "register", "CRYPTO_SPEC", "TSE_SPEC"]
