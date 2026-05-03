
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .gateway import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="embedded-myceliadb",
        description="Start the self-contained MyceliaDB-compatible gateway bundled with DocForge Enterprise.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address. Keep loopback for local enterprise runs.")
    parser.add_argument("--port", type=int, default=9999, help="Gateway port.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".docforge_workspace/embedded_myceliadb"),
        help="Persistent embedded MyceliaDB vault directory.",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Local transport token. If omitted, MYCELIA_LOCAL_TOKEN is used when present.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress HTTP request logging.")
    args = parser.parse_args(argv)
    token = args.token or os.environ.get("MYCELIA_LOCAL_TOKEN", "")
    serve(host=args.host, port=args.port, root=args.root, token=token, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
