from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import AsyncIterator, Iterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from plotly.offline.offline import get_plotlyjs
from watchfiles import Change, awatch

from .git_diff import (
    ChurnMonitorError,
    collect_monitor_targets,
    collect_overview,
    collect_target_snapshot,
    merge_latest_timestamp,
    MonitorTarget,
    pick_selected_target_id,
    resolve_repo_root,
    resolve_watch_paths,
)
from .models import DiffSnapshot

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
) -> FastAPI:
    initial_root = (repo_root or Path.cwd()).resolve()
    try:
        resolved_root = resolve_repo_root(initial_root)
    except ChurnMonitorError:
        resolved_root = initial_root
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(title="Churn Monitor", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.watch_stop_event = Event()
    app.state.last_edit_overrides = {}
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    config = {
        "repoRoot": str(resolved_root),
        "homeDir": str(Path.home()),
        "defaultBase": default_base or "",
    }

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        template = (static_dir / "index.html").read_text(encoding="utf-8")
        asset_version = resolve_asset_version(static_dir)
        html = template.replace("__ASSET_VERSION__", asset_version).replace(
            "__APP_CONFIG__",
            json.dumps(config, ensure_ascii=True),
        )
        return HTMLResponse(content=html)

    @app.get("/plotly.min.js")
    def plotly_bundle() -> Response:
        return Response(content=get_plotlyjs(), media_type="text/javascript")

    @app.get("/api/snapshot")
    def snapshot(
        base: str | None = Query(default=None),
        target: str | None = Query(default=None),
    ) -> dict[str, object]:
        resolved_base = base or default_base
        try:
            overview_model = collect_overview(
                resolved_root,
                resolved_base,
                selected_target_id=target,
                last_edit_overrides=app.state.last_edit_overrides,
            )
        except ChurnMonitorError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        return overview_model.model_dump(mode="json")

    @app.get("/api/events")
    async def events(
        request: Request,
        base: str | None = Query(default=None),
        target: str | None = Query(default=None),
    ) -> StreamingResponse:
        resolved_base = base or default_base

        async def event_stream() -> AsyncIterator[str]:
            selected_target_id = target
            watch_paths = resolve_watch_paths(resolved_root)

            first_event = True
            try:
                for event_name, payload in stream_sync_events(
                    resolved_root,
                    resolved_base,
                    selected_target_id=selected_target_id,
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
                    next_watch_paths = resolve_watch_paths(resolved_root)
                    if next_watch_paths != watch_paths:
                        watch_paths = next_watch_paths
                        restart_watch = True

                    try:
                        for event_name, payload in stream_sync_events(
                            resolved_root,
                            resolved_base,
                            selected_target_id=selected_target_id,
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
        overview_model = collect_overview(
            repo_root,
            base_ref,
            selected_target_id=selected_target_id,
            last_edit_overrides=last_edit_overrides,
        )
    except ChurnMonitorError as exc:
        payload = {"status": exc.status_code, "detail": str(exc)}
        fingerprint = f"problem:{exc.status_code}:{payload['detail']}"
        return "problem", payload, fingerprint

    payload = overview_model.model_dump(mode="json")
    return "snapshot", payload, build_payload_fingerprint(payload)


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

    overrides = last_edit_overrides or {}
    effective_target_id = pick_selected_target_id(selected_target_id, targets)
    selected_target = next(target for target in targets if target.id == effective_target_id)

    yield "targets", {
        "reset": True,
        "selected_target_id": effective_target_id,
        "targets": [build_target_descriptor(target) for target in targets],
    }

    selected_snapshot = collect_target_snapshot(selected_target, base_ref)
    apply_last_edit_override(selected_snapshot, selected_target, overrides)
    yield "snapshot", selected_snapshot.model_dump(mode="json")
    yield "targets", {
        "reset": False,
        "selected_target_id": effective_target_id,
        "targets": [build_target_summary_payload(selected_target, selected_snapshot)],
    }

    pending_targets: list[dict[str, object]] = []
    for target in targets:
        if target.id == effective_target_id:
            continue

        snapshot = collect_target_snapshot(target, base_ref)
        apply_last_edit_override(snapshot, target, overrides)
        pending_targets.append(build_target_summary_payload(target, snapshot))
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


def build_target_descriptor(target: MonitorTarget) -> dict[str, object]:
    return {
        "id": target.id,
        "head_ref": target.head_ref,
        "worktree_path": str(target.worktree_path) if target.worktree_path is not None else None,
        "last_activity_at": None,
        "summary": None,
        "is_current": target.is_current,
    }


def build_target_summary_payload(
    target: MonitorTarget,
    snapshot: DiffSnapshot,
) -> dict[str, object]:
    snapshot_payload = snapshot.model_dump(mode="json")
    return {
        "id": snapshot_payload["target_id"],
        "head_ref": snapshot_payload["head_ref"],
        "worktree_path": snapshot_payload["worktree_path"],
        "last_activity_at": snapshot_payload["last_edit_at"] or snapshot_payload["head_commit_at"],
        "summary": snapshot_payload["summary"],
        "is_current": target.is_current,
    }


def apply_last_edit_override(
    snapshot: DiffSnapshot,
    target: MonitorTarget,
    overrides: dict[str, datetime],
) -> None:
    if target.last_edit_key and target.last_edit_key in overrides:
        snapshot.last_edit_at = merge_latest_timestamp(
            snapshot.last_edit_at,
            overrides[target.last_edit_key],
        )


def build_payload_fingerprint(payload: dict[str, object]) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


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


def resolve_asset_version(static_dir: Path) -> str:
    latest_mtime_ns = max(path.stat().st_mtime_ns for path in static_dir.iterdir() if path.is_file())
    return str(latest_mtime_ns)
