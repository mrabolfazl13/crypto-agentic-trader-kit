"""Characterize the market regime of each stored month (one-shot measurement).

Reads the stored 1m parquet month by month, resamples to 4h, and reports per
month: median/mean buy&hold return of the universe, share of positive symbols,
median monthly realized volatility (std of 4h simple returns x sqrt(180)), and
a coarse UP/DOWN/FLAT label from the median return. No strategy logic involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from cat.config import load_config  # noqa: E402
from cat.quant.features import resample  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.storage import load_manifest, load_universe, read_month  # noqa: E402


def main() -> int:
    settings = load_quant_settings(load_config(root=ROOT))
    universe = load_universe(settings)
    manifest = load_manifest(settings)
    rows = []
    for month in settings.months:
        returns, vols = [], []
        for symbol in universe["_symbols"]:
            record = (manifest.get("symbols") or {}).get(symbol) or {}
            if month not in (record.get("months") or {}):
                continue
            frame = read_month(settings, symbol, month)
            if frame.empty:
                continue
            bars = resample(frame, "4h")
            closes = bars["close"].astype(float)
            if len(closes) < 2:
                continue
            hold = float(closes.iloc[-1] / closes.iloc[0] - 1.0) * 100.0
            rets = closes.pct_change().dropna()
            vol = float(rets.std(ddof=1)) * float(np.sqrt(180.0)) * 100.0
            returns.append(hold)
            vols.append(vol)
        if not returns:
            rows.append({"month": month, "symbols": 0})
            continue
        med = float(np.median(returns))
        rows.append({
            "month": month,
            "symbols": len(returns),
            "median_hold_pct": round(med, 2),
            "mean_hold_pct": round(float(np.mean(returns)), 2),
            "positive_share_pct": round(100.0 * sum(1 for r in returns if r > 0) / len(returns), 1),
            "p10_hold_pct": round(float(np.percentile(returns, 10)), 2),
            "p90_hold_pct": round(float(np.percentile(returns, 90)), 2),
            "median_monthly_vol_pct": round(float(np.median(vols)), 2),
            "regime": "UP" if med > 5.0 else ("DOWN" if med < -5.0 else "FLAT"),
        })
    out = ROOT / "state" / "quant" / "results" / "regime_profile.json"
    out.write_text(json.dumps({"months": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
