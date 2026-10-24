"""Autonomous, resumable optimization loop (research/PAPER only).

One experiment = one pre-registered hypothesis measured by ``research.run_research``
on closed candles: split -> grid on train -> out-of-sample gate -> stored learning.
A rejected hypothesis never trades; it only changes what the next experiment asks.

The loop is deliberately small in authority: it cannot change risk limits, symbols,
mode or capital. It only searches strategy fields that ``research.validate_grid``
already allows, and every accepted step still has to pass ``research.acceptance``.
State lives in ``state/research/optimizer/<run_id>.json`` so stop/resume and an
API crash cannot lose the experiment history.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config

DISCLAIMER_FA = (
    "این حلقه فقط پژوهش روی کندل‌های بسته است: سفارشی نمی‌سازد، حالت را عوض نمی‌کند و "
    "حدود ریسک را لمس نمی‌کند. بازده گذشته تضمین سود آینده نیست."
)

# Each hypothesis changes one family of strategy parameters and is tagged with the
# failure it is meant to answer, so the next experiment can be chosen from evidence.
PLAN: tuple[dict[str, Any], ...] = (
    {
        "id": "entry-speed",
        "question": "آیا ورود سریع‌تر به روند، بازده خارج از نمونه را بهتر می‌کند؟",
        "grid": {"ema_fast": [8, 12]},
        "answers": "more_trades",
    },
    {
        "id": "stop-distance",
        "question": "آیا فاصله‌ی حد ضرر، افت سرمایه را کم می‌کند؟",
        "grid": {"atr_stop_mult": [1.2, 1.8]},
        "answers": "risk",
    },
    {
        "id": "target-distance",
        "question": "آیا هدف دورتر، انتظار ریاضی را مثبت می‌کند؟",
        "grid": {"atr_target_mult": [2.5, 4.5]},
        "answers": "edge",
    },
    {
        "id": "rsi-band",
        "question": "آیا باند RSI، کیفیت ورود را بالا می‌برد؟",
        "grid": {"rsi_bullish_low": [40, 50]},
        "answers": "more_trades",
    },
    {
        "id": "volatility-floor",
        "question": "آیا کف نوسان، معاملات بی‌لبه را حذف می‌کند؟",
        "grid": {"atr_pct_threshold": [1.0, 2.0]},
        "answers": "edge",
    },
    {
        "id": "trend-strictness",
        "question": "آیا سخت‌گیری بیشتر روی شیب روند، ضرر رژیم نزولی را کم می‌کند؟",
        "grid": {"slope_threshold": [0.03, 0.09]},
        "answers": "risk",
    },
    # Round 2 — pre-registered from the round-1 evidence on ds-ex-e44bf6ffb4
    # (BTC/4h, 400 days): all six single-knob variants finished with 0 of 3 folds
    # profitable and net −0.12%…−0.44%; exit-distance knobs were the least bad.
    # Each hypothesis still changes exactly one family of strategy fields.
    {
        "id": "holding-minimum",
        "question": "آیا حداقل دور‌ه‌ی نگه‌داشت بلندتر، ضرر ناشی از گردش و کارمزد را کم می‌کند؟",
        "grid": {"min_bars": [60, 120, 180]},
        "answers": "edge",
    },
    {
        "id": "rsi-ceiling",
        "question": "آیا سقف RSI پایین‌تر، از خریدن پولبک‌های بیش‌ازحد گرم جلوگیری می‌کند؟",
        "grid": {"rsi_bullish_high": [58, 62, 68]},
        "answers": "edge",
    },
    {
        "id": "entry-volume-quality",
        "question": "آیا رد کردن ورودها در حجم ضعیف، کیفیت سیگنال را بالا می‌برد؟",
        "grid": {"volume_weak_ratio": [0.7, 1.0]},
        "answers": "edge",
    },
    {
        "id": "trend-separation",
        "question": "آیا فاصله‌ی بیشتر بین EMAها، ورود در روند واقعی را قطعی‌تر می‌کند؟",
        "grid": {"ema_slow": [80, 120, 200]},
        "answers": "edge",
    },
    {
        "id": "pullback-depth",
        "question": "آیا صبر برای افت عمیق‌تر قبل از ورود، میانگین معامله را مثبت می‌کند؟",
        "grid": {"rsi_weak": [30, 35, 40]},
        "answers": "edge",
    },
    {
        "id": "exit-asymmetry",
        "question": "آیا ترکیب حد ضرر بازتر با هدف دورتر، انتظار ریاضی را مثبت می‌کند؟",
        "grid": {"atr_stop_mult": [1.8, 2.4], "atr_target_mult": [4.0, 5.5]},
        "answers": "edge",
    },
    # Round 3 — pre-registered from round 2 on the same dataset: every hypothesis
    # was rejected, but exit-asymmetry (stop 2.4 / target 5.5) cut the OOS loss to
    # −0.038% with PF 0.657, while entry-side knobs left the metrics at the base
    # variant's values. So the evidence points at the exit bracket, not the entry.
    {
        "id": "exit-bracket-wider",
        "question": "آیا ادامه‌ی بازتر کردن همان براکت خروج، زیان خارج از نمونه را به صفر می‌رساند؟",
        "grid": {"atr_stop_mult": [2.4, 3.0, 3.6], "atr_target_mult": [5.5, 6.5, 7.5]},
        "answers": "edge",
    },
    {
        "id": "stop-only-wider",
        "question": "آیا بهبود دور ۲ فقط از حد ضرر بازتر آمد یا از هدف دورتر؟",
        "grid": {"atr_stop_mult": [3.0, 3.6]},
        "answers": "edge",
    },
    {
        "id": "target-only-far",
        "question": "آیا تنها جابه‌جایی هدف (بدون تغییر حد ضرر) سود خارج از نمونه می‌سازد؟",
        "grid": {"atr_target_mult": [6.0, 7.5]},
        "answers": "edge",
    },
    # Round 4 — pre-registered from rounds 1-3 on ds-ex-e44bf6ffb4 + ETH replication:
    # 15 single-knob hypotheses on two symbols all ended 0-of-3 profitable folds;
    # exit-asymmetry's BTC near-zero (−0.038%) did NOT repeat on ETH (−0.269%), so
    # it was window noise. The untested knobs are the text/bool switches.
    {
        "id": "regime-gate-highvol",
        "question": "آیا گشودن دروازه به رژیم پرنوسان، نمونه‌ی خارج از نمونه را بدون ضرر بزرگ‌تر می‌کند؟",
        "grid": {"regime_gate": ["TRENDING_UP", "HIGH_VOLATILITY"]},
        "answers": "more_trades",
    },
    {
        "id": "regime-gate-ranging",
        "question": "آیا ورود در regime خنثی (به‌جای روند صعودی) بازده خارج از نمونه را تغییر می‌دهد؟",
        "grid": {"regime_gate": ["TRENDING_UP", "RANGING"]},
        "answers": "more_trades",
    },
    {
        "id": "conflict-guard-off",
        "question": "آیا غیرفعال‌کردن نگهبان تضاد سیگنال، ورودهای معتبرِ جا‌مانده را برمی‌گرداند؟",
        "grid": {"no_trade_if_signals_conflict": [True, False]},
        "answers": "more_trades",
    },
    # Round 5 — the classifier itself: round-3 exit gains coincided with almost
    # every daily bar classifying as HIGH_VOLATILITY; a higher volatility ceiling
    # lets the directional labels be reached at all. One knob: vol_threshold.
    {
        "id": "volatility-ceiling",
        "question": "آیا بالا بردن سقف نوسانِ طبقه‌بندی رژیم، دروازه‌ی روند را اندازه‌گیری‌پذیر می‌کند؟",
        "grid": {"vol_threshold": [1.0, 2.0, 3.0]},
        "answers": "more_trades",
    },
    {
        "id": "multi-signal-requirement",
        "question": "آیا برداشتن شرط چند‌سیگناله بودن، بسامد آزمون را بدون نابودی ریسک بالا می‌برد؟",
        "grid": {"require_multiple_signals": [True, False]},
        "answers": "more_trades",
    },
    # Round 6 — pre-registered from round 4: gating on RANGING gave the first
    # positive OOS numbers in the whole study (BTC/4h 400d: +0.095%, PF 2.236,
    # DD 0.177%) but with only 4 trades, below the pre-set minimum of 8. This is
    # a sample-size experiment, NOT a re-judged one: acceptance rules are unchanged.
    {
        "id": "ranging-entry-band",
        "question": "آیا باز کردن باند RSI در گیت RANGING، نمونه‌ی خارج از نمونه را به کف ۸ معامله می‌رساند؟",
        "grid": {"regime_gate": ["RANGING"], "rsi_bullish_low": [30, 40, 50]},
        "answers": "more_trades",
    },
)

_ACTIVE: dict[str, threading.Event] = {}
_LOCK = threading.Lock()
# Windows refuses os.replace while a reader holds the target open; serialise
# state reads/writes in-process and retry briefly instead of losing a step.
_IO_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir(root: Path) -> Path:
    return Path(root) / "state" / "research" / "optimizer"


def _path(root: Path, run_id: str) -> Path:
    if not run_id.startswith("opt-") or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError(f"run_id نامعتبر: {run_id}")
    return state_dir(root) / f"{run_id}.json"


def _write(state: dict[str, Any], root: Path) -> None:
    path = _path(root, state["run_id"])
    with _IO_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                threading.Event().wait(0.02 * (attempt + 1))


def load_state(root: Path, run_id: str) -> dict[str, Any]:
    path = _path(root, run_id)
    with _IO_LOCK:
        if not path.exists():
            raise FileNotFoundError(run_id)
        return json.loads(path.read_text(encoding="utf-8"))


def list_states(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    directory = state_dir(root)
    if not directory.exists():
        return []
    files = sorted(directory.glob("opt-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    states = []
    for path in files[: max(0, limit)]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        states.append(summary(state))
    return states


def summary(state: dict[str, Any]) -> dict[str, Any]:
    experiments = state.get("experiments", [])
    accepted = [item for item in experiments if item.get("verdict") == "accepted"]
    return {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "symbol": state.get("symbol"),
        "timeframe": state.get("timeframe"),
        "dataset_id": state.get("dataset_id"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "planned": len(state.get("queue", [])),
        "completed": len(experiments),
        "accepted": len(accepted),
        "rejected": len(experiments) - len(accepted),
        "current": state.get("current"),
        "error": state.get("error"),
        # An experiment with no trades returns 0.0; calling that the "best" would read
        # like break-even, so only measured trades count.
        "best_net_return_pct": max(
            (float(item["net_return_pct"]) for item in experiments
             if isinstance(item.get("net_return_pct"), (int, float)) and int(item.get("trades") or 0) > 0),
            default=None,
        ),
        "champion_label": (state.get("champion") or {}).get("params_label"),
        "disclaimer": DISCLAIMER_FA,
    }


def new_state(*, symbol: str, timeframe: str, dataset_id: str, max_experiments: int,
              folds: int, warmup: int, spread_pct: float,
              start_at: str | None = None) -> dict[str, Any]:
    begin = 0
    if start_at:
        ids = [item["id"] for item in PLAN]
        if start_at not in ids:
            raise ValueError(f"فرضیه‌ی «{start_at}» در فهرست پیش‌ثبت‌شده نیست: {', '.join(ids)}")
        begin = ids.index(start_at)
    plan = [item["id"] for item in PLAN[begin: begin + max_experiments]]
    return {
        "run_id": f"opt-{uuid.uuid4().hex[:12]}",
        "status": "running",
        "created_at": _now(),
        "updated_at": _now(),
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_id": dataset_id,
        "folds": folds,
        "warmup": warmup,
        "spread_pct": spread_pct,
        "queue": plan,
        "current": plan[0] if plan else None,
        "start_at": start_at,
        "experiments": [],
        "champion": None,
        "learnings": [],
        "next_reason": "ترتیب پیش‌ثبت‌شده‌ی فرضیه‌ها؛ هنوز رد یا پذیرشی ثبت نشده است.",
        "error": None,
        "disclaimer": DISCLAIMER_FA,
    }


def load_context(store: Any, config: Config, *, symbol: str, timeframe: str, dataset_id: str):
    """Reload the exact stored dataset, so resume measures the same candles."""
    from . import datasets

    bars = datasets.load_klines(store, dataset_id)
    higher = list(bars)
    confirm_tf = config.confirm_timeframe
    if confirm_tf and confirm_tf != timeframe:
        try:
            aggregated = datasets.resample(bars, confirm_tf)
            if len(aggregated) >= 60:
                higher = aggregated
        except Exception:  # noqa: BLE001 - a coarser confirm series is optional
            higher = list(bars)
    from .data import SymbolFilter

    filters = SymbolFilter(symbol=symbol, price_precision=2,
                           qty_precision=6, min_qty=0.0, min_quote=0.0, max_quote=0.0,
                           tradable=True, fee_pct=config.taker_fee_pct)
    return bars, higher, filters


def run_experiment(config: Config, store: Any, *, symbol: str, timeframe: str, bars: list,
                   higher: list, filters: Any, dataset_id: str, hypothesis: dict[str, Any],
                   champion: dict[str, Any] | None, folds: int, warmup: int,
                   spread_pct: float) -> dict[str, Any]:
    from . import research

    return research.run_research(
        store, config, symbol=symbol, timeframe=timeframe, bars=bars, higher_bars=higher,
        filters=filters, grid=hypothesis["grid"], folds=folds, champion=champion,
        warmup=warmup, assumed_spread_pct=spread_pct, dataset_id=dataset_id,
    )


def _reason_groups(reasons: list[str]) -> set[str]:
    groups: set[str] = set()
    for reason in reasons:
        lowered = str(reason).lower()
        if "trades" in lowered and "minimum" in lowered:
            groups.add("more_trades")
        if "drawdown" in lowered:
            groups.add("risk")
        if "profit factor" in lowered or "net-return" in lowered or "net return" in lowered:
            groups.add("edge")
        if "risk-limit" in lowered:
            groups.add("evidence")
    return groups


def choose_next(queue: list[str], completed: list[dict[str, Any]]) -> tuple[list[str], str]:
    """Move the hypothesis that answers the last rejection to the front."""
    if not completed:
        return queue, "ترتیب پیش‌ثبت‌شده‌ی فرضیه‌ها."
    last = completed[-1]
    if last.get("verdict") == "accepted":
        return queue, "آخرین نسخه پذیرفته شد؛ فرضیه‌ی بعدی طبق ترتیب پیش‌ثبت‌شده ادامه می‌یابد."
    groups = _reason_groups(last.get("reasons") or [])
    if not groups:
        return queue, "دلیل رد در هیچ دسته‌ی شناخته‌شده‌ای قرار نگرفت؛ ترتیب پیش‌ثبت‌شده حفظ شد."
    for target in ("more_trades", "risk", "edge"):
        if target not in groups:
            continue
        for index, hypothesis_id in enumerate(queue):
            if hypothesis_id in {item["id"] for item in PLAN if item["answers"] == target}:
                if index == 0:
                    return queue, f"دلیل رد «{target}» بود و فرضیه‌ی بعدی از قبل هم‌راستا بود."
                reordered = [hypothesis_id] + queue[:index] + queue[index + 1:]
                return reordered, (
                    f"دلیل رد «{target}» بود؛ فرضیه‌ی «{hypothesis_id}» که همان ضعف را هدف می‌گیرد، "
                    "به ابتدای صف منتقل شد."
                )
    return queue, "دسته‌ی رد با هیچ فرضیه‌ی باقی‌مانده هم‌راستا نبود؛ ترتیب پیش‌ثبت‌شده حفظ شد."


def _aggregate_metrics(aggregate: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Fold aggregates report medians/means; read both spellings so a real run is never blank."""
    net = aggregate.get("net_return_pct", aggregate.get("net_return_pct_median"))
    factor = aggregate.get("profit_factor", aggregate.get("profit_factor_mean"))
    return net, factor, aggregate.get("max_drawdown_pct")


