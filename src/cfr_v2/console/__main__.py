"""The CFR console (LDR-0023): a local website for everything done on the command line.

    python -m cfr_v2.console                 # http://127.0.0.1:8765
    python -m cfr_v2.console --port 9000

Listens on 127.0.0.1 only. Jobs run with this process's environment plus the repo's ``.env``,
read once at start. Needs the ``console`` extra (fastapi, uvicorn, python-multipart).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .settings import DEFAULT_PORT, ConsoleSettings

REPO_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.console", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--workspace", type=Path, help="where jobs keep logs and files (default var/console)")
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        print("error: the console needs its extra: pip install -e '.[console]'", file=sys.stderr)
        return 2

    from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

    from .app import create_app

    settings = ConsoleSettings.load(REPO_ROOT, workspace=args.workspace, port=args.port)
    init_telemetry(service_name="cfr-v2-console")
    print(f"CFR console on http://127.0.0.1:{args.port}", file=sys.stderr)
    try:
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
