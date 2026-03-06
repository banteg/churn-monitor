from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .models import (
    CommitEntry,
    DiffNode,
    DiffSnapshot,
    MonitorOverview,
    MonitorTargetsPayload,
    MonitorTargetSummary,
    SnapshotSummary,
)

AUTODETECT_BASE_CANDIDATES: Final[tuple[str, ...]] = (
    "origin/HEAD",
    "origin/main",
    "origin/master",
    "main",
    "master",
)


class ChurnMonitorError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class FileDelta:
    path: str
    added_lines: int
    deleted_lines: int
    is_binary: bool = False
    previous_path: str | None = None

    @property
    def net_lines(self) -> int:
        return self.added_lines - self.deleted_lines

    @property
    def value(self) -> int:
        if self.is_binary:
            return 1
        return self.added_lines + self.deleted_lines


@dataclass(slots=True)
class MonitorTarget:
    id: str
    head_ref: str
    repo_root: Path
    worktree_path: Path | None
    is_current: bool

    @property
    def last_edit_key(self) -> str | None:
        if self.worktree_path is None:
            return None
        return str(self.worktree_path)


@dataclass(slots=True)
class WorktreeEntry:
    path: Path
    head_ref: str
    is_current: bool
    is_detached: bool = False


@dataclass(slots=True)
class SnapshotPlan:
    repo_root: Path
    head_ref: str
    head_spec: str
    base_ref: str
    merge_base: str
    target_id: str
    worktree_path: Path | None
    include_untracked: bool


def resolve_snapshot_plan(
    repo_root: Path,
    base_override: str | None = None,
    *,
    head_ref_override: str | None = None,
    target_id: str | None = None,
    worktree_path: Path | None = None,
) -> SnapshotPlan:
    resolved_root = resolve_repo_root(repo_root)

    if head_ref_override is None:
        if not head_exists(resolved_root):
            raise ChurnMonitorError("No commits found yet. Create the first commit before diffing.")
        head_ref = resolve_head_ref(resolved_root)
        head_spec = "HEAD"
        include_untracked = True
    else:
        try:
            verify_ref(resolved_root, head_ref_override)
        except ChurnMonitorError as exc:
            raise ChurnMonitorError(
                f"Head ref '{head_ref_override}' does not resolve to a commit.",
                status_code=404,
            ) from exc
        head_ref = head_ref_override
        head_spec = head_ref_override
        include_untracked = False

    base_ref = resolve_base_ref(resolved_root, base_override)
    try:
        merge_base = git_text(resolved_root, "merge-base", head_spec, base_ref).strip()
    except ChurnMonitorError as exc:
        raise ChurnMonitorError(
            f"Unable to compute a merge base between {head_ref} and {base_ref}.",
            status_code=409,
        ) from exc

    return SnapshotPlan(
        repo_root=resolved_root,
        head_ref=head_ref,
        head_spec=head_spec,
        base_ref=base_ref,
        merge_base=merge_base,
        target_id=target_id or build_branch_target_id(head_ref),
        worktree_path=worktree_path,
        include_untracked=include_untracked,
    )


def collect_snapshot(
    repo_root: Path,
    base_override: str | None = None,
    *,
    head_ref_override: str | None = None,
    target_id: str | None = None,
    worktree_path: Path | None = None,
) -> DiffSnapshot:
    plan = resolve_snapshot_plan(
        repo_root,
        base_override,
        head_ref_override=head_ref_override,
        target_id=target_id,
        worktree_path=worktree_path,
    )

    tracked = parse_numstat_output(git_numstat(plan.repo_root, plan.merge_base, plan.head_spec))
    untracked = list(read_untracked_deltas(plan.repo_root)) if plan.include_untracked else []
    commits = collect_commits(plan.repo_root, plan.merge_base, head_spec=plan.head_spec)

    leaves: dict[str, FileDelta] = {delta.path: delta for delta in tracked}
    for delta in untracked:
        leaves[delta.path] = delta

    nodes = build_nodes(plan.repo_root, leaves.values())
    summary = build_summary(leaves.values(), len(commits))
    snapshot_key = compute_snapshot_key(
        plan.repo_root,
        plan.head_ref,
        plan.base_ref,
        plan.merge_base,
        leaves.values(),
        commits,
    )
    last_edit_at = infer_last_edit_at(plan.repo_root, leaves.values()) if plan.include_untracked else None
    head_commit_at = resolve_ref_commit_at(plan.repo_root, plan.head_spec)

    return DiffSnapshot(
        target_id=plan.target_id,
        repo_root=str(plan.repo_root),
        worktree_path=str(plan.worktree_path) if plan.worktree_path is not None else None,
        head_ref=plan.head_ref,
        head_commit_at=head_commit_at,
        base_ref=plan.base_ref,
        merge_base=plan.merge_base,
        snapshot_key=snapshot_key,
        last_edit_at=last_edit_at,
        generated_at=datetime.now(tz=UTC),
        summary=summary,
        commits=commits,
        nodes=nodes,
    )


