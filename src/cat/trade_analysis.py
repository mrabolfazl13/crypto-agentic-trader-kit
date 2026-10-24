"""Post-trade analysis built only on the evidence the engine recorded.

Two layers stay separated everywhere:
- code-recorded reasons (entry_reason/exit_reason written by the engine at run time)
- the post-trade analytical explanation generated here, which is a hypothesis
  about the past, never a justification the engine used to trade.

Groups with few trades are flagged instead of hidden, and every generated
hypothesis ships with pre-set success/failure criteria before any experiment.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

MIN_SAMPLE = 20      # group size from which a rule may even be discussed
WEAK_SAMPLE = 5      # below this the group is labelled insufficient
MIN_OOS_TRADES = 8   # same bar the research acceptance gate uses

REPORT_ID = re.compile(r"^ta-[0-9a-f]{12}$")

HOUR_BUCKETS = ((1, "زیر ۱ ساعت"), (6, "۱ تا ۶ ساعت"), (24, "۶ تا ۲۴ ساعت"),
                (72, "۱ تا ۳ روز"), (None, "بیش از ۳ روز"))

REGIME_FA = {
    "TRENDING_UP": "روند صعودی",
    "TRENDING_DOWN": "روند نزولی",
    "RANGING": "ردیفی",
    "HIGH_VOLATILITY": "نوسان بالا",
    "UNKNOWN": "نامشخص",
}


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN guard


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    ev = row.get("evidence")
    if callable(getattr(ev, "evidence", None)):
        ev = ev.evidence()
    return ev if isinstance(ev, dict) else {}


def _ts(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _pnl(row: dict[str, Any]) -> float:
    return _num(row.get("pnl")) or 0.0


def _return_pct(row: dict[str, Any]) -> float | None:
    entry, qty = _num(row.get("entry")), _num(row.get("qty"))
    if not entry or not qty:
        return None
    return _pnl(row) / (entry * qty) * 100.0


def explain_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Ledger + code reason + the clearly-labelled post-trade analysis."""
    ev = _evidence(row)
    entry, exit_price = _num(row.get("entry")), _num(row.get("exit"))
    qty = _num(row.get("qty"))
    pnl = _pnl(row)
    initial_stop = _num(ev.get("initial_stop"))
    mfe, mae = _num(ev.get("mfe_pct")), _num(ev.get("mae_pct"))
    risk_pct = _num(ev.get("risk_pct"))
    holding = _num(ev.get("holding_hours"))

    realized_r = None
    if initial_stop is not None and entry is not None and qty and entry > initial_stop:
        risk_amount = qty * (entry - initial_stop)
        if risk_amount > 0:
            realized_r = pnl / risk_amount

    lines: list[str] = []
    uncertainties: list[str] = []

    if ev:
        regime = REGIME_FA.get(str(ev.get("regime") or ""), str(ev.get("regime") or "نامشخص"))
        confidence = _num(ev.get("confidence"))
        lines.append(
            f"ورود با دلیل ثبت‌شده‌ی کد «{ev.get('entry_reason') or 'نامشخص'}» در رژیم {regime}"
            + (f" با اطمینان {confidence:.2f}" if confidence is not None else "") + ".")
        if holding is not None:
            lines.append(f"خروج با دلیل ثبت‌شده «{row.get('reason') or 'نامشخص'}» پس از {holding:.1f} ساعت.")
        else:
            lines.append(f"خروج با دلیل ثبت‌شده «{row.get('reason') or 'نامشخص'}».")
    else:
        uncertainties.append("شواهد ورود برای این معامله ذخیره نشده (اجرای قدیمی موتور)؛ فقط دفتر معاملات موجود است.")

    if mfe is not None and mae is not None:
        lines.append(f"سود شناور تا {mfe:+.2f}٪ و زیان شناور تا {mae:+.2f}٪ دیده شد.")
    if realized_r is not None:
        lines.append(f"نتیجه‌ی نهایی {realized_r:+.2f}R نسبت به ریسک اولیه بود.")
    if row.get("reason") == "stop" and mae is not None and risk_pct is not None:
        lines.append("مسیر داخل کندل ثبت نشده است؛ عمقِ فراتر از استاپ فقط به‌صورت بازه‌ی کندل معلوم است.")
    if row.get("reason") == "target" and mfe is not None:
        lines.append("برخورد هدف داخل کندل رخ داده؛ زمان دقیق برخورد در دسترس نیست.")

    if initial_stop is None and ev:
        uncertainties.append("استاپ اولیه ثبت نشده؛ R واقعی محاسبه نمی‌شود.")

    return {
        "code_recorded": {
            "entry_reason": ev.get("entry_reason"),
            "exit_reason": row.get("reason"),
            "decision_time": ev.get("decision_time"),
            "confidence": ev.get("confidence"),
            "regime": ev.get("regime"),
            "volume_state": ev.get("volume_state"),
            "bullish": ev.get("bullish", []),
            "bearish": ev.get("bearish", []),
            "indicators": ev.get("indicators", {}),
            "initial_stop": initial_stop,
            "initial_target": _num(ev.get("initial_target")),
        },
        "ledger": {
            "symbol": row.get("symbol"),
            "run_id": row.get("run_id"),
            "entry_time": _ts(row.get("entry_time")),
            "exit_time": _ts(row.get("exit_time")),
            "entry": entry,
            "exit": exit_price,
            "qty": qty,
            "pnl": pnl,
            "fees": _num(row.get("fees")),
        },
        "analysis": {
            "realized_r": round(realized_r, 3) if realized_r is not None else None,
            "risk_pct": risk_pct,
            "rr_ratio": _num(ev.get("rr_ratio")),
            "mfe_pct": mfe,
            "mae_pct": mae,
            "capital_pct": _num(ev.get("capital_pct")),
            "lines": lines,
            "uncertainties": uncertainties,
        },
        "note": "بخش تحلیل پس از معامله است و جای دلیل ثبت‌شده‌ی کد را نمی‌گیرد؛ هیچ اطلاعات آینده در توجیه گذشته به کار نرفته است.",
    }