def learning_text(body: dict[str, Any]) -> str:
    verdict = body.get("verdict")
    aggregate = ((body.get("walk_forward") or {}).get("aggregate") or {})
    net, _factor, drawdown = _aggregate_metrics(aggregate)
    trades = aggregate.get("trades")
    measured = f"خارج از نمونه: {trades} معامله، خالص {net}%، افت {drawdown}%"
    if verdict == "accepted":
        return f"پذیرفته شد ({body.get('candidate_label')}). {measured}. این نسخه جایگزین قهرمان شد."
    reasons = body.get("reasons") or []
    groups = _reason_groups([str(item) for item in reasons])
    lesson = {
        "more_trades": "نمونه‌ی خارج از نمونه کافی نبود؛ آزمایش بعدی باید تعداد ورودها را بالا ببرد.",
        "risk": "افت سرمایه بیشتر از حد تحمل بود؛ آزمایش بعدی باید ریسک هر معامله را کم کند.",
        "edge": "بهبود بازده/فاکتور سود خارج از نمونه ثبت نشد؛ آزمایش بعدی باید کیفیت ورود یا خروج را هدف بگیرد.",
        "evidence": "شواهد محدودیت ریسک ناقص بود؛ سیستم fail-closed عمل کرد.",
    }
    notes = " ".join(lesson[group] for group in ("more_trades", "risk", "edge", "evidence") if group in groups)
    return f"رد شد ({body.get('candidate_label')}). {measured}. {notes}".strip()