def collect_targets_payload(
    repo_root: Path,
    *,
    selected_target_id: str | None = None,
    branch_limit: int | None = None,
) -> MonitorTargetsPayload:
    targets = collect_monitor_targets(
        repo_root,
        selected_target_id=selected_target_id,
        branch_limit=branch_limit,
    )
    if not targets:
        raise ChurnMonitorError("No commits found yet. Create the first commit before diffing.")

    effective_target_id = pick_selected_target_id(selected_target_id, targets)
    return MonitorTargetsPayload(
        selected_target_id=effective_target_id,
        targets=[build_target_descriptor(target) for target in targets],
    )


def collect_target_summaries(
    repo_root: Path,
    base_override: str | None = None,
    *,
    selected_target_id: str | None = None,
    last_edit_overrides: dict[str, datetime] | None = None,
    branch_limit: int | None = None,
) -> MonitorTargetsPayload:
    targets = collect_monitor_targets(
        repo_root,
        selected_target_id=selected_target_id,
        branch_limit=branch_limit,
    )
    if not targets:
        raise ChurnMonitorError("No commits found yet. Create the first commit before diffing.")

    effective_target_id = pick_selected_target_id(selected_target_id, targets)
    summaries = [
        collect_target_summary(
            target,
            base_override,
            last_edit_overrides=last_edit_overrides,
        )
        for target in targets
    ]
    summaries.sort(key=monitor_target_sort_key)
    return MonitorTargetsPayload(selected_target_id=effective_target_id, targets=summaries)


def collect_snapshot_for_target(
    repo_root: Path,
    base_override: str | None = None,
    *,
    selected_target_id: str | None = None,
    last_edit_overrides: dict[str, datetime] | None = None,
    branch_limit: int | None = None,
) -> DiffSnapshot:
    targets = collect_monitor_targets(
        repo_root,
        selected_target_id=selected_target_id,
        branch_limit=branch_limit,
    )
    if not targets:
        raise ChurnMonitorError("No commits found yet. Create the first commit before diffing.")

    effective_target_id = pick_selected_target_id(selected_target_id, targets)
    target = resolve_target(targets, effective_target_id)
    snapshot = collect_target_snapshot(target, base_override)
    return apply_snapshot_overrides(snapshot, target, last_edit_overrides)


def collect_overview(
    repo_root: Path,
    base_override: str | None = None,
    *,
    selected_target_id: str | None = None,
    last_edit_overrides: dict[str, datetime] | None = None,
    branch_limit: int | None = None,
) -> MonitorOverview:
    summaries_payload = collect_target_summaries(
        repo_root,
        base_override,
        selected_target_id=selected_target_id,
        last_edit_overrides=last_edit_overrides,
        branch_limit=branch_limit,
    )
    snapshot = collect_snapshot_for_target(
        repo_root,
        base_override,
        selected_target_id=summaries_payload.selected_target_id,
        last_edit_overrides=last_edit_overrides,
        branch_limit=branch_limit,
    )
    return MonitorOverview(
        selected_target_id=summaries_payload.selected_target_id,
        targets=summaries_payload.targets,
        snapshot=snapshot,
    )


def collect_target_snapshot(target: MonitorTarget, base_override: str | None = None) -> DiffSnapshot:
    if target.worktree_path is not None:
        return collect_snapshot(
            target.repo_root,
            base_override,
            target_id=target.id,
            worktree_path=target.worktree_path,
        )

    return collect_snapshot(
        target.repo_root,
        base_override,
        head_ref_override=target.head_ref,
        target_id=target.id,
    )


