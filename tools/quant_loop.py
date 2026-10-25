"""Run the autonomous research loop as a standalone, resumable process.

The API panel starts this file with the quant virtualenv interpreter; a killed
process is safe because every experiment ends with a checkpoint in
``state/quant/research.sqlite3``. Progress is printed as one JSON object per
line so the panel/stdout log can follow real work instead of a fake bar.

PAPER only: the loop never sends an order, never touches exchange credentials
and never changes the risk limits in ``config.yaml``.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cat.config import load_config  # noqa: E402
from cat.quant.loop import AutonomousResearchLoop  # noqa: E402
from cat.quant.settings import load_quant_settings  # noqa: E402
from cat.quant.store import ResearchStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous PAPER research loop")
    parser.add_argument("--search-symbols", type=int, default=30)
    parser.add_argument("--full-symbols", type=int, default=None)
    parser.add_argument("--max-experiments", type=int, default=None)
    parser.add_argument("--max-minutes", type=float, default=None)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--plan-key", default=None, help="run only the named experiments from LOOP_PLAN")
    args = parser.parse_args()

    from dataclasses import replace

    settings = load_quant_settings(load_config(root=ROOT))
    loop_overrides = {}
    if args.max_experiments is not None:
        loop_overrides["max_experiments"] = max(1, args.max_experiments)
    if args.max_minutes is not None:
        loop_overrides["max_minutes"] = max(1.0, args.max_minutes)
    if loop_overrides:
        settings = replace(settings, loop=replace(settings.loop, **loop_overrides))

    stop_event = threading.Event()

    def handle(signum, _frame):  # noqa: ANN001 - signal handler signature
        print(json.dumps({"phase": "loop", "message": f"signal {signum}; stopping after current step"}),
              flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    def progress(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=False, default=str), flush=True)

    store = ResearchStore(settings.database_path)
    plan = None
    if args.plan_key:
        from cat.quant.loop import LOOP_PLAN
        keys = [key.strip() for key in args.plan_key.split(",") if key.strip()]
        plan = [dict(item) for item in LOOP_PLAN if item["key"] in keys]
        if not plan:
            print(json.dumps({"phase": "loop", "message": f"no plan items matched {keys}"}), flush=True)
            return 2
    loop = AutonomousResearchLoop(settings, store, progress=progress, stop_event=stop_event,
                                  search_symbols=args.search_symbols, full_symbols=args.full_symbols)
    try:
        result = loop.run(resume=not args.no_resume, plan=plan)
    finally:
        store.close()
    print(json.dumps({"phase": "loop", "message": "final", "status": result.get("status"),
                      "loop_id": result.get("loop_id"), "experiments": result.get("experiments")}),
          flush=True)
    return 0 if result.get("status") in {"completed", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
