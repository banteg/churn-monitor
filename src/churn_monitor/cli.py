from __future__ import annotations

import argparse
from types import FrameType
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .app import create_app

GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 1


class ChurnMonitorServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self._app = app

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        signal_watchers_to_stop(self._app)
        super().handle_exit(sig, frame)


def signal_watchers_to_stop(app: FastAPI) -> None:
    stop_event = getattr(app.state, "watch_stop_event", None)
    if stop_event is not None:
        stop_event.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Realtime churn monitor for a git diff from base.")
    parser.add_argument("--base", help="Explicit base ref to diff against.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local server to.")
    parser.add_argument("--port", type=int, default=8000, help="Port for the local server.")
    parser.add_argument(
        "--debounce-ms",
        type=int,
        default=400,
        help="Watch debounce interval in milliseconds before recomputing the snapshot.",
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
    app = create_app(repo_root, default_base=args.base, debounce_ms=max(args.debounce_ms, 50))
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    )
    server = ChurnMonitorServer(config, app)
    try:
        server.run()
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
