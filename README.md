# Churn Monitor

Realtime local churn monitor for git branch diffs.

Churn Monitor starts a local web app that compares your current `HEAD` against a base ref and visualizes:

- churn and net line movement in a treemap
- changed files, additions, deletions, and commit count
- top additions and top cuts
- commits between the merge base and `HEAD`
- untracked files alongside tracked diff output

## Requirements

- Python 3.12+
- a Git repository with at least one commit
- `uv`

## Quick Start

```bash
uv sync
uv run churn-monitor
```

Then open `http://127.0.0.1:8000`.

To inspect another repository:

```bash
uv run churn-monitor --repo /path/to/repo
```

## Base Ref Resolution

If you do not pass `--base`, Churn Monitor tries these refs in order:

1. `origin/HEAD`
2. `origin/main`
3. `origin/master`
4. `main`
5. `master`

If none resolve, pass an explicit base:

```bash
uv run churn-monitor --base origin/main
```

## CLI

```bash
uv run churn-monitor --help
```

Options:

- `--base`: explicit base ref to diff against
- `--host`: host to bind the local server to
- `--port`: port for the local server
- `--debounce-ms`: file-watch debounce interval before recomputing
- `--repo`: repository root to inspect

## Development

```bash
uv sync --extra dev
npm --prefix frontend install
uv run python -m pytest
```

Frontend source lives in `frontend/`. Production assets are built into
`src/churn_monitor/client` for the FastAPI app to serve.

After frontend changes, rebuild with:

```bash
npm --prefix frontend run build
```
