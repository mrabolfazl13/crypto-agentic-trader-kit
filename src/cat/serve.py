"""Loopback-only launcher for the local PAPER dashboard/API.

Binds to 127.0.0.1 so the unauthenticated service is never reachable from the
network. Reconfigures stdout/stderr to UTF-8 first because the Persian report
text would otherwise break on a legacy Windows cp1252 console.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="cat-serve", description="Local PAPER API + dashboard server")
    parser.add_argument("--port", type=int, default=8177, help="loopback port (default 8177)")
    parser.add_argument("--root", default=None, help="workspace root holding config.yaml and state/")
    args = parser.parse_args(argv)

    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(root=args.root), host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