def collect_target_summary(
    target: MonitorTarget,
    base_override: str | None = None,
    *,
    last_edit_overrides: dict[str, datetime] | None = None,
) -> MonitorTargetSummary:
    plan = resolve_snapshot_plan(
        target.repo_root,
        base_override,
        head_ref_override=None if target.worktree_path is not None else target.head_ref,
        target_id=target.id,
        worktree_path=target.worktree_path,
    )
    tracked = parse_numstat_output(git_numstat(plan.repo_root, plan.merge_base, plan.head_spec))
    untracked = list(read_untracked_deltas(plan.repo_root)) if plan.include_untracked else []
    leaves: dict[str, FileDelta] = {delta.path: delta for delta in tracked}
    for delta in untracked:
        leaves[delta.path] = delta

    commit_count = count_commits(plan.repo_root, plan.merge_base, head_spec=plan.head_spec)
    last_edit_at = infer_last_edit_at(plan.repo_root, leaves.values()) if plan.include_untracked else None
    if last_edit_overrides:
        if target.last_edit_key and target.last_edit_key in last_edit_overrides:
            last_edit_at = merge_latest_timestamp(last_edit_at, last_edit_overrides[target.last_edit_key])
    head_commit_at = resolve_ref_commit_at(plan.repo_root, plan.head_spec)
    return MonitorTargetSummary(
        id=plan.target_id,
        head_ref=plan.head_ref,
        worktree_path=str(plan.worktree_path) if plan.worktree_path is not None else None,
        last_activity_at=last_edit_at or head_commit_at,
        summary=build_summary(leaves.values(), commit_count),
        is_current=target.is_current,
    )


def build_target_descriptor(target: MonitorTarget) -> MonitorTargetSummary:
    return MonitorTargetSummary(
        id=target.id,
        head_ref=target.head_ref,
        worktree_path=str(target.worktree_path) if target.worktree_path is not None else None,
        is_current=target.is_current,
    )


def apply_snapshot_overrides(
    snapshot: DiffSnapshot,
    target: MonitorTarget,
    last_edit_overrides: dict[str, datetime] | None = None,
) -> DiffSnapshot:
    if last_edit_overrides and target.last_edit_key and target.last_edit_key in last_edit_overrides:
        snapshot.last_edit_at = merge_latest_timestamp(
            snapshot.last_edit_at,
            last_edit_overrides[target.last_edit_key],
        )
    return snapshot


def resolve_target(targets: list[MonitorTarget], target_id: str) -> MonitorTarget:
    for target in targets:
        if target.id == target_id:
            return target
    raise ChurnMonitorError(f"Target '{target_id}' does not exist.", status_code=404)


def collect_monitor_targets(
    repo_root: Path,
    *,
    selected_target_id: str | None = None,
    branch_limit: int | None = None,
) -> list[MonitorTarget]:
    resolved_root = resolve_repo_root(repo_root)
    worktree_entries = list_worktrees(resolved_root)

    targets: list[MonitorTarget] = []
    active_branches: set[str] = set()
    for entry in worktree_entries:
        target_id = (
            build_detached_target_id(entry.path)
            if entry.is_detached
            else build_branch_target_id(entry.head_ref)
        )
        targets.append(
            MonitorTarget(
                id=target_id,
                head_ref=entry.head_ref,
                repo_root=entry.path,
                worktree_path=entry.path,
                is_current=entry.is_current,
            )
        )
        if not entry.is_detached:
            active_branches.add(entry.head_ref)

    branch_count = 0
    for branch in list_local_branches(resolved_root):
        if branch in active_branches:
            continue
        target_id = build_branch_target_id(branch)
        if branch_limit is not None and branch_count >= branch_limit and target_id != selected_target_id:
            continue
        targets.append(
            MonitorTarget(
                id=target_id,
                head_ref=branch,
                repo_root=resolved_root,
                worktree_path=None,
                is_current=False,
            )
        )
        branch_count += 1

    return targets


