from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from plotly.offline.offline import get_plotlyjs

from .git_diff import DiffTreemapError, collect_snapshot, resolve_repo_root


def create_app(
    repo_root: Path | None = None,
    *,
    default_base: str | None = None,
    poll_ms: int = 1000,
) -> FastAPI:
    initial_root = (repo_root or Path.cwd()).resolve()
    try:
        resolved_root = resolve_repo_root(initial_root)
    except DiffTreemapError:
        resolved_root = initial_root
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(title="Diff Treemap", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    config = {
        "repoRoot": str(resolved_root),
        "defaultBase": default_base or "",
        "pollMs": poll_ms,
    }

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

    return app