def _bucket(hours: float | None) -> str:
    if hours is None:
        return "نامشخص"
    for cap, label in HOUR_BUCKETS:
        if cap is None or hours < cap:
            return label
    return "نامشخص"


def group_value(row: dict[str, Any], group_by: str) -> dict[str, str]:
    """Key for one row under the requested dimension."""
    ev = _evidence(row)
    if group_by == "symbol":
        return {"key": str(row.get("symbol") or "?"), "label": str(row.get("symbol") or "?")}
    if group_by == "timeframe":
        return {"key": str(row.get("timeframe") or "?"), "label": str(row.get("timeframe") or "?")}
    if group_by == "side":
        side = str(ev.get("side") or "long")
        return {"key": side, "label": "خرید (لانگ)" if side == "long" else side}
    if group_by == "regime":
        raw = str(ev.get("regime") or "UNKNOWN")
        return {"key": raw, "label": REGIME_FA.get(raw, raw)}
    if group_by == "exit_reason":
        key = str(row.get("reason") or "unknown")
        return {"key": key, "label": key}
    if group_by == "entry_reason":
        key = str(ev.get("entry_reason") or "unknown")
        return {"key": key, "label": key}
    if group_by == "outcome":
        key = "win" if _pnl(row) > 0 else "loss"
        return {"key": key, "label": "سود" if key == "win" else "زیان"}
    if group_by == "hour":
        key = f"{_dt(_ts(row.get('exit_time'))).hour:02d}"
        return {"key": key, "label": f"ساعت {key} UTC"}
    if group_by == "weekday":
        names = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
        index = _dt(_ts(row.get("exit_time"))).weekday()
        return {"key": str(index), "label": names[index]}
    if group_by == "hold_bucket":
        holding = _num(ev.get("holding_hours"))
        label = _bucket(holding)
        return {"key": label, "label": label}
    raise ValueError(f"بُعد گروه‌بندی ناشناخته: {group_by}")


