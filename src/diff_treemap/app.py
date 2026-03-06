from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from plotly.offline.offline import get_plotlyjs
from watchfiles import Change, awatch

from .git_diff import DiffTreemapError, collect_snapshot, resolve_repo_root, resolve_watch_paths

WATCH_RETRY_MS = 1000
WATCH_DEBOUNCE_MS = 400
WATCH_KEEPALIVE_MS = 15000
IGNORED_WATCH_PARTS = {
    ".hypothesis",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}


def create_app(
    repo_root: Path | None = None,
    *,
    default_base: str | None = None,
    debounce_ms: int = WATCH_DEBOUNCE_MS,
) -> FastAPI:
    initial_root = (repo_root or Path.cwd()).resolve()
    try:
        resolved_root = resolve_repo_root(initial_root)
    except DiffTreemapError:
        resolved_root = initial_root
    watch_paths = resolve_watch_paths(resolved_root)
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(title="Diff Treemap", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.watch_stop_event = Event()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    config = {
        "repoRoot": str(resolved_root),
        "homeDir": str(Path.home()),
        "defaultBase": default_base or "",
    }
    app.state.last_edit_at = None

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        template = (static_dir / "index.html").read_text(encoding="utf-8")
        html = template.replace(
            "__APP_CONFIG__",
            json.dumps(config, ensure_ascii=True),
        )
        return HTMLResponse(content=html)

    @app.get("/plotly.min.js")
    def plotly_bundle() -> Response:
        return Response(content=get_plotlyjs(), media_type="text/javascript")

    @app.get("/api/snapshot")
    def snapshot(base: str | None = Query(default=None)) -> dict[str, object]:
        resolved_base = base or default_base
        try:
            snapshot_model = collect_snapshot(resolved_root, resolved_base)
        except DiffTreemapError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        return snapshot_model.model_dump(mode="json")

    @app.get("/api/events")
    async def events(
        request: Request,
        base: str | None = Query(default=None),
    ) -> StreamingResponse:
        resolved_base = base or default_base

        async def event_stream() -> AsyncIterator[str]:
            last_fingerprint: str | None = None

            event_name, payload, fingerprint = snapshot_event(
                resolved_root,
                resolved_base,
                last_edit_at=app.state.last_edit_at,
            )
            last_fingerprint = fingerprint
            yield encode_sse(event_name, payload, retry_ms=WATCH_RETRY_MS)

            async for changes in awatch(
                *watch_paths,
                watch_filter=watch_filter,
                debounce=max(debounce_ms, 50),
                step=50,
                stop_event=app.state.watch_stop_event,
                rust_timeout=WATCH_KEEPALIVE_MS,
                yield_on_timeout=True,
            ):
                if await request.is_disconnected():
                    break

                if not changes:
                    yield ": keepalive\n\n"
                    continue

                app.state.last_edit_at = detect_last_edit_at(changes)
                event_name, payload, fingerprint = snapshot_event(
                    resolved_root,
                    resolved_base,
                    last_edit_at=app.state.last_edit_at,
                )
                if fingerprint == last_fingerprint:
                    continue

                last_fingerprint = fingerprint
                yield encode_sse(event_name, payload)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.watch_stop_event.clear()
    try:
        yield
    finally:
        app.state.watch_stop_event.set()


def snapshot_event(
    repo_root: Path,
    base_ref: str | None,
    *,
    last_edit_at: datetime | None = None,
) -> tuple[str, dict[str, object], str]:
    try:
        snapshot_model = collect_snapshot(repo_root, base_ref)
    except DiffTreemapError as exc:
        payload = {"status": exc.status_code, "detail": str(exc)}
        fingerprint = f"problem:{exc.status_code}:{payload['detail']}"
        return "problem", payload, fingerprint

    if last_edit_at is not None:
        snapshot_model.last_edit_at = last_edit_at

    payload = snapshot_model.model_dump(mode="json")
    fingerprint_parts = [f"snapshot:{payload['snapshot_key']}"]
    if payload["last_edit_at"]:
        fingerprint_parts.append(payload["last_edit_at"])
    return "snapshot", payload, "|".join(fingerprint_parts)


def encode_sse(event: str, payload: dict[str, object], *, retry_ms: int | None = None) -> str:
    lines: list[str] = []
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"event: {event}")
    for line in json.dumps(payload, ensure_ascii=True).splitlines():
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def watch_filter(change: Change, path: str) -> bool:
    del change
    path_parts = set(Path(path).parts)
    return not path_parts.intersection(IGNORED_WATCH_PARTS)


def detect_last_edit_at(changes: set[tuple[Change, str]]) -> datetime | None:
    latest_mtime: float | None = None

    for change, raw_path in changes:
        path = Path(raw_path)
        if change == Change.deleted or not path.exists():
            if path.parent.exists():
                latest_mtime = max(latest_mtime or 0.0, path.parent.stat().st_mtime)
            continue

        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        latest_mtime = mtime if latest_mtime is None else max(latest_mtime, mtime)

    if latest_mtime is None:
        return None

    return datetime.fromtimestamp(latest_mtime, tz=UTC)
