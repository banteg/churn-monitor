from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Realtime treemap view of a git diff from base.")
    parser.add_argument("--base", help="Explicit base ref to diff against.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local server to.")
    parser.add_argument("--port", type=int, default=8000, help="Port for the local server.")
    parser.add_argument(
        "--poll-ms",
        type=int,
        default=1000,
        help="Browser polling interval in milliseconds.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Git repository root to inspect. Defaults to the current directory.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    repo_root = Path(args.repo).resolve()
    app = create_app(repo_root, default_base=args.base, poll_ms=max(args.poll_ms, 250))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