def _record(body: dict[str, Any], hypothesis: dict[str, Any]) -> dict[str, Any]:
    aggregate = ((body.get("walk_forward") or {}).get("aggregate") or {})
    net, factor, drawdown = _aggregate_metrics(aggregate)
    return {
        "index": None,
        "hypothesis_id": hypothesis["id"],
        "question": hypothesis["question"],
        "grid": hypothesis["grid"],
        "verdict": body.get("verdict"),
        "reasons": list(body.get("reasons") or []),
        "candidate_label": body.get("candidate_label"),
        "champion_label": body.get("champion_label"),
        "net_return_pct": net,
        "max_drawdown_pct": drawdown,
        "profit_factor": factor,
        "trades": aggregate.get("trades"),
        "dataset_id": body.get("dataset_id"),
        "split": body.get("split"),
        "learning": learning_text(body),
        "decided_at": _now(),
    }


def _champion_from(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "params_label": body.get("candidate_label"),
        "validation": body.get("validation"),
        "walk_forward": body.get("walk_forward"),
        "accepted": True,
    }


def _run_loop(config: Config, store: Any, state: dict[str, Any], bars: list, higher: list,
              filters: Any, stop_event: threading.Event, root: Path) -> None:
    try:
        while state["queue"]:
            if stop_event.is_set():
                state["status"] = "stopped"
                state["updated_at"] = _now()
                _write(state, root)
                return
            hypothesis_id = state["queue"][0]
            hypothesis = next(item for item in PLAN if item["id"] == hypothesis_id)
            state["current"] = hypothesis_id
            state["status"] = "running"
            state["updated_at"] = _now()
            _write(state, root)
            try:
                body = run_experiment(
                    config, store, symbol=state["symbol"], timeframe=state["timeframe"],
                    bars=bars, higher=higher, filters=filters, dataset_id=state["dataset_id"],
                    hypothesis=hypothesis, champion=state.get("champion"),
                    folds=int(state["folds"]), warmup=int(state["warmup"]),
                    spread_pct=float(state["spread_pct"]),
                )
            except Exception as exc:  # noqa: BLE001 - persist the failure, keep the history
                state["status"] = "failed"
                state["error"] = f"{type(exc).__name__}: {exc}"
                state["updated_at"] = _now()
                _write(state, root)
                return
            record = _record(body, hypothesis)
            record["index"] = len(state["experiments"]) + 1
            state["experiments"].append(record)
            state["learnings"].append(record["learning"])
            if record["verdict"] == "accepted":
                state["champion"] = _champion_from(body)
            state["queue"].pop(0)
            state["queue"], state["next_reason"] = choose_next(state["queue"], state["experiments"])
            state["current"] = state["queue"][0] if state["queue"] else None
            state["updated_at"] = _now()
            _write(state, root)
        state["status"] = "done"
        state["current"] = None
        state["updated_at"] = _now()
        _write(state, root)
    finally:
        with _LOCK:
            _ACTIVE.pop(state["run_id"], None)


