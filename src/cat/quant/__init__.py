"""Local, PAPER-only quantitative research core.

This package is deliberately separate from the live/paper trading engine: it reads
historical candles, builds features and simulates hypothetical trades. It has no
broker, no order method and no API keys. The only mutable state is local research
storage and the research database.
"""

from .settings import QuantSettings, load_quant_settings

__all__ = ["QuantSettings", "load_quant_settings"]