GROUP_DIMENSIONS = ("symbol", "timeframe", "side", "regime", "exit_reason", "entry_reason",
                    "outcome", "hour", "weekday", "hold_bucket")


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: _ts(r.get("exit_time")))
    pnls = [_pnl(r) for r in ordered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    n = len(pnls)
    cum = peak = dd = 0.0
    streak = worst_streak = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        if p <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0
    best_share = (max(wins) / gross_win * 100.0) if wins and gross_win > 0 else None
    returns = [_return_pct(r) for r in ordered]
    returns = [r for r in returns if r is not None]
    if n >= MIN_SAMPLE:
        sample = "sufficient"
    elif n >= WEAK_SAMPLE:
        sample = "weak"
    else:
        sample = "insufficient"
    guidance = {
        "insufficient": "تعداد نمونه برای قاعده‌سازی کافی نیست؛ فقط مشاهده است، نه مجوز معامله.",
        "weak": "نمونه کم است؛ هر الگوی دیده‌شده باید روی داده‌ی بیرون از نمونه تأیید شود.",
        "sufficient": (
            "انتظار ریاضی این گروه منفی است؛ تا اصلاح و تأیید بیرون از نمونه، از آن پرهیز شود."
            if sum(pnls) < 0 else
            "انتظار ریاضی این گروه مثبت است؛ این هنوز سود تضمین‌شده نیست و به دروازه‌ی بیرون از نمونه وابسته است."
        ),
    }[sample]
    return {
        "n": n,
        "sample_quality": sample,
        "win_rate_pct": round(len(wins) / n * 100.0, 2) if n else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else None,
        "net_pnl": round(sum(pnls), 4),
        "expectancy": round(sum(pnls) / n, 4) if n else None,
        "expectancy_pct": round(sum(returns) / len(returns), 4) if returns else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_pnl": round(dd, 4),
        "max_consecutive_losses": worst_streak,
        "best_trade_share_pct": round(best_share, 2) if best_share is not None else None,
        "guidance": guidance,
    }


def analyze_groups(rows: list[dict[str, Any]], *, group_by: str) -> list[dict[str, Any]]:
    """Every group is reported — losing groups are never dropped."""
    if group_by not in GROUP_DIMENSIONS:
        raise ValueError(f"بُعد گروه‌بندی ناشناخته: {group_by}")
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        gv = group_value(row, group_by)
        buckets.setdefault(gv["key"], {"label": gv["label"], "rows": []})["rows"].append(row)
    out = []
    for key, bucket in buckets.items():
        stats = _group_stats(bucket["rows"])
        stats.update({"key": key, "label": bucket["label"], "group_by": group_by})
        out.append(stats)
    out.sort(key=lambda g: (g["net_pnl"] if g["net_pnl"] is not None else 0.0))
    return out


def overlap_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Simultaneous-position risk across symbols from the trade intervals."""
    intervals = []
    for row in rows:
        start, end = _ts(row.get("entry_time")), _ts(row.get("exit_time"))
        if end > start:
            intervals.append((start, end, str(row.get("symbol") or "?")))
    if not intervals:
        return {"max_concurrent": 0, "pairs": [], "note": "بازه‌ی زمانی معامله‌ای در دسترس نیست."}
    events: list[tuple[int, int]] = []
    for start, end, _ in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    current = peak = 0
    peak_ts = 0
    for ts, delta in events:
        current += delta
        if current > peak:
            peak, peak_ts = current, ts
    symbols = sorted({s for _, _, s in intervals})
    pairs = []
    for i, sym_a in enumerate(symbols):
        for sym_b in symbols[i + 1:]:
            shared = 0.0
            count = 0
            for start, end, s1 in intervals:
                if s1 != sym_a:
                    continue
                for start2, end2, s2 in intervals:
                    if s2 != sym_b:
                        continue
                    lo, hi = max(start, start2), min(end, end2)
                    if hi > lo:
                        shared += hi - lo
                        count += 1
            if count:
                pairs.append({"symbols": f"{sym_a} × {sym_b}", "overlaps": count,
                              "overlap_hours": round(shared / 3600.0, 2)})
    note = ("این گزارش از بازه‌های ثبت‌شده‌ی معاملات چند اجرا ساخته می‌شود؛ "
            "همپوشانی به معنی ریسک هم‌زمانی است، نه همبستگی آماری اثبات‌شده.")
    return {"max_concurrent": peak, "peak_ts": peak_ts,
            "symbols": symbols, "pairs": pairs, "note": note}


def _hypothesis(h_id: str, question: str, supporting: list[str], opposing: list[str],
                related: int, change: str, experiment: str, grid: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": h_id,
        "question": question,
        "supporting_evidence": supporting,
        "opposing_evidence": opposing,
        "related_trades": related,
        "mechanism": "فرضِ پس از واقعه — هنوز اثبات نشده و فقط از الگوی داده برخاسته است.",
        "proposed_change": change,
        "success_criterion": (
            f"در اعتبارسنجی بیرون از نمونه با حداقل {MIN_OOS_TRADES} معامله، بازده خالص از قهرمان فعال بیشتر شود "
            "و حداقل نیمی از فولدها سودآور باشند (همان دروازه‌ی پذیرش research)."
        ),
        "failure_criterion": (
            f"کمتر از {MIN_OOS_TRADES} معامله، بازده خالص کمتر یا مساوی قهرمان، یا سودآوری نیمی از فولدها نشود."
        ),
        "required_experiment": experiment,
        "grid": grid,
        "status": "untested",
    }


def discover_hypotheses(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """At most three testable hypotheses per cycle, each with pre-set criteria."""
    if len(rows) < WEAK_SAMPLE:
        return {"hypotheses": [], "note": (
            f"فقط {len(rows)} معامله ثبت شده است؛ کمتر از حداقل نمونه برای فرضیه‌سازی. "
            "داده‌ی بیشتری لازم است، نه قاعده‌ی جدید.")}

    out: list[dict[str, Any]] = []

    stops = [r for r in rows if r.get("reason") == "stop"]
    if stops:
        losses = [_pnl(r) for r in stops if _pnl(r) <= 0]
        maes = [_num(_evidence(r).get("mae_pct")) for r in stops]
        maes = [m for m in maes if m is not None]
        supporting = [f"{len(stops)} خروج روی استاپ ثبت شده است." +
                      (f" میانگین زیان شناور این خروجی‌ها {median(maes):+.2f}٪ بوده است." if maes else "")]
        opposing = []
        if not losses:
            opposing.append("هیچ خروج استاپی زیان خالص نداشته است.")
        out.append(_hypothesis(
            "h-wider-stop-bracket",
            "آیا براکت خروج بازتر، خروج‌های زودهنگام روی استاپ را کم می‌کند بدون آنکه زیان میانگین را غیرقابل‌تحمل کند؟",
            supporting, opposing, len(stops),
            "بازکردن هم‌زمان استاپ و هدف به اندازه‌ی یک پله، طوری که نسبت سود/ریسک خالص زیر گات ۱٫۵ نرود "
            "(استاپِ بازتر با هدف ثابت هرگز از گات سود/ریسک نمی‌گذرد و آزمایش بی‌معامله می‌ماند).",
            "grid: استاپ و هدف پله‌ی بعدی روی همان داده، با folds و warmup جاری.",
            # Pairing rule: target >= 1.5*stop + 1.5 keeps net RR >= 1.5 for atr_pct >= 1.0
            # under the 0.6% round-trip cost; stop-only widening is structurally untradeable.
            {"atr_stop_mult": [2.4, 3.6], "atr_target_mult": [5.2, 7.2]},
        ))

    regime_groups = [g for g in analyze_groups(rows, group_by="regime") if g["n"] >= WEAK_SAMPLE]
    losing_regimes = [g for g in regime_groups if (g["expectancy"] or 0.0) < 0]
    winning_regimes = [g for g in regime_groups if (g["expectancy"] or 0.0) > 0]
    if losing_regimes and winning_regimes:
        names = "، ".join(g["label"] for g in losing_regimes)
        out.append(_hypothesis(
            "h-regime-gate",
            f"آیا حذف ورود در رژیم‌های {names}، انتظار ریاضی کل را بالا می‌برد؟",
            [f"رژیم {g['label']} با {g['n']} معامله انتظار ریاضی {g['expectancy']:+.4f} دارد." for g in losing_regimes],
            [f"رژیم {g['label']} با {g['n']} معامله انتظار ریاضی {g['expectancy']:+.4f} دارد." for g in winning_regimes],
            sum(g["n"] for g in losing_regimes),
            "محدودکردن دروازه‌ی ورود به رژیم‌هایی که انتظار ریاضی مثبت نشان داده‌اند.",
            "grid: کلید رژیم روی همان داده، با folds و warmup جاری.",
            {"regime_gate": sorted(g["key"] for g in winning_regimes)},
        ))

    winners = [r for r in rows if _pnl(r) > 0]
    give_backs = []
    for row in winners:
        mfe, ret = _num(_evidence(row).get("mfe_pct")), _return_pct(row)
        if mfe is not None and ret is not None and mfe > 0:
            give_backs.append(mfe - ret)
    if len(give_backs) >= WEAK_SAMPLE and median(give_backs) > 0.5:
        out.append(_hypothesis(
            "h-earlier-target",
            "آیا برنده‌ها بخش بزرگی از سود شناورشان را پس می‌دهند و هدف زودتر، سود خالص بیشتری می‌سازد؟",
            [f"میانه‌ی سود برگشتیِ برنده‌ها {median(give_backs):+.2f} درصد واحد است ({len(give_backs)} معامله سودده)."],
            ["خروج زودتر می‌تواند سهم برنده‌های بزرگ را هم کم کند؛ این ریسک باید در همان آزمون سنجیده شود."],
            len(give_backs),
            "جلوبردن یک پله‌ی نسبت هدف/ریسک در گرید خانواده‌ی فعال.",
            "grid: نسبت هدف پله‌ی بعدی روی همان داده، با folds و warmup جاری.",
            {"atr_target_mult": [2.5, 3.5]},
        ))

    out.sort(key=lambda h: -h["related_trades"])
    note = ("فرضیه‌ها از شواهد ثبت‌شده برخاسته‌اند اما اثبات نیستند؛ داوری فقط با آزمون بیرون از نمونه انجام می‌شود "
            "و حداکثر سه فرضیه در هر چرخه پیشنهاد می‌گردد.")
    return {"hypotheses": out[:3], "note": note}


REPORT_DIMENSIONS = ("symbol", "regime", "exit_reason", "entry_reason", "side",
                     "hold_bucket", "weekday", "hour")


def report_dir(root: Path) -> Path:
    return Path(root) / "state" / "research" / "trade-analysis"


def build_report(store: Any, *, run_ids: list[str] | None = None, limit_runs: int = 10) -> dict[str, Any]:
    """Assemble the full analysis payload from real stored runs — no synthetic fills."""
    if run_ids:
        resolved = []
        for run_id in run_ids:
            run = store.get_run(run_id)
            if run is None:
                raise ValueError(f"اجرا یافت نشد: {run_id}")
            resolved.append(run)
    else:
        listed = store.list_runs(kind="backtest", limit=limit_runs)
        if not listed:
            raise ValueError("هیچ اجرای بک‌تست ذخیره نشده است؛ اول یک بک‌تست واقعی اجرا کنید.")
        resolved = [run for run in (store.get_run(item["id"]) for item in listed) if run is not None]
        if not resolved:
            raise ValueError("اجراهای ذخیره‌شده خوانا نیستند؛ اول یک بک‌تست واقعی اجرا کنید.")

    rows: list[dict[str, Any]] = []
    run_labels = []
    for run in resolved:
        run_labels.append({"run_id": run["id"], "symbol": run.get("symbol"),
                           "timeframe": run.get("timeframe"), "params_label": run.get("params_label"),
                           "dataset_id": run.get("dataset_id"),
                           "created_at": run.get("created_at")})
        for row in run.get("trades") or []:
            row.setdefault("timeframe", run.get("timeframe"))
            rows.append(row)

    note = ("این گزارش فقط از معاملات ثبت‌شده‌ی اجراهای واقعی ساخته شده است؛ "
            "هیچ عددی تولید یا تکمیل نمی‌شود.")
    if not rows:
        note = "اجرای انتخاب‌شده هیچ معامله‌ای ثبت نکرده است؛ برای تحلیل، اجرایی با معامله لازم است."

    return {
        "report_id": f"ta-{uuid.uuid4().hex[:12]}",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "run_ids": [run["id"] for run in resolved],
        "run_labels": run_labels,
        "trade_count": len(rows),
        "symbols": sorted({str(row.get("symbol") or "?") for row in rows}),
        "groups": {dim: analyze_groups(rows, group_by=dim) for dim in REPORT_DIMENSIONS},
        "overlap": overlap_report(rows),
        "discovery": discover_hypotheses(rows),
        "explanations": [explain_trade(row) for row in rows],
        "note": note,
    }


def save_report(root: Path, payload: dict[str, Any]) -> Path:
    if not REPORT_ID.match(str(payload.get("report_id"))):
        raise ValueError(f"report id نامعتبر: {payload.get('report_id')!r}")
    base = report_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{payload['report_id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def list_reports(root: Path) -> list[dict[str, Any]]:
    base = report_dir(root)
    if not base.exists():
        return []
    out = []
    for path in sorted(base.glob("ta-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "report_id": payload.get("report_id"),
            "generated_at": payload.get("generated_at"),
            "trade_count": payload.get("trade_count"),
            "symbols": payload.get("symbols"),
            "run_ids": payload.get("run_ids"),
        })
    return out


def load_report(root: Path, report_id: str) -> dict[str, Any] | None:
    if not REPORT_ID.match(str(report_id)):
        return None
    path = report_dir(root) / f"{report_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
