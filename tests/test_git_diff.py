from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from diff_treemap.app import create_app, encode_sse, snapshot_event
from diff_treemap.cli import signal_watchers_to_stop
from diff_treemap.git_diff import DiffTreemapError, collect_snapshot, resolve_base_ref


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write(repo: Path, relative: str, content: bytes) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "codex@example.com")
    git(tmp_path, "config", "user.name", "Codex")

    write(tmp_path, "src/alpha.py", b"one\ntwo\nthree\n")
    write(tmp_path, "README.md", b"base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "feat: initial commit")
    return tmp_path


def test_collect_snapshot_includes_branch_worktree_and_untracked(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    write(repo, "tests/test_alpha.py", b"def test_ok():\n    assert True\n")
    git(repo, "add", "tests/test_alpha.py")
    git(repo, "commit", "-m", "test: add coverage")

    write(repo, "src/alpha.py", b"one\nthree\nfour\nfive\n")
    write(repo, "README.md", b"base\nplus one\n")
    git(repo, "add", "README.md")
    write(repo, "notes.txt", b"scratch\nline two\n")

    snapshot = collect_snapshot(repo)

    assert snapshot.base_ref == "main"
    assert snapshot.summary.commit_count == 1
    assert snapshot.summary.changed_files == 4
    assert snapshot.summary.added_lines == 7
    assert snapshot.summary.deleted_lines == 1
    assert snapshot.summary.net_lines == 6
    assert snapshot.commits[0].subject == "test: add coverage"

    leaves = {node.path: node for node in snapshot.nodes if node.kind == "file"}
    assert leaves["src/alpha.py"].net_lines == 1
    assert leaves["tests/test_alpha.py"].added_lines == 2
    assert leaves["README.md"].added_lines == 1
    assert leaves["notes.txt"].added_lines == 2
    assert "src" in {node.id for node in snapshot.nodes}
    assert snapshot.snapshot_key
    assert snapshot.last_edit_at is not None


def test_resolve_base_ref_prefers_existing_default_branch(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    assert resolve_base_ref(repo) == "main"


def test_resolve_base_ref_uses_explicit_override(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    assert resolve_base_ref(repo, "HEAD") == "HEAD"


def test_collect_snapshot_marks_binary_untracked(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    write(repo, "assets/blob.bin", b"\0\1\2")

    snapshot = collect_snapshot(repo)

    binary_leaf = next(node for node in snapshot.nodes if node.path == "assets/blob.bin")
    assert binary_leaf.is_binary is True
    assert binary_leaf.value == 1
    assert snapshot.summary.binary_files == 1


def test_api_returns_error_for_unborn_head(tmp_path: Path) -> None:
    git(tmp_path, "init", "--initial-branch=main")
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/snapshot")

    assert response.status_code == 409
    assert "No commits found yet" in response.json()["detail"]


def test_collect_snapshot_requires_resolvable_base(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")

    with pytest.raises(DiffTreemapError):
        collect_snapshot(repo, "missing-branch")


def test_snapshot_event_returns_initial_snapshot(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    event_name, payload, fingerprint = snapshot_event(repo, None)

    assert event_name == "snapshot"
    assert payload["base_ref"] == "main"
    assert payload["head_ref"] == "feature"
    assert fingerprint.startswith("snapshot:")
    assert payload["summary"]["commit_count"] == 0
    assert "last_edit_at" in payload
    assert payload["commits"] == []

    encoded = encode_sse(event_name, payload, retry_ms=1000)
    assert encoded.startswith("retry: 1000\nevent: snapshot\n")
    payload_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
    decoded = json.loads(payload_line.removeprefix("data: "))
    assert decoded["snapshot_key"] == payload["snapshot_key"]


def test_snapshot_event_returns_problem_for_invalid_base(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    event_name, payload, fingerprint = snapshot_event(repo, "missing")

    assert event_name == "problem"
    assert payload["status"] == 404
    assert fingerprint.startswith("problem:")


def test_shutdown_sets_watch_stop_event(repo: Path) -> None:
    app = create_app(repo)

    with TestClient(app) as client:
        client.get("/api/snapshot")
        assert app.state.watch_stop_event.is_set() is False

    assert app.state.watch_stop_event.is_set() is True


def test_signal_watchers_to_stop_sets_watch_stop_event(repo: Path) -> None:
    app = create_app(repo)

    with TestClient(app) as client:
        client.get("/api/snapshot")
        signal_watchers_to_stop(app)
        assert app.state.watch_stop_event.is_set() is True


def test_snapshot_event_uses_watcher_last_edit(repo: Path) -> None:
    git(repo, "checkout", "-b", "feature")
    last_edit_at = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
    event_name, payload, fingerprint = snapshot_event(
        repo,
        None,
        last_edit_at=last_edit_at,
    )

    assert event_name == "snapshot"
    assert payload["last_edit_at"] == "2026-03-06T10:00:00Z"
    assert fingerprint.endswith("2026-03-06T10:00:00Z")