def _start_thread(config: Config, store: Any, state: dict[str, Any], bars: list, higher: list,
                  filters: Any, root: Path) -> None:
    event = threading.Event()
    with _LOCK:
        if _ACTIVE:
            raise ValueError("یک حلقه‌ی بهینه‌سازی دیگر در حال اجراست؛ اول آن را متوقف یا تمام کنید.")
        _ACTIVE[state["run_id"]] = event
    thread = threading.Thread(
        target=_run_loop, args=(config, store, state, bars, higher, filters, event, root),
        name=f"optimizer-{state['run_id']}", daemon=True,
    )
    thread.start()


def start(config: Config, store: Any, *, symbol: str, timeframe: str, dataset_id: str,
          max_experiments: int, folds: int, warmup: int, spread_pct: float,
          start_at: str | None = None) -> dict[str, Any]:
    if not (1 <= max_experiments <= len(PLAN)):
        raise ValueError(f"max_experiments باید بین ۱ و {len(PLAN)} باشد")
    bars, higher, filters = load_context(store, config, symbol=symbol, timeframe=timeframe,
                                         dataset_id=dataset_id)
    from . import research

    # run_research splits 60/20/20 and refuses a segment under max(120, warmup+1);
    # validating the same floor here fails at the request instead of 0.5s later.
    segment = max(research.MIN_SEGMENT_BARS, warmup + 1)
    floor = segment * 5
    if len(bars) < floor:
        raise ValueError(
            f"داده کافی نیست: {len(bars)} کندل؛ برای تقسیم ۶۰/۲۰/۲۰ با warmup={warmup} "
            f"حداقل {floor} کندل لازم است (هر بخش ≥ {segment})."
        )
    state = new_state(symbol=symbol, timeframe=timeframe, dataset_id=dataset_id,
                      max_experiments=max_experiments, folds=folds, warmup=warmup,
                      spread_pct=spread_pct, start_at=start_at)
    _write(state, config.root)
    _start_thread(config, store, state, bars, higher, filters, config.root)
    return state