def list_worktrees(repo_root: Path) -> list[WorktreeEntry]:
    resolved_root = resolve_repo_root(repo_root)
    raw = git_text(resolved_root, "worktree", "list", "--porcelain")
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                records.append(current)
            current = {"worktree": line.removeprefix("worktree ")}
            continue

        key, _, value = line.partition(" ")
        current[key] = value

    if current:
        records.append(current)

    entries: list[WorktreeEntry] = []
    for record in records:
        if "prunable" in record:
            continue

        path = Path(record["worktree"]).resolve()
        if not path.exists():
            continue

        branch = record.get("branch")
        head_sha = record.get("HEAD", "")
        is_detached = "detached" in record or not branch
        head_ref = branch.removeprefix("refs/heads/") if branch else head_sha[:12]
        entries.append(
            WorktreeEntry(
                path=path,
                head_ref=head_ref,
                is_current=path == resolved_root,
                is_detached=is_detached,
            )
        )

    return entries


def build_branch_target_id(head_ref: str) -> str:
    return f"branch:{head_ref}"


def build_detached_target_id(worktree_path: Path) -> str:
    return f"detached:{worktree_path}"


def pick_selected_target_id(
    selected_target_id: str | None,
    targets: list[MonitorTargetSummary | MonitorTarget],
) -> str:
    if selected_target_id and any(target.id == selected_target_id for target in targets):
        return selected_target_id

    for target in targets:
        if target.is_current:
            return target.id

    return targets[0].id


def monitor_target_sort_key(target: MonitorTargetSummary) -> tuple[float, str]:
    if target.last_activity_at is None:
        timestamp = 0.0
    else:
        timestamp = target.last_activity_at.timestamp()
    return (-timestamp, target.head_ref.casefold())


def merge_latest_timestamp(
    left: datetime | None,
    right: datetime | None,
) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def resolve_repo_root(repo_root: Path) -> Path:
    repo_root = repo_root.resolve()
    try:
        actual_root = git_text(repo_root, "rev-parse", "--show-toplevel").strip()
    except ChurnMonitorError as exc:
        raise ChurnMonitorError("This directory is not inside a Git repository.", status_code=404) from exc
    return Path(actual_root).resolve()


def resolve_watch_paths(repo_root: Path) -> tuple[Path, ...]:
    resolved_root = resolve_repo_root(repo_root)
    paths: list[Path] = []

    for worktree in list_worktrees(resolved_root):
        if worktree.path not in paths:
            paths.append(worktree.path)

    for args in (
        ("rev-parse", "--absolute-git-dir"),
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
    ):
        try:
            candidate = resolve_git_path(resolved_root, *args)
        except ChurnMonitorError:
            continue

        if candidate not in paths:
            paths.append(candidate)

    return tuple(paths)


