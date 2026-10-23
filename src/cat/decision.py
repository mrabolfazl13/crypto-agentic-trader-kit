"""TradeDecision construction and JSON-Schema conformance."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .risk import RiskResult
from .strategy import Analysis, Proposal


def schema_path(root: Path) -> Path:
    return Path(root) / "schemas" / "trade_decision.schema.json"


def validator(root: Path) -> Draft202012Validator:
    schema = json.loads(schema_path(root).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def build_decision(
    *,
    decision_id: str,
    now: datetime,
    snapshot,
    proposal: Proposal,
    risk: RiskResult,
    analysis: Analysis,
) -> dict[str, Any]:
    rationale = list(proposal.rationale) + [
        f"risk engine {'APPROVED' if risk.approved else 'REJECTED'} after checks: {', '.join(risk.checks)}"
    ]
    if not risk.approved:
        rationale += [f"blocked: {reason}" for reason in risk.reasons[:3]]
    decision = {
        "decision_id": decision_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "symbol": snapshot.symbol,
        "action": proposal.action,
        "confidence": round(float(proposal.confidence), 4),
        "rationale": rationale,
        "entry_price": round(risk.entry, 8) if risk.entry else None,
        "stop_price": round(risk.stop, 8) if risk.stop else None,
        "target_price": round(risk.target, 8) if risk.target else None,
        "risk": {
            "estimated_fee_pct": round(risk.est_fee_pct, 6),
            "estimated_slippage_pct": round(risk.est_slippage_pct, 6),
            "position_size_quote": round(risk.notional, 8),
            "reward_risk_ratio": round(risk.reward_risk_ratio, 4) if risk.reward_risk_ratio else None,
        },
        "data_quality": snapshot.quality.as_schema(),
    }
    extras = {
        "regime": analysis.regime,
        "signals_bullish": analysis.bullish,
        "signals_bearish": analysis.bearish,
        "indicators": analysis.indicators,
        "scenarios": analysis.scenarios,
        "invalidation": analysis.invalidation,
        "base_position_size": round(risk.qty, 12),
        "strategy_reason": proposal.reason,
        "risk_approved": bool(risk.approved),
        "risk_reasons": list(risk.reasons),
    }
    return {"decision": decision, "extras": extras}


def validate_decision(root: Path, decision: dict[str, Any]) -> list[str]:
    errors = sorted(validator(root).iter_errors(decision), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
