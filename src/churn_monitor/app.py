from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import AsyncIterator, Iterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from watchfiles import Change, awatch

from .git_diff import (
    ChurnMonitorError,
    build_target_descriptor,
    collect_monitor_targets,
    collect_snapshot_for_target,
    collect_target_summary,
    collect_targets_payload,
    pick_selected_target_id,
    resolve_repo_root,
    resolve_watch_paths,
)

WATCH_RETRY_MS = 1000
WATCH_DEBOUNCE_MS = 400
WATCH_KEEPALIVE_MS = 15000
TARGET_SUMMARY_BATCH_SIZE = 12
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
    client_dir: Path | None = None,
) -> FastAPI:
    initial_root = (repo_root or Path.cwd()).resolve()
    try:
        resolved_root = resolve_repo_root(initial_root)
    except ChurnMonitorError:
        resolved_root = initial_root

    resolved_client_dir = client_dir or (Path(__file__).resolve().parent / "client")
    app = FastAPI(title="Churn Monitor", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.watch_stop_event = Event()
    app.state.last_edit_overrides = {}
    app.mount(
        "/assets",
        StaticFiles(directory=resolved_client_dir / "assets", check_dir=False),
        name="assets",
    )

    config = {
        "repoRoot": str(resolved_root),
        "homeDir": str(Path.home()),
        "defaultBase": default_base or "",
    }

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def index() -> Response:
        index_path = resolved_client_dir / "index.html"
        if not index_path.exists():
            return HTMLResponse(
                content="Frontend build missing. Run `npm --prefix frontend install` and `npm --prefix frontend run build`.",
                status_code=503,
            )
        return FileResponse(index_path)

    @app.get("/api/config")
    def api_config() -> dict[str, str]:
        return config

    @app.get("/api/targets")
    def targets(target: str | None = Query(default=None)) -> dict[str, object]:
        try:
            payload = collect_targets_payload(resolved_root, selected_target_id=target)
        except ChurnMonitorError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return payload.model_dump(mode="json")

    @app.get("/api/snapshot")
    def snapshot(
        base: str | None = Query(default=None),
        target: str | None = Query(default=None),
    ) -> dict[str, object]:
        resolved_base = base or default_base
        try:
            snapshot_model = collect_snapshot_for_target(
                resolved_root,
                resolved_base,
                selected_target_id=target,
                last_edit_overrides=app.state.last_edit_overrides,
            )
        except ChurnMonitorError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        return snapshot_model.model_dump(mode="json")

    @app.get("/api/events")
    async def events(
        request: Request,
        base: str | None = Query(default=None),
    ) -> StreamingResponse:
        resolved_base = base or default_base

        async def event_stream() -> AsyncIterator[str]:
            watch_paths = resolve_watch_paths(resolved_root)
            first_event = True

            try:
                for event_name, payload in stream_sync_events(
                    resolved_root,
                    resolved_base,
                    last_edit_overrides=app.state.last_edit_overrides,
                ):
                    if await request.is_disconnected():
                        return
                    yield encode_sse(
                        event_name,
                        payload,
                        retry_ms=WATCH_RETRY_MS if first_event else None,
                    )
                    first_event = False
            except ChurnMonitorError as exc:
                payload = {"status": exc.status_code, "detail": str(exc)}
                yield encode_sse("problem", payload, retry_ms=WATCH_RETRY_MS)
                return

            while True:
                restart_watch = False
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
                        return

                    if not changes:
                        yield ": keepalive\n\n"
                        continue

                    app.state.last_edit_overrides.update(
                        detect_target_last_edits(resolved_root, changes),
                    )
                    yield encode_sse("invalidate", {"at": format_timestamp(datetime.now(tz=UTC))})

                    next_watch_paths = resolve_watch_paths(resolved_root)
                    if next_watch_paths != watch_paths:
                        watch_paths = next_watch_paths
                        restart_watch = True

                    try:
                        for event_name, payload in stream_sync_events(
                            resolved_root,
                            resolved_base,
                            last_edit_overrides=app.state.last_edit_overrides,
                        ):
                            if await request.is_disconnected():
                                return
                            yield encode_sse(event_name, payload)
                    except ChurnMonitorError as exc:
                        payload = {"status": exc.status_code, "detail": str(exc)}
                        yield encode_sse("problem", payload)
                        return

                    if restart_watch:
                        break

                if not restart_watch:
                    break

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
    app.state.last_edit_overrides = {}
    try:
        yield
    finally:
        app.state.watch_stop_event.set()


def snapshot_event(
    repo_root: Path,
    base_ref: str | None,
    *,
    selected_target_id: str | None = None,
    last_edit_overrides: dict[str, datetime] | None = None,
) -> tuple[str, dict[str, object], str]:
    try:
        snapshot_model = collect_snapshot_for_target(
            repo_root,
            base_ref,
            selected_target_id=selected_target_id,
            last_edit_overrides=last_edit_overrides,
        )
    except ChurnMonitorError as exc:
        payload = {"status": exc.status_code, "detail": str(exc)}
        fingerprint = f"problem:{exc.status_code}:{payload['detail']}"
        return "problem", payload, fingerprint

    payload = snapshot_model.model_dump(mode="json")
    fingerprint_parts = [f"snapshot:{payload['target_id']}:{payload['snapshot_key']}"]
    if payload["last_edit_at"]:
        fingerprint_parts.append(payload["last_edit_at"])
    return "snapshot", payload, "|".join(fingerprint_parts)


def stream_sync_events(
    repo_root: Path,
    base_ref: str | None,
    *,
    selected_target_id: str | None = None,
    last_edit_overrides: dict[str, datetime] | None = None,
) -> Iterator[tuple[str, dict[str, object]]]:
    resolved_root = resolve_repo_root(repo_root)
    targets = collect_monitor_targets(resolved_root)
    if not targets:
        raise ChurnMonitorError("No commits found yet. Create the first commit before diffing.")

    effective_target_id = pick_selected_target_id(selected_target_id, targets)
    yield "targets", {
        "reset": True,
        "selected_target_id": effective_target_id,
        "targets": [build_target_descriptor(target).model_dump(mode="json") for target in targets],
    }

    pending_targets: list[dict[str, object]] = []
    for target in targets:
        summary = collect_target_summary(
            target,
            base_ref,
            last_edit_overrides=last_edit_overrides,
        )
        pending_targets.append(summary.model_dump(mode="json"))
        if len(pending_targets) >= TARGET_SUMMARY_BATCH_SIZE:
            yield "targets", {
                "reset": False,
                "selected_target_id": effective_target_id,
                "targets": pending_targets,
            }
            pending_targets = []

    if pending_targets:
        yield "targets", {
            "reset": False,
            "selected_target_id": effective_target_id,
            "targets": pending_targets,
        }


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


def detect_target_last_edits(
    repo_root: Path,
    changes: set[tuple[Change, str]],
) -> dict[str, datetime]:
    overrides: dict[str, datetime] = {}
    for target in collect_monitor_targets(repo_root):
        if target.worktree_path is None:
            continue

        relevant_changes = {
            change
            for change in changes
            if path_is_within(Path(change[1]), target.worktree_path)
        }
        if not relevant_changes:
            continue

        last_edit_at = detect_last_edit_at(relevant_changes)
        if last_edit_at is not None:
            overrides[str(target.worktree_path)] = last_edit_at

    return overrides


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


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


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