def head_exists(repo_root: Path) -> bool:
    try:
        git_text(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    except ChurnMonitorError:
        return False
    return True


def resolve_head_ref(repo_root: Path) -> str:
    try:
        return git_text(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
    except ChurnMonitorError:
        return git_text(repo_root, "rev-parse", "--short", "HEAD").strip()


def resolve_ref_commit_at(repo_root: Path, ref: str) -> datetime | None:
    raw = git_text(repo_root, "show", "-s", "--format=%ct", ref).strip()
    if not raw:
        return None
    return datetime.fromtimestamp(int(raw), tz=UTC)


def resolve_base_ref(repo_root: Path, base_override: str | None = None) -> str:
    if base_override:
        try:
            verify_ref(repo_root, base_override)
        except ChurnMonitorError as exc:
            raise ChurnMonitorError(
                f"Base ref '{base_override}' does not resolve to a commit.",
                status_code=404,
            ) from exc
        return base_override

    seen: set[str] = set()
    for candidate in iter_base_candidates(repo_root):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            verify_ref(repo_root, candidate)
        except ChurnMonitorError:
            continue
        return candidate

    raise ChurnMonitorError(
        "Unable to resolve a base ref. Pass --base or use ?base=<ref>.",
        status_code=404,
    )


def iter_base_candidates(repo_root: Path) -> Iterable[str]:
    try:
        symbolic = git_text(repo_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    except ChurnMonitorError:
        symbolic = ""

    if symbolic:
        yield symbolic.removeprefix("refs/remotes/")

    yield from AUTODETECT_BASE_CANDIDATES


def list_local_branches(repo_root: Path) -> list[str]:
    raw = git_text(
        repo_root,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)",
        "refs/heads",
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def verify_ref(repo_root: Path, ref: str) -> None:
    git_text(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")


def resolve_git_path(repo_root: Path, *args: str) -> Path:
    raw = git_text(repo_root, *args).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def git_text(repo_root: Path, *args: str) -> str:
    return run_git(repo_root, *args).decode("utf-8", errors="replace")


def git_bytes(repo_root: Path, *args: str) -> bytes:
    return run_git(repo_root, *args)


def git_numstat(repo_root: Path, merge_base: str, head_spec: str) -> bytes:
    if head_spec == "HEAD":
        return git_bytes(repo_root, "diff", "--numstat", "-z", "-M", merge_base)
    return git_bytes(repo_root, "diff", "--numstat", "-z", "-M", f"{merge_base}..{head_spec}")


def run_git(repo_root: Path, *args: str) -> bytes:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip() or "Git command failed."
        raise ChurnMonitorError(message)
    return completed.stdout


def collect_commits(repo_root: Path, merge_base: str, *, head_spec: str = "HEAD") -> list[CommitEntry]:
    raw = git_text(
        repo_root,
        "log",
        "--format=%H%x1f%ct%x1f%s%x1e",
        f"{merge_base}..{head_spec}",
    )
    commits: list[CommitEntry] = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        sha, committed_at_unix, subject = record.rstrip("\n").split("\x1f", maxsplit=2)
        commits.append(
            CommitEntry(
                sha=sha,
                subject=subject,
                committed_at=datetime.fromtimestamp(int(committed_at_unix), tz=UTC),
            )
        )
    return commits


def count_commits(repo_root: Path, merge_base: str, *, head_spec: str = "HEAD") -> int:
    raw = git_text(repo_root, "rev-list", "--count", f"{merge_base}..{head_spec}").strip()
    return int(raw or "0")


def parse_numstat_output(raw: bytes) -> list[FileDelta]:
    deltas: list[FileDelta] = []
    index = 0
    size = len(raw)

    while index < size:
        add_end = raw.find(b"\t", index)
        delete_end = raw.find(b"\t", add_end + 1)
        if add_end == -1 or delete_end == -1:
            break

        added_token = raw[index:add_end].decode("utf-8", errors="replace")
        deleted_token = raw[add_end + 1 : delete_end].decode("utf-8", errors="replace")
        index = delete_end + 1

        previous_path: str | None = None
        if index < size and raw[index] == 0:
            index += 1
            previous_path, index = read_nul_terminated(raw, index)
            path, index = read_nul_terminated(raw, index)
        else:
            path, index = read_nul_terminated(raw, index)

        is_binary = added_token == "-" or deleted_token == "-"
        deltas.append(
            FileDelta(
                path=path,
                added_lines=0 if is_binary else int(added_token),
                deleted_lines=0 if is_binary else int(deleted_token),
                is_binary=is_binary,
                previous_path=previous_path,
            )
        )

    return deltas


def read_nul_terminated(raw: bytes, index: int) -> tuple[str, int]:
    end = raw.find(b"\0", index)
    if end == -1:
        end = len(raw)
    token = raw[index:end].decode("utf-8", errors="surrogateescape")
    return token, end + 1


def read_untracked_deltas(repo_root: Path) -> Iterable[FileDelta]:
    raw = git_bytes(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        relative = entry.decode("utf-8", errors="surrogateescape")
        path = repo_root / relative
        if not path.is_file():
            continue

        sample = path.read_bytes()
        if looks_binary(sample):
            yield FileDelta(path=relative, added_lines=0, deleted_lines=0, is_binary=True)
            continue

        yield FileDelta(path=relative, added_lines=count_lines(sample), deleted_lines=0)


def looks_binary(content: bytes) -> bool:
    if not content:
        return False
    if b"\0" in content:
        return True

    sample = content[:8192]
    suspicious = 0
    for byte in sample:
        if byte in b"\n\r\t\f\b":
            continue
        if 32 <= byte <= 126:
            continue
        suspicious += 1

    return suspicious / len(sample) > 0.30


def count_lines(content: bytes) -> int:
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def build_nodes(repo_root: Path, leaves: Iterable[FileDelta]) -> list[DiffNode]:
    root_id = "."
    root_label = repo_root.name or str(repo_root)
    accumulator: dict[str, dict[str, object]] = {
        root_id: {
            "id": root_id,
            "parent": None,
            "label": root_label,
            "path": ".",
            "kind": "root",
            "value": 0,
            "added_lines": 0,
            "deleted_lines": 0,
            "net_lines": 0,
            "is_binary": False,
            "previous_path": None,
        }
    }

    sorted_leaves = sorted(leaves, key=lambda item: item.path)
    for leaf in sorted_leaves:
        parts = [part for part in leaf.path.split("/") if part]
        parent_id = root_id

        for depth, part in enumerate(parts[:-1], start=1):
            node_id = "/".join(parts[:depth])
            parent_path = "/".join(parts[: depth - 1]) if depth > 1 else "."
            node = accumulator.setdefault(
                node_id,
                {
                    "id": node_id,
                    "parent": parent_id,
                    "label": part,
                    "path": node_id,
                    "kind": "dir",
                    "value": 0,
                    "added_lines": 0,
                    "deleted_lines": 0,
                    "net_lines": 0,
                    "is_binary": False,
                    "previous_path": None,
                },
            )
            node["parent"] = parent_id
            node["path"] = node_id if node_id != root_id else parent_path
            parent_id = node_id

        leaf_id = leaf.path
        accumulator[leaf_id] = {
            "id": leaf_id,
            "parent": parent_id,
            "label": parts[-1] if parts else leaf.path,
            "path": leaf.path,
            "kind": "file",
            "value": 0,
            "added_lines": 0,
            "deleted_lines": 0,
            "net_lines": 0,
            "is_binary": leaf.is_binary,
            "previous_path": leaf.previous_path,
        }

        current_id: str | None = leaf_id
        while current_id is not None:
            node = accumulator[current_id]
            node["value"] = int(node["value"]) + leaf.value
            node["added_lines"] = int(node["added_lines"]) + leaf.added_lines
            node["deleted_lines"] = int(node["deleted_lines"]) + leaf.deleted_lines
            node["net_lines"] = int(node["net_lines"]) + leaf.net_lines
            current_id = node["parent"]

    return [DiffNode.model_validate(accumulator[key]) for key in sort_nodes(accumulator)]


def sort_nodes(nodes: dict[str, dict[str, object]]) -> list[str]:
    def node_key(node_id: str) -> tuple[int, str]:
        if node_id == ".":
            return (0, "")
        return (1, node_id)

    return sorted(nodes, key=node_key)


def build_summary(leaves: Iterable[FileDelta], commit_count: int) -> SnapshotSummary:
    items = list(leaves)
    added = sum(item.added_lines for item in items)
    deleted = sum(item.deleted_lines for item in items)
    return SnapshotSummary(
        commit_count=commit_count,
        added_lines=added,
        deleted_lines=deleted,
        net_lines=added - deleted,
        changed_files=len(items),
        binary_files=sum(1 for item in items if item.is_binary),
    )


def compute_snapshot_key(
    repo_root: Path,
    head_ref: str,
    base_ref: str,
    merge_base: str,
    leaves: Iterable[FileDelta],
    commits: Iterable[CommitEntry],
) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(str(repo_root).encode("utf-8"))
    digest.update(b"\0")
    digest.update(head_ref.encode("utf-8"))
    digest.update(b"\0")
    digest.update(base_ref.encode("utf-8"))
    digest.update(b"\0")
    digest.update(merge_base.encode("utf-8"))
    digest.update(b"\0")

    for leaf in sorted(leaves, key=lambda item: item.path):
        digest.update(leaf.path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(leaf.added_lines).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(leaf.deleted_lines).encode("ascii"))
        digest.update(b"\0")
        digest.update(b"1" if leaf.is_binary else b"0")
        digest.update(b"\0")
        if leaf.previous_path:
            digest.update(leaf.previous_path.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")

    for commit in commits:
        digest.update(commit.sha.encode("ascii"))
        digest.update(b"\0")

    return digest.hexdigest()[:12]


def infer_last_edit_at(repo_root: Path, leaves: Iterable[FileDelta]) -> datetime | None:
    latest_mtime: float | None = None
    for leaf in leaves:
        path = repo_root / leaf.path
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        latest_mtime = mtime if latest_mtime is None else max(latest_mtime, mtime)

    if latest_mtime is None:
        return None

    return datetime.fromtimestamp(latest_mtime, tz=UTC)