def stop(config: Config, run_id: str) -> dict[str, Any]:
    state = load_state(config.root, run_id)
    with _LOCK:
        event = _ACTIVE.get(run_id)
    if event is None:
        raise ValueError("این حلقه در حال اجرا نیست.")
    event.set()
    state["status"] = "stopping"
    state["updated_at"] = _now()
    _write(state, config.root)
    return state


def resume(config: Config, store: Any, run_id: str) -> dict[str, Any]:
    state = load_state(config.root, run_id)
    with _LOCK:
        busy = run_id in _ACTIVE or bool(_ACTIVE)
    if busy:
        raise ValueError("این حلقه از قبل در حال اجراست.")
    if state.get("status") == "done":
        raise ValueError("این حلقه تمام شده است؛ اجرای تازه بسازید.")
    bars, higher, filters = load_context(store, config, symbol=state["symbol"],
                                         timeframe=state["timeframe"], dataset_id=state["dataset_id"])
    state["status"] = "running"
    state["error"] = None
    state["updated_at"] = _now()
    _write(state, config.root)
    _start_thread(config, store, state, bars, higher, filters, config.root)
    return state


def export_csv(state: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["index", "hypothesis_id", "question", "verdict", "candidate_label",
                     "net_return_pct", "max_drawdown_pct", "profit_factor", "trades",
                     "dataset_id", "decided_at", "reasons", "learning"])
    for item in state.get("experiments", []):
        writer.writerow([
            item.get("index"), item.get("hypothesis_id"), item.get("question"), item.get("verdict"),
            item.get("candidate_label"), item.get("net_return_pct"), item.get("max_drawdown_pct"),
            item.get("profit_factor"), item.get("trades"), item.get("dataset_id"),
            item.get("decided_at"), "; ".join(item.get("reasons") or []), item.get("learning"),
        ])
    return "\ufeff" + buffer.getvalue()
