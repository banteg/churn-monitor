from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .models import DiffNode, DiffSnapshot, SnapshotSummary

AUTODETECT_BASE_CANDIDATES: Final[tuple[str, ...]] = (
    "origin/HEAD",
    "origin/main",
    "origin/master",
    "main",
    "master",
)


class DiffTreemapError(RuntimeError):
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


def collect_snapshot(repo_root: Path, base_override: str | None = None) -> DiffSnapshot:
    repo_root = resolve_repo_root(repo_root)

    if not head_exists(repo_root):
        raise DiffTreemapError("No commits found yet. Create the first commit before diffing.")

    head_ref = resolve_head_ref(repo_root)
    base_ref = resolve_base_ref(repo_root, base_override)
    try:
        merge_base = git_text(repo_root, "merge-base", "HEAD", base_ref).strip()
    except DiffTreemapError as exc:
        raise DiffTreemapError(
            f"Unable to compute a merge base between HEAD and {base_ref}.",
            status_code=409,
        ) from exc

    tracked = parse_numstat_output(git_bytes(repo_root, "diff", "--numstat", "-z", "-M", merge_base))
    untracked = list(read_untracked_deltas(repo_root))

    leaves: dict[str, FileDelta] = {delta.path: delta for delta in tracked}
    for delta in untracked:
        leaves[delta.path] = delta

    nodes = build_nodes(repo_root, leaves.values())
    summary = build_summary(leaves.values())
    snapshot_key = compute_snapshot_key(repo_root, head_ref, base_ref, merge_base, leaves.values())

    return DiffSnapshot(
        repo_root=str(repo_root),
        head_ref=head_ref,
        base_ref=base_ref,
        merge_base=merge_base,
        snapshot_key=snapshot_key,
        generated_at=datetime.now(tz=UTC),
        summary=summary,
        nodes=nodes,
    )


def resolve_repo_root(repo_root: Path) -> Path:
    repo_root = repo_root.resolve()
    try:
        actual_root = git_text(repo_root, "rev-parse", "--show-toplevel").strip()
    except DiffTreemapError as exc:
        raise DiffTreemapError("This directory is not inside a Git repository.", status_code=404) from exc
    return Path(actual_root).resolve()


def head_exists(repo_root: Path) -> bool:
    try:
        git_text(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    except DiffTreemapError:
        return False
    return True


def resolve_head_ref(repo_root: Path) -> str:
    try:
        return git_text(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
    except DiffTreemapError:
        return git_text(repo_root, "rev-parse", "--short", "HEAD").strip()


def resolve_base_ref(repo_root: Path, base_override: str | None = None) -> str:
    if base_override:
        try:
            verify_ref(repo_root, base_override)
        except DiffTreemapError as exc:
            raise DiffTreemapError(
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
        except DiffTreemapError:
            continue
        return candidate

    raise DiffTreemapError(
        "Unable to resolve a base ref. Pass --base or use ?base=<ref>.",
        status_code=404,
    )


def iter_base_candidates(repo_root: Path) -> Iterable[str]:
    try:
        symbolic = git_text(repo_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    except DiffTreemapError:
        symbolic = ""

    if symbolic:
        yield symbolic.removeprefix("refs/remotes/")

    yield from AUTODETECT_BASE_CANDIDATES


def verify_ref(repo_root: Path, ref: str) -> None:
    git_text(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")


def git_text(repo_root: Path, *args: str) -> str:
    return run_git(repo_root, *args).decode("utf-8", errors="replace")


def git_bytes(repo_root: Path, *args: str) -> bytes:
    return run_git(repo_root, *args)


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
        raise DiffTreemapError(message)
    return completed.stdout


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


def build_summary(leaves: Iterable[FileDelta]) -> SnapshotSummary:
    items = list(leaves)
    added = sum(item.added_lines for item in items)
    deleted = sum(item.deleted_lines for item in items)
    return SnapshotSummary(
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

    return digest.hexdigest()[:12]
